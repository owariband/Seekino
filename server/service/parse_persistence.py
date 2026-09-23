from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import monotonic_ns
from typing import Callable, Literal

from engine.parse.embed import ChunkEmbedding
from engine.parse.persist import ParsePipelineStage
from engine.parse.pipeline import ParsePipelineResult
from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseModels,
    VideoParseResult,
)
from gateway.embedding import EmbeddingResult

from ..config.parse_profile import ParseConfig
from ..config.video_processing import VideoProcessingConfig
from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from ..model.dto.parseDTO import PreparedEmbedding
from ..model.job import ParseJobStage, VideoChunkCheckpointStage
from ..mq.contracts import ParseJobLeaseLostError
from ..repository.job import ParseJobRepository
from ..repository.video import VideoRepository
from ..schema import VideoChunkKeyframeReference
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore
from .parse_import import prepare_completed_video_parse_import


class VideoParsePersistence:
    """Lease-fenced durability adapter for the Engine Parse pipeline."""

    def __init__(
        self,
        jobs: ParseJobRepository,
        videos: VideoRepository,
        artifacts: ArtifactStore,
        *,
        lease: ParseJobLease,
        attempt_directory: Path,
        checkpoints: dict[int, VideoChunkCheckpointState],
        chunk_media_refs: Mapping[int, str],
        analysis_window_media_refs: Mapping[int, str],
        keyframes_by_sequence: Mapping[
            int,
            tuple[VideoChunkKeyframeReference, ...],
        ],
        config: VideoProcessingConfig,
        parse_config: ParseConfig | None,
        missing_legacy_background: bool,
        processing_started_ns: int,
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._jobs = jobs
        self._videos = videos
        self._artifacts = artifacts
        self._lease = lease
        self._attempt_directory = attempt_directory
        self._checkpoints = checkpoints
        self._chunk_media_refs = dict(chunk_media_refs)
        self._analysis_window_media_refs = dict(analysis_window_media_refs)
        self._keyframes_by_sequence = dict(keyframes_by_sequence)
        self._config = config
        self._parse_config = parse_config
        self._missing_legacy_background = missing_legacy_background
        self._processing_started_ns = processing_started_ns
        self._monotonic_clock = monotonic_clock
        self._windows: tuple[AnalysisWindow, ...] = ()
        self._completed_chunks: list[VideoChunkParseResult] = []

    def start_stage(self, stage: ParsePipelineStage) -> None:
        target = _JOB_STAGE_BY_PIPELINE_STAGE[stage]
        if not self._jobs.update_progress(self._lease, stage=target):
            raise ParseJobLeaseLostError("parse job lease is no longer current")

    def ensure_active(self) -> None:
        self._jobs.assert_current_lease(self._lease)

    def load_completed_chunks(
        self,
        windows: Sequence[AnalysisWindow],
        *,
        models: VideoParseModels,
    ) -> Sequence[VideoChunkParseResult]:
        if models.transcript_mode != self._config.asr_mode:
            raise RuntimeError(
                "Parse pipeline described a different transcript mode"
            )

        self._windows = tuple(windows)
        completed: list[VideoChunkParseResult] = []
        context_before = _initial_context_snapshot()
        planned_sequences = {window.sequence_no for window in windows}
        extra_sequences = set(self._checkpoints) - planned_sequences
        if extra_sequences:
            raise ArtifactIntegrityError(
                "parse checkpoints contain VideoChunks outside the current plan"
            )

        gap_found = False
        for window in windows:
            checkpoint = self._checkpoints.get(window.sequence_no)
            parsed = (
                checkpoint is not None
                and _checkpoint_rank(checkpoint.stage)
                >= _checkpoint_rank(VideoChunkCheckpointStage.PARSED.value)
            )
            if not parsed:
                if checkpoint is not None and checkpoint.parse_artifact_uri is not None:
                    raise ArtifactIntegrityError(
                        "parse artifact pointer exists before the PARSED checkpoint stage"
                    )
                gap_found = True
                continue
            if checkpoint.parse_artifact_uri is None:
                raise ArtifactIntegrityError(
                    "PARSED checkpoint has no parse artifact pointer"
                )
            if gap_found:
                raise ArtifactIntegrityError(
                    "parsed VideoChunk checkpoints are not a continuous prefix"
                )
            try:
                path = self._artifacts.resolve(checkpoint.parse_artifact_uri)
            except (FileNotFoundError, ValueError) as exc:
                raise ArtifactIntegrityError(
                    "PARSED checkpoint points to a missing parse artifact"
                ) from exc
            artifact = VideoChunkCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            _validate_checkpoint_models(
                artifact,
                models=models,
                transcript_mode=self._config.asr_mode,
                primary_input_mode=(
                    self._parse_config.vision.input_mode
                    if self._parse_config is not None
                    else "video"
                ),
            )
            if not _analysis_windows_match(artifact.chunk.window, window):
                raise ArtifactIntegrityError(
                    f"parsed VideoChunk {window.sequence_no} uses another window"
                )
            chunk = artifact.chunk.model_copy(update={"window": window})
            _validate_algorithm_chunk(chunk, window, context_before)
            completed.append(chunk)
            context_before = chunk.context_after

        if self._missing_legacy_background and len(completed) != len(windows):
            raise RuntimeError(
                "旧解析任务缺少背景快照，无法确认已解析片段使用的背景；"
                "请保留原检查点并创建新的解析任务"
            )
        self._completed_chunks = completed
        return tuple(completed)

    def save_chunk(
        self,
        chunk: VideoChunkParseResult,
        *,
        models: VideoParseModels,
    ) -> None:
        sequence_no = chunk.window.sequence_no
        if not self._windows or sequence_no != len(self._completed_chunks):
            raise RuntimeError(
                f"Algorithm returned VideoChunk {sequence_no} out of sequence"
            )
        expected_window = self._windows[sequence_no]
        context_before = (
            self._completed_chunks[-1].context_after
            if self._completed_chunks
            else _initial_context_snapshot()
        )
        _validate_algorithm_chunk(chunk, expected_window, context_before)
        self.ensure_active()

        parse_path = (
            self._attempt_directory
            / "chunks"
            / f"chunk-{sequence_no:04d}.json"
        )
        _write_json_atomic(
            parse_path,
            VideoChunkCheckpoint(
                transcript_model=models.fallback.transcript_model,
                transcript_source=self._config.asr_mode,
                video_model=models.video_parser,
                chunk=chunk,
            ).model_dump(mode="json"),
        )
        self._save_checkpoint(
            requested_stage=VideoChunkCheckpointStage.PARSED,
            sequence_no=sequence_no,
            parse_artifact_uri=self._artifacts.uri_for_path(parse_path),
            processing_duration_ms=(
                self._lease.processing_duration_base_ms
                + max(
                    0,
                    self._monotonic_clock() - self._processing_started_ns,
                )
                // 1_000_000
            ),
        )
        self._completed_chunks.append(chunk)

    def save_parse_result(self, result: VideoParseResult) -> None:
        self.ensure_active()
        _write_json_atomic(
            self._attempt_directory / "pipeline.parse.json",
            result.model_dump(mode="json"),
        )

    def load_embedding(
        self,
        *,
        sequence_no: int,
        modality: Literal["text", "video"],
        input_sha256: str,
    ) -> ChunkEmbedding | None:
        stage, field_name = _embedding_checkpoint(modality)
        committed_uri = _committed_embedding_uri(
            self._checkpoints.get(sequence_no),
            stage=stage,
            field_name=field_name,
        )
        if committed_uri is None:
            return None

        video_chunk_id = derive_video_chunk_id(self._lease.video_id, sequence_no)
        try:
            path = self._artifacts.resolve(committed_uri)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("embedding artifact must be a JSON object")
            if (
                payload.get("video_chunk_id") != video_chunk_id
                or payload.get("input_sha256") != input_sha256
                or not isinstance(payload.get("embedding"), dict)
            ):
                raise ValueError("embedding artifact identity does not match")
            result = EmbeddingResult.model_validate(payload["embedding"])
            _validate_embedding(
                result,
                modality=modality,
                dimensions=self._config.embedding_dimensions,
            )
            return ChunkEmbedding(
                sequence_no=sequence_no,
                modality=modality,
                input_sha256=input_sha256,
                result=result,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise ArtifactIntegrityError(
                f"committed {modality} embedding artifact is invalid for "
                f"VideoChunk {sequence_no}"
            ) from exc

    def save_embedding(self, embedding: ChunkEmbedding) -> None:
        self.ensure_active()
        _validate_embedding(
            embedding.result,
            modality=embedding.modality,
            dimensions=self._config.embedding_dimensions,
        )
        stage, field_name = _embedding_checkpoint(embedding.modality)
        suffix = "text" if embedding.modality == "text" else "vision"
        path = (
            self._attempt_directory
            / "embeddings"
            / f"chunk-{embedding.sequence_no:04d}.{suffix}.json"
        )
        video_chunk_id = derive_video_chunk_id(
            self._lease.video_id,
            embedding.sequence_no,
        )
        _write_json_atomic(
            path,
            {
                "video_chunk_id": video_chunk_id,
                "input_sha256": embedding.input_sha256,
                "embedding": embedding.result.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                ),
            },
        )
        self._save_checkpoint(
            requested_stage=stage,
            sequence_no=embedding.sequence_no,
            **{field_name: self._artifacts.uri_for_path(path)},
        )

    def persist(self, result: ParsePipelineResult) -> Sequence[str]:
        self.ensure_active()
        prepared = prepare_completed_video_parse_import(
            result.parse_result,
            workspace_id=self._lease.workspace_id,
            series_id=self._lease.series_id,
            series_name=self._lease.series_name,
            series_description=self._lease.series_description,
            important_extra_data=self._lease.important_extra_data,
            character_bible=self._lease.character_bible,
            video_id=self._lease.video_id,
            episode_order=self._lease.episode_order,
            upload_sequence=self._lease.upload_sequence,
            original_filename=self._lease.original_filename,
            original_source_uri=self._lease.original_source_uri,
            content_hash=self._lease.content_hash,
            total_bytes=self._lease.total_bytes,
            pipeline_version=self._lease.pipeline_version,
            chunk_media_refs=self._chunk_media_refs,
            analysis_window_media_refs=self._analysis_window_media_refs,
            keyframes_by_sequence=self._keyframes_by_sequence,
            text_embeddings={
                sequence_no: _prepared_embedding(
                    embedding,
                    derive_video_chunk_id(self._lease.video_id, sequence_no),
                )
                for sequence_no, embedding in result.text_embeddings.items()
            },
            vision_embeddings={
                sequence_no: _prepared_embedding(
                    embedding,
                    derive_video_chunk_id(self._lease.video_id, sequence_no),
                )
                for sequence_no, embedding in result.vision_embeddings.items()
            },
        )
        report = self._videos.import_video_parse_for_job(prepared, self._lease)
        imported_ids = set(report.video_chunk_ids)
        checkpoint_ids = {
            item.video_chunk_id for item in self._checkpoints.values()
        }
        if imported_ids != checkpoint_ids:
            raise RuntimeError(
                "persisted VideoChunk set disagrees with parse checkpoints"
            )
        return tuple(report.video_chunk_ids)

    def _save_checkpoint(
        self,
        *,
        requested_stage: VideoChunkCheckpointStage,
        sequence_no: int,
        parse_artifact_uri: str | None = None,
        text_embedding_artifact_uri: str | None = None,
        vision_embedding_artifact_uri: str | None = None,
        processing_duration_ms: int | None = None,
    ) -> VideoChunkCheckpointState:
        current = self._checkpoints.get(sequence_no)
        stage = requested_stage
        if current is not None and _checkpoint_rank(current.stage) > _checkpoint_rank(
            requested_stage.value
        ):
            stage = VideoChunkCheckpointStage(current.stage)
        saved = self._jobs.save_checkpoint(
            self._lease,
            VideoChunkCheckpointState(
                job_id=self._lease.job_id,
                sequence_no=sequence_no,
                video_chunk_id=derive_video_chunk_id(
                    self._lease.video_id,
                    sequence_no,
                ),
                stage=stage.value,
                chunk_start_seconds=(current.chunk_start_seconds if current is not None else None),
                chunk_end_seconds=(current.chunk_end_seconds if current is not None else None),
                parse_artifact_uri=parse_artifact_uri,
                text_embedding_artifact_uri=text_embedding_artifact_uri,
                vision_embedding_artifact_uri=vision_embedding_artifact_uri,
            ),
            processing_duration_ms=processing_duration_ms,
        )
        self._checkpoints[sequence_no] = saved
        return saved

def _embedding_checkpoint(
    modality: Literal["text", "video"],
) -> tuple[VideoChunkCheckpointStage, str]:
    if modality == "text":
        return (
            VideoChunkCheckpointStage.TEXT_EMBEDDED,
            "text_embedding_artifact_uri",
        )
    return (
        VideoChunkCheckpointStage.VISION_EMBEDDED,
        "vision_embedding_artifact_uri",
    )


def _committed_embedding_uri(
    checkpoint: VideoChunkCheckpointState | None,
    *,
    stage: VideoChunkCheckpointStage,
    field_name: str,
) -> str | None:
    if checkpoint is None:
        return None
    uri = getattr(checkpoint, field_name)
    committed = _checkpoint_rank(checkpoint.stage) >= _checkpoint_rank(stage.value)
    if committed and uri is None:
        raise ArtifactIntegrityError(
            f"{stage.value} checkpoint has no committed embedding artifact"
        )
    if not committed and uri is not None:
        raise ArtifactIntegrityError(
            "embedding artifact pointer exists before its checkpoint stage"
        )
    return uri


def _initial_context_snapshot() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="context-initial",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )


def _validate_algorithm_chunk(
    chunk: VideoChunkParseResult,
    window: AnalysisWindow,
    context_before: ContextSnapshot,
) -> None:
    if chunk.window != window:
        raise RuntimeError(
            f"Algorithm returned another window for VideoChunk {window.sequence_no}"
        )
    if chunk.context_before != context_before:
        raise RuntimeError(
            f"Algorithm broke the context chain at VideoChunk {window.sequence_no}"
        )
    if chunk.context_after.after_chunk_sequence_no != window.sequence_no:
        raise RuntimeError(
            f"Algorithm returned an invalid context for VideoChunk {window.sequence_no}"
        )


def _validate_checkpoint_models(
    checkpoint: VideoChunkCheckpoint,
    *,
    models: VideoParseModels,
    transcript_mode: Literal["lite", "none"],
    primary_input_mode: Literal["video", "ordered_frames"] = "video",
) -> None:
    if checkpoint.transcript_model != models.fallback.transcript_model:
        raise ArtifactIntegrityError("transcript model changed for parsed VideoChunk")
    if checkpoint.transcript_source != transcript_mode:
        raise ArtifactIntegrityError("transcript mode changed for parsed VideoChunk")
    if checkpoint.video_model != models.video_parser:
        raise ArtifactIntegrityError("video model changed for parsed VideoChunk")

    provenance = checkpoint.chunk.video_parse
    if provenance is None:
        return
    if provenance.provider == "doubao":
        if (
            provenance.model != models.video_parser
            or provenance.input_mode != primary_input_mode
        ):
            raise ArtifactIntegrityError(
                "primary parser changed for parsed VideoChunk"
            )
        return

    fallback = models.fallback
    expected = {
        "provider": fallback.provider,
        "model": fallback.visual_model,
        "input_mode": fallback.input_mode,
        "frame_fps": fallback.frame_fps,
        "group_duration_seconds": fallback.group_duration_seconds,
        "max_frames_per_group": fallback.max_frames_per_group,
        "fallback_transcript_model": (
            fallback.transcript_model if transcript_mode == "lite" else None
        ),
        "visual_model": fallback.visual_model,
        "refiner_model": fallback.refiner_model,
    }
    if not fallback.enabled or any(
        getattr(provenance, name) != value for name, value in expected.items()
    ):
        raise ArtifactIntegrityError("fallback parser changed for parsed VideoChunk")


def _analysis_windows_match(
    persisted: AnalysisWindow,
    current: AnalysisWindow,
) -> bool:
    if persisted == current:
        return True
    persisted_data = persisted.model_dump()
    current_data = current.model_dump()
    persisted_data.pop("media_path")
    current_data.pop("media_path")
    return persisted_data == current_data


def _validate_embedding(
    result: EmbeddingResult,
    *,
    modality: str,
    dimensions: int,
) -> None:
    if result.modality != modality:
        raise RuntimeError(
            f"embedding gateway returned {result.modality!r} for {modality!r}"
        )
    if len(result.vector) != dimensions:
        raise RuntimeError(
            "embedding gateway returned an unexpected vector dimension"
        )
    if any(not math.isfinite(float(value)) for value in result.vector):
        raise RuntimeError("embedding gateway returned a non-finite vector")


def _prepared_embedding(
    embedding: ChunkEmbedding,
    video_chunk_id: str,
) -> PreparedEmbedding:
    return PreparedEmbedding(
        vector=[float(value) for value in embedding.result.vector],
        metadata={
            "model": embedding.result.model,
            "modality": embedding.modality,
            "dimensions": len(embedding.result.vector),
            "input_sha256": embedding.input_sha256,
            "video_chunk_id": video_chunk_id,
        },
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _checkpoint_rank(stage: str) -> int:
    return _CHECKPOINT_ORDER[VideoChunkCheckpointStage(stage)]


_JOB_STAGE_BY_PIPELINE_STAGE = {
    "transcribe": ParseJobStage.PARSING,
    "understand": ParseJobStage.PARSING,
    "text_embedding": ParseJobStage.TEXT_EMBEDDING,
    "vision_embedding": ParseJobStage.VISION_EMBEDDING,
    "persist": ParseJobStage.PERSISTING,
}

_CHECKPOINT_ORDER = {
    stage: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
