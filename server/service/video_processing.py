from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from engine.parse.analysis_windows import materialize_canonical_chunks
from engine.parse.runtime import EngineVideoParseRuntime
from engine.parse.schemas import AnalysisWindow, VideoChunkParseResult
from engine.retrieval.video_retrieval import (
    VideoChunkRetrievalDocument,
    build_video_chunk_retrieval_documents,
)
from gateway.schemas import EmbeddingResult

from ..config.video_processing import VideoProcessingConfig
from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from ..model.dto.parseDTO import PreparedEmbedding
from ..model.job import (
    ParseJobStage,
    VideoChunkCheckpointStage,
)
from ..repository.job import ParseJobLeaseLostError, ParseJobRepository
from ..repository.video import VideoRepository
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore
from .index_sync import IndexSyncService
from .parse_import import prepare_completed_video_parse_import

# Stage weights describe work completed, not elapsed time or an ETA.
_PREPARING_PROGRESS = 2
_SPLIT_READY_PROGRESS = 10
_PARSED_PROGRESS = 60
_TEXT_EMBEDDED_PROGRESS = 75
_VISION_EMBEDDED_PROGRESS = 90
_PERSISTING_PROGRESS = 92


class MultimodalEmbedder(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...


class VideoProcessingService:
    """Turn one claimed original Video into searchable MySQL/Qdrant facts."""

    def __init__(
        self,
        jobs: ParseJobRepository,
        videos: VideoRepository,
        index_sync: IndexSyncService,
        artifacts: ArtifactStore,
        parser: EngineVideoParseRuntime,
        embedder: MultimodalEmbedder,
        *,
        config: VideoProcessingConfig | None = None,
    ) -> None:
        self._jobs = jobs
        self._videos = videos
        self._index_sync = index_sync
        self._artifacts = artifacts
        self._parser = parser
        self._embedder = embedder
        self._config = config or VideoProcessingConfig()

    def process(self, lease: ParseJobLease) -> list[str]:
        source_video = self._verified_source(lease)
        work_directory = self._artifacts.work_directory(lease.job_id)
        checkpoints = {
            item.sequence_no: item
            for item in self._jobs.list_checkpoints(lease.job_id)
        }
        chunk_paths: dict[int, Path] = {}
        chunk_media_refs: dict[int, str] = {}
        analysis_media_refs: dict[int, str] = {}
        window_count = 0

        self._update_progress(
            lease,
            stage=ParseJobStage.PREPARING,
            progress=_PREPARING_PROGRESS,
        )

        def windows_ready(windows: list[AnalysisWindow]) -> None:
            nonlocal window_count
            window_count = len(windows)
            for window in windows:
                sequence_no = window.sequence_no
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.PREPARED,
                    sequence_no=sequence_no,
                )
            canonical_paths = materialize_canonical_chunks(
                windows,
                output_dir=work_directory / "canonical-chunks",
                max_height=self._config.canonical_max_height,
            )
            for window, canonical_path in zip(
                windows,
                canonical_paths,
                strict=True,
            ):
                sequence_no = window.sequence_no
                chunk_paths[sequence_no] = canonical_path
                chunk_artifact = self._artifacts.publish_video_artifact(
                    canonical_path,
                    series_id=lease.series_id,
                    video_id=lease.video_id,
                    category="chunks",
                    filename=canonical_path.name,
                )
                analysis_artifact = self._artifacts.publish_video_artifact(
                    window.media_path,
                    series_id=lease.series_id,
                    video_id=lease.video_id,
                    category="analysis-windows",
                    filename=window.media_path.name,
                )
                chunk_media_refs[sequence_no] = chunk_artifact.uri
                analysis_media_refs[sequence_no] = analysis_artifact.uri
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.SPLIT_READY,
                    sequence_no=sequence_no,
                    chunk_media_uri=chunk_artifact.uri,
                    analysis_window_media_uri=analysis_artifact.uri,
                )
            self._update_progress(
                lease,
                stage=ParseJobStage.SPLIT_READY,
                progress=_SPLIT_READY_PROGRESS,
            )

        def chunk_complete(chunk: VideoChunkParseResult, path: Path) -> None:
            sequence_no = chunk.window.sequence_no
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints.get(sequence_no),
                requested_stage=VideoChunkCheckpointStage.PARSED,
                sequence_no=sequence_no,
                parse_artifact_uri=self._artifacts.uri_for_path(path),
            )
            parsed_count = sequence_no + 1
            progress = _SPLIT_READY_PROGRESS + round(
                (_PARSED_PROGRESS - _SPLIT_READY_PROGRESS)
                * parsed_count / max(window_count, 1)
            )
            self._update_progress(
                lease,
                stage=ParseJobStage.PARSING,
                progress=min(progress, _PARSED_PROGRESS),
            )

        result = self._parser.run(
            source_video,
            duration_ms=lease.duration_ms,
            output_dir=work_directory,
            series_bible=_series_bible(
                lease.character_bible,
                lease.important_extra_data,
            ),
            on_windows_ready=windows_ready,
            on_chunk_complete=chunk_complete,
        )
        if not chunk_paths:
            raise RuntimeError("Engine returned without preparing canonical VideoChunks")

        # A crash can happen after the JSON checkpoint is committed but before
        # its MySQL pointer is saved. Reconcile every completed parse here.
        for chunk in result.chunks:
            sequence_no = chunk.window.sequence_no
            parse_path = (
                work_directory / "chunks" / f"chunk-{sequence_no:04d}.json"
            )
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints.get(sequence_no),
                requested_stage=VideoChunkCheckpointStage.PARSED,
                sequence_no=sequence_no,
                parse_artifact_uri=self._artifacts.uri_for_path(parse_path),
            )
        self._update_progress(
            lease,
            stage=ParseJobStage.PARSING,
            progress=_PARSED_PROGRESS,
        )

        documents = {
            item.sequence_no: item
            for item in build_video_chunk_retrieval_documents(result)
        }
        text_embeddings = self._embed_text_chunks(
            lease,
            work_directory,
            documents,
            checkpoints,
        )
        vision_embeddings = self._embed_vision_chunks(
            lease,
            work_directory,
            chunk_media_refs,
            checkpoints,
        )
        self._update_progress(
            lease,
            stage=ParseJobStage.PERSISTING,
            progress=_PERSISTING_PROGRESS,
        )
        prepared = prepare_completed_video_parse_import(
            result,
            workspace_id=lease.workspace_id,
            series_id=lease.series_id,
            series_name=lease.series_name,
            series_description=lease.series_description,
            important_extra_data=lease.important_extra_data,
            character_bible=lease.character_bible,
            video_id=lease.video_id,
            episode_order=lease.episode_order,
            upload_sequence=lease.upload_sequence,
            original_filename=lease.original_filename,
            original_source_uri=lease.original_source_uri,
            content_hash=lease.content_hash,
            total_bytes=lease.total_bytes,
            pipeline_version=lease.pipeline_version,
            chunk_media_refs=chunk_media_refs,
            analysis_window_media_refs=analysis_media_refs,
            text_embeddings=text_embeddings,
            vision_embeddings=vision_embeddings,
        )
        report = self._videos.import_video_parse_for_job(prepared, lease)
        self._jobs.assert_current_lease(lease)
        self._index_sync.sync_video_chunks(report.video_chunk_ids)
        self._jobs.assert_current_lease(lease)
        for sequence_no in sorted(checkpoints):
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints[sequence_no],
                requested_stage=VideoChunkCheckpointStage.INDEXED,
                sequence_no=sequence_no,
            )
        return report.video_chunk_ids

    def _embed_text_chunks(
        self,
        lease: ParseJobLease,
        work_directory: Path,
        documents: Mapping[int, VideoChunkRetrievalDocument],
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> dict[int, PreparedEmbedding]:
        embeddings: dict[int, PreparedEmbedding] = {}
        total = len(documents)
        for completed, sequence_no in enumerate(sorted(documents), start=1):
            document = documents[sequence_no]
            input_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
            path = (
                work_directory
                / "embeddings"
                / f"chunk-{sequence_no:04d}.text.json"
            )
            embedding = self._load_or_embed(
                path,
                video_chunk_id=derive_video_chunk_id(lease.video_id, sequence_no),
                modality="text",
                input_hash=input_hash,
                create=lambda: self._embedder.embed_text(document.text),
            )
            embeddings[sequence_no] = embedding
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints.get(sequence_no),
                requested_stage=VideoChunkCheckpointStage.TEXT_EMBEDDED,
                sequence_no=sequence_no,
                text_embedding_artifact_uri=self._artifacts.uri_for_path(path),
            )
            self._update_progress(
                lease,
                stage=ParseJobStage.TEXT_EMBEDDING,
                progress=_PARSED_PROGRESS + round(
                    (_TEXT_EMBEDDED_PROGRESS - _PARSED_PROGRESS) * completed / total
                ),
            )
        return embeddings

    def _embed_vision_chunks(
        self,
        lease: ParseJobLease,
        work_directory: Path,
        chunk_media_refs: Mapping[int, str],
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> dict[int, PreparedEmbedding]:
        embeddings: dict[int, PreparedEmbedding] = {}
        total = len(chunk_media_refs)
        for completed, sequence_no in enumerate(sorted(chunk_media_refs), start=1):
            media_uri = chunk_media_refs[sequence_no]
            media = self._artifacts.resolve(media_uri)
            input_hash = self._artifacts.fingerprint(media_uri).sha256
            path = (
                work_directory
                / "embeddings"
                / f"chunk-{sequence_no:04d}.vision.json"
            )
            embedding = self._load_or_embed(
                path,
                video_chunk_id=derive_video_chunk_id(lease.video_id, sequence_no),
                modality="video",
                input_hash=input_hash,
                create=lambda media=media: self._embedder.embed_video(media),
            )
            embeddings[sequence_no] = embedding
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints.get(sequence_no),
                requested_stage=VideoChunkCheckpointStage.VISION_EMBEDDED,
                sequence_no=sequence_no,
                vision_embedding_artifact_uri=self._artifacts.uri_for_path(path),
            )
            self._update_progress(
                lease,
                stage=ParseJobStage.VISION_EMBEDDING,
                progress=_TEXT_EMBEDDED_PROGRESS + round(
                    (_VISION_EMBEDDED_PROGRESS - _TEXT_EMBEDDED_PROGRESS)
                    * completed / total
                ),
            )
        return embeddings

    def _load_or_embed(
        self,
        path: Path,
        *,
        video_chunk_id: str,
        modality: str,
        input_hash: str,
        create: Callable[[], EmbeddingResult],
    ) -> PreparedEmbedding:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("video_chunk_id") == video_chunk_id
                and payload.get("input_sha256") == input_hash
                and isinstance(payload.get("embedding"), dict)
            ):
                result = EmbeddingResult.model_validate(payload["embedding"])
                self._validate_embedding(result, modality)
                return _prepared_embedding(result, input_hash, video_chunk_id)

        result = create()
        self._validate_embedding(result, modality)
        _write_json_atomic(
            path,
            {
                "video_chunk_id": video_chunk_id,
                "input_sha256": input_hash,
                "embedding": result.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                ),
            },
        )
        return _prepared_embedding(result, input_hash, video_chunk_id)

    def _validate_embedding(self, result: EmbeddingResult, modality: str) -> None:
        if result.modality != modality:
            raise RuntimeError(
                f"embedding gateway returned {result.modality!r} for {modality!r}"
            )
        if len(result.vector) != self._config.embedding_dimensions:
            raise RuntimeError(
                "embedding gateway returned an unexpected vector dimension"
            )
        if any(not math.isfinite(float(value)) for value in result.vector):
            raise RuntimeError("embedding gateway returned a non-finite vector")

    def _verified_source(self, lease: ParseJobLease) -> Path:
        fingerprint = self._artifacts.fingerprint(lease.original_source_uri)
        if (
            fingerprint.size_bytes != lease.total_bytes
            or fingerprint.sha256 != lease.content_hash
        ):
            raise ArtifactIntegrityError(
                "original Video bytes disagree with the completed upload"
            )
        return self._artifacts.resolve(lease.original_source_uri)

    def _update_progress(
        self,
        lease: ParseJobLease,
        *,
        stage: ParseJobStage,
        progress: int,
    ) -> None:
        if not self._jobs.update_progress(
            lease,
            stage=stage,
            progress=progress,
        ):
            raise ParseJobLeaseLostError("parse job lease is no longer current")

    def _save_checkpoint(
        self,
        lease: ParseJobLease,
        current: VideoChunkCheckpointState | None,
        *,
        requested_stage: VideoChunkCheckpointStage,
        sequence_no: int,
        chunk_media_uri: str | None = None,
        analysis_window_media_uri: str | None = None,
        parse_artifact_uri: str | None = None,
        text_embedding_artifact_uri: str | None = None,
        vision_embedding_artifact_uri: str | None = None,
    ) -> VideoChunkCheckpointState:
        stage = requested_stage
        if current is not None and _checkpoint_rank(current.stage) > _checkpoint_rank(
            requested_stage.value
        ):
            stage = VideoChunkCheckpointStage(current.stage)
        return self._jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=lease.job_id,
                sequence_no=sequence_no,
                video_chunk_id=derive_video_chunk_id(lease.video_id, sequence_no),
                stage=stage.value,
                chunk_media_uri=chunk_media_uri,
                analysis_window_media_uri=analysis_window_media_uri,
                parse_artifact_uri=parse_artifact_uri,
                text_embedding_artifact_uri=text_embedding_artifact_uri,
                vision_embedding_artifact_uri=vision_embedding_artifact_uri,
            ),
        )


def _prepared_embedding(
    result: EmbeddingResult,
    input_hash: str,
    video_chunk_id: str,
) -> PreparedEmbedding:
    return PreparedEmbedding(
        vector=[float(value) for value in result.vector],
        metadata={
            "model": result.model,
            "modality": result.modality,
            "dimensions": len(result.vector),
            "input_sha256": input_hash,
            "video_chunk_id": video_chunk_id,
        },
    )


def _series_bible(
    character_bible: list[dict],
    important_extra_data: dict | None,
) -> str | None:
    source_text = None
    if important_extra_data is not None:
        candidate = important_extra_data.get("seriesBible")
        if isinstance(candidate, str) and candidate.strip():
            source_text = candidate.strip()
    if not character_bible and source_text is None:
        return None
    structured = json.dumps(
        {"characters": character_bible},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if source_text is None:
        return structured
    if not character_bible:
        return source_text
    return f"{source_text}\n\nStructured character data:\n{structured}"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _checkpoint_rank(stage: str) -> int:
    return _CHECKPOINT_ORDER[VideoChunkCheckpointStage(stage)]


_CHECKPOINT_ORDER = {
    stage: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
