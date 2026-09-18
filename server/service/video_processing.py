from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from contextvars import copy_context
from pathlib import Path
from threading import Condition, Event
from typing import Literal, Protocol

from engine.parse.analysis_windows import (
    materialize_analysis_window,
    materialize_canonical_chunk,
    plan_analysis_windows,
)
from engine.parse.background import (
    BackgroundImage,
    extract_background_image_ids,
    prepare_background_text,
)
from engine.parse.frame_sampler import extract_video_frames
from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    TranscriptAnalysis,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseModels,
    VideoParseResult,
)
from engine.parse.usage import parse_chunk_scope
from engine.retrieval.video_retrieval import (
    VideoChunkRetrievalDocument,
    build_video_chunk_retrieval_documents,
)
from gateway.schemas import EmbeddingResult

from ..config.video_processing import VideoProcessingConfig
from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from ..model.dto.parseDTO import PreparedEmbedding
from ..model.job import ParseJobStage, VideoChunkCheckpointStage
from ..model.job_timing import VideoChunkTimingStage
from ..mq.contracts import ParseJobLeaseLostError
from ..repository.job import ParseJobRepository
from ..repository.video import VideoRepository
from ..schema import VideoChunkKeyframeReference
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore
from ..utils.files import write_json_once
from .index_sync import IndexSyncService
from .parse_import import prepare_completed_video_parse_import
from .parse_usage import ParseUsageService
from .parse_timing import ParseChunkStageTimer


class MultimodalEmbedder(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...


class VideoParseRuntime(Protocol):
    def describe_models(
        self,
        transcript_mode: Literal["lite", "none"],
    ) -> VideoParseModels: ...

    def transcribe_chunk(self, video_chunk: AnalysisWindow) -> TranscriptAnalysis: ...

    def parse_chunk(
        self,
        compact: ContextSnapshot,
        video_chunk: AnalysisWindow,
        *,
        candidate_transcript: TranscriptAnalysis | None = None,
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoChunkParseResult: ...


class VideoProcessingService:
    """Turn one claimed original Video into searchable MySQL/Qdrant facts."""

    def __init__(
        self,
        jobs: ParseJobRepository,
        videos: VideoRepository,
        index_sync: IndexSyncService,
        artifacts: ArtifactStore,
        runtime: VideoParseRuntime,
        embedder: MultimodalEmbedder,
        *,
        config: VideoProcessingConfig | None = None,
        parse_usage: ParseUsageService | None = None,
        stage_timer: ParseChunkStageTimer | None = None,
    ) -> None:
        self._jobs = jobs
        self._videos = videos
        self._index_sync = index_sync
        self._artifacts = artifacts
        self._runtime = runtime
        self._embedder = embedder
        self._config = config or VideoProcessingConfig()
        self._parse_usage = parse_usage
        self._stage_timer = stage_timer

    def process(self, lease: ParseJobLease) -> list[str]:
        capture = (
            self._parse_usage.capture(lease)
            if self._parse_usage
            else nullcontext()
        )
        with capture:
            return self._process(lease)

    def _measure_stage(
        self,
        lease: ParseJobLease,
        sequence_no: int,
        stage: VideoChunkTimingStage,
    ) -> AbstractContextManager[None]:
        """Use a no-op context in unit/legacy deployments without timing."""

        if self._stage_timer is None:
            return nullcontext()
        return self._stage_timer.measure(
            lease,
            sequence_no=sequence_no,
            stage=stage,
        )

    def _process(self, lease: ParseJobLease) -> list[str]:
        source_video = self._verified_source(lease)
        work_directory = self._artifacts.work_directory(lease.job_id)
        attempt_directory = _attempt_directory(work_directory, lease.lease_version)
        checkpoints = {
            item.sequence_no: item
            for item in self._jobs.list_checkpoints(lease.job_id)
        }
        (
            series_bible,
            background_images,
            missing_legacy_background,
        ) = self._parse_background(lease, work_directory, checkpoints)
        models = self._runtime.describe_models(self._config.asr_mode)
        if models.transcript_mode != self._config.asr_mode:
            raise RuntimeError("Parse runtime described a different transcript mode")

        chunk_media_refs: dict[int, str] = {}
        analysis_media_refs: dict[int, str] = {}
        keyframes_by_sequence: dict[
            int,
            tuple[VideoChunkKeyframeReference, ...],
        ] = {}
        self._advance_stage(lease, ParseJobStage.PREPARING)
        windows = plan_analysis_windows(
            source_video,
            duration_ms=lease.duration_ms,
            output_dir=attempt_directory / "windows",
            chunk_duration_ms=self._config.chunk_duration_ms,
            context_overlap_ms=self._config.context_overlap_ms,
        )
        for window in windows:
            sequence_no = window.sequence_no
            checkpoints[sequence_no] = self._save_checkpoint(
                lease,
                checkpoints.get(sequence_no),
                requested_stage=VideoChunkCheckpointStage.PREPARED,
                sequence_no=sequence_no,
                chunk_start_ms=window.chunk_start_ms,
                chunk_end_ms=window.chunk_end_ms,
            )
        published_filename_prefix = (
            f"{lease.job_id}-lease-{lease.lease_version:08d}"
        )
        # Validate the full planned topology before single-item materializers
        # are used for accurate segment timing.
        if [window.sequence_no for window in windows] != list(range(len(windows))):
            raise RuntimeError("analysis windows must have contiguous sequence numbers")
        for window in windows:
            sequence_no = window.sequence_no
            with self._measure_stage(lease, sequence_no, VideoChunkTimingStage.SPLIT):
                materialize_analysis_window(
                    window,
                    max_height=self._config.analysis_max_height,
                )
                canonical_path = materialize_canonical_chunk(
                    window,
                    output_dir=attempt_directory / "canonical-chunks",
                    max_height=self._config.canonical_max_height,
                )
                self._jobs.assert_current_lease(lease)
                [(relative_at_ms, keyframe_path)] = extract_video_frames(
                    canonical_path,
                    [0],
                    output_dir=(
                        attempt_directory
                        / "keyframes"
                        / f"chunk-{sequence_no:04d}"
                    ),
                    max_height=self._config.canonical_max_height,
                )
                if not (
                    0
                    <= relative_at_ms
                    < window.chunk_end_ms - window.chunk_start_ms
                ):
                    raise RuntimeError(
                        "canonical keyframe position is outside its VideoChunk"
                    )
                chunk_artifact = self._artifacts.publish_video_artifact(
                    canonical_path,
                    series_id=lease.series_id,
                    video_id=lease.video_id,
                    category="chunks",
                    filename=(
                        f"{published_filename_prefix}-{canonical_path.name}"
                    ),
                )
                analysis_artifact = self._artifacts.publish_video_artifact(
                    window.media_path,
                    series_id=lease.series_id,
                    video_id=lease.video_id,
                    category="analysis-windows",
                    filename=(
                        f"{published_filename_prefix}-{window.media_path.name}"
                    ),
                )
                keyframe_artifact = self._artifacts.publish_video_artifact(
                    keyframe_path,
                    series_id=lease.series_id,
                    video_id=lease.video_id,
                    category="keyframes",
                    filename=(
                        f"{published_filename_prefix}-chunk-{sequence_no:04d}-"
                        f"{keyframe_path.name}"
                    ),
                )
                chunk_media_refs[sequence_no] = chunk_artifact.uri
                analysis_media_refs[sequence_no] = analysis_artifact.uri
                keyframes_by_sequence[sequence_no] = (
                    VideoChunkKeyframeReference(
                        uri=keyframe_artifact.uri,
                        at_ms=window.chunk_start_ms + relative_at_ms,
                    ),
                )
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.SPLIT_READY,
                    sequence_no=sequence_no,
                    chunk_media_uri=chunk_artifact.uri,
                    analysis_window_media_uri=analysis_artifact.uri,
                )

        self._advance_stage(lease, ParseJobStage.PARSING)
        chunks = self._load_completed_prefix(
            windows,
            checkpoints,
            models=models,
        )
        if missing_legacy_background and len(chunks) != len(windows):
            raise RuntimeError(
                "旧解析任务缺少背景快照，无法确认已解析片段使用的背景；"
                "请保留原检查点并创建新的解析任务"
            )

        context_before = (
            chunks[-1].context_after if chunks else _initial_context_snapshot()
        )
        pending_windows = windows[len(chunks) :]
        candidate_transcripts = self._ordered_candidate_transcripts(
            lease,
            pending_windows,
        )
        try:
            for window, candidate_transcript in candidate_transcripts:
                with self._measure_stage(
                    lease, window.sequence_no, VideoChunkTimingStage.PARSE,
                ):
                    chunk = self._runtime.parse_chunk(
                        context_before,
                        window,
                        candidate_transcript=candidate_transcript,
                        series_bible=series_bible,
                        background_images=background_images,
                    )
                # Model calls cannot be force-cancelled reliably. Their output only
                # becomes durable after the current lease is checked again.
                self._jobs.assert_current_lease(lease)
                _validate_algorithm_chunk(chunk, window, context_before)

                sequence_no = window.sequence_no
                parse_path = (
                    attempt_directory
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
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.PARSED,
                    sequence_no=sequence_no,
                    parse_artifact_uri=self._artifacts.uri_for_path(parse_path),
                )
                chunks.append(chunk)
                context_before = chunk.context_after
        finally:
            candidate_transcripts.close()

        result = VideoParseResult(
            models=models,
            source_video=source_video,
            duration_ms=lease.duration_ms,
            chunk_duration_ms=self._config.chunk_duration_ms,
            context_overlap_ms=self._config.context_overlap_ms,
            chunks=chunks,
            events=sorted(
                (event for chunk in chunks for event in chunk.events),
                key=lambda event: (event.start_ms, event.end_ms),
            ),
        )
        self._jobs.assert_current_lease(lease)
        _write_json_atomic(
            attempt_directory / "pipeline.parse.json",
            result.model_dump(mode="json"),
        )
        documents = {
            item.sequence_no: item
            for item in build_video_chunk_retrieval_documents(result)
        }
        self._advance_stage(lease, ParseJobStage.TEXT_EMBEDDING)
        text_embeddings = self._embed_text_chunks(
            lease,
            attempt_directory,
            documents,
            checkpoints,
        )
        self._advance_stage(lease, ParseJobStage.VISION_EMBEDDING)
        vision_embeddings = self._embed_vision_chunks(
            lease,
            attempt_directory,
            chunk_media_refs,
            checkpoints,
        )
        self._advance_stage(lease, ParseJobStage.PERSISTING)
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
            keyframes_by_sequence=keyframes_by_sequence,
            text_embeddings=text_embeddings,
            vision_embeddings=vision_embeddings,
        )
        report = self._videos.import_video_parse_for_job(prepared, lease)
        imported_ids = set(report.video_chunk_ids)
        checkpoint_ids = {item.video_chunk_id for item in checkpoints.values()}
        if imported_ids != checkpoint_ids:
            raise RuntimeError(
                "persisted VideoChunk set disagrees with parse checkpoints"
            )
        for sequence_no in sorted(checkpoints):
            video_chunk_id = checkpoints[sequence_no].video_chunk_id
            # A fully indexed checkpoint is durable work from a prior attempt;
            # do not manufacture another timing row while recovering it.
            if _checkpoint_rank(checkpoints[sequence_no].stage) < _checkpoint_rank(
                VideoChunkCheckpointStage.INDEXED.value
            ):
                with self._measure_stage(
                    lease, sequence_no, VideoChunkTimingStage.INDEX,
                ):
                    self._jobs.assert_current_lease(lease)
                    self._index_sync.sync_video_chunks([video_chunk_id])
                    self._jobs.assert_current_lease(lease)
                    checkpoints[sequence_no] = self._save_checkpoint(
                        lease, checkpoints[sequence_no],
                        requested_stage=VideoChunkCheckpointStage.INDEXED,
                        sequence_no=sequence_no,
                    )
        return report.video_chunk_ids

    def _load_completed_prefix(
        self,
        windows: Sequence[AnalysisWindow],
        checkpoints: Mapping[int, VideoChunkCheckpointState],
        *,
        models: VideoParseModels,
    ) -> list[VideoChunkParseResult]:
        completed: list[VideoChunkParseResult] = []
        context_before = _initial_context_snapshot()
        planned_sequences = {window.sequence_no for window in windows}
        extra_sequences = set(checkpoints) - planned_sequences
        if extra_sequences:
            raise ArtifactIntegrityError(
                "parse checkpoints contain VideoChunks outside the current plan"
            )
        gap_found = False
        for window in windows:
            checkpoint = checkpoints.get(window.sequence_no)
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
            )
            if not _analysis_windows_match(artifact.chunk.window, window):
                raise ArtifactIntegrityError(
                    f"parsed VideoChunk {window.sequence_no} uses another window"
                )
            chunk = artifact.chunk.model_copy(update={"window": window})
            _validate_algorithm_chunk(chunk, window, context_before)
            completed.append(chunk)
            context_before = chunk.context_after
        return completed

    def _ordered_candidate_transcripts(
        self,
        lease: ParseJobLease,
        windows: Sequence[AnalysisWindow],
    ) -> Iterator[tuple[AnalysisWindow, TranscriptAnalysis | None]]:
        if self._config.asr_mode == "none":
            for window in windows:
                yield window, TranscriptAnalysis(transcript=[])
            return
        if not windows:
            return

        root_context = copy_context()
        window_iterator = iter(windows)
        ready = Condition()
        stopped = Event()
        results: dict[int, TranscriptAnalysis | BaseException] = {}
        outstanding = 0
        maximum_outstanding = self._config.asr_concurrency + 1

        def transcribe_pending_windows() -> None:
            nonlocal outstanding
            while not stopped.is_set():
                with ready:
                    ready.wait_for(
                        lambda: stopped.is_set()
                        or outstanding < maximum_outstanding
                    )
                    if stopped.is_set():
                        return
                    try:
                        window = next(window_iterator)
                    except StopIteration:
                        return
                    outstanding += 1
                try:
                    def transcribe_one() -> TranscriptAnalysis:
                        with self._measure_stage(
                            lease, window.sequence_no, VideoChunkTimingStage.ASR,
                        ):
                            return self._runtime.transcribe_chunk(window)

                    result: TranscriptAnalysis | BaseException = (
                        root_context.copy().run(transcribe_one)
                    )
                except BaseException as exc:
                    result = exc
                    stopped.set()
                with ready:
                    results[window.sequence_no] = result
                    ready.notify_all()

        executor = ThreadPoolExecutor(
            max_workers=self._config.asr_concurrency,
            thread_name_prefix="parse-asr",
        )
        workers = []
        try:
            workers = [
                executor.submit(transcribe_pending_windows)
                for _ in range(min(self._config.asr_concurrency, len(windows)))
            ]
            for window in windows:
                with ready:
                    ready.wait_for(lambda: window.sequence_no in results)
                    transcript = results.pop(window.sequence_no)
                    outstanding -= 1
                    ready.notify_all()
                if isinstance(transcript, BaseException):
                    raise transcript
                self._jobs.assert_current_lease(lease)
                yield window, transcript
        finally:
            with ready:
                stopped.set()
                ready.notify_all()
            for worker in workers:
                worker.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def _parse_background(
        self,
        lease: ParseJobLease,
        work_directory: Path,
        checkpoints: Mapping[int, VideoChunkCheckpointState],
    ) -> tuple[str | None, list[BackgroundImage], bool]:
        snapshot_path = work_directory / "background.snapshot.json"
        if not snapshot_path.is_file():
            legacy_parse = any(
                _checkpoint_rank(item.stage)
                >= _checkpoint_rank(VideoChunkCheckpointStage.PARSED.value)
                for item in checkpoints.values()
            )
            # 已完成的旧片段仍可恢复入库；部分解析不能冒用当前背景续跑。
            if legacy_parse:
                return None, [], True
            text = prepare_background_text(lease.character_bible, lease.important_extra_data)
            images = []
            for image_id in extract_background_image_ids(text or ""):
                path = self._artifacts.resolve_series_image(
                    image_id, series_id=lease.series_id, variant="model",
                )
                images.append(
                    {
                        "image_id": image_id,
                        "sha256": self._artifacts.fingerprint(
                            self._artifacts.uri_for_path(path)
                        ).sha256,
                    }
                )
            self._jobs.assert_current_lease(lease)
            write_json_once(
                snapshot_path,
                {
                    "version": 1,
                    "job_id": lease.job_id,
                    "workspace_id": lease.workspace_id,
                    "series_id": lease.series_id,
                    "content_hash": lease.content_hash,
                    "series_bible": text,
                    "images": images,
                },
            )

        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or any(
            snapshot.get(key) != expected for key, expected in {
                "version": 1, "job_id": lease.job_id, "workspace_id": lease.workspace_id,
                "series_id": lease.series_id, "content_hash": lease.content_hash,
            }.items()
        ):
            raise ArtifactIntegrityError("解析背景快照与任务不一致")
        text = snapshot.get("series_bible")
        images = snapshot.get("images")
        if (text is not None and not isinstance(text, str)) or not isinstance(images, list):
            raise ArtifactIntegrityError("解析背景快照格式错误")
        if any(
            not isinstance(item, dict) or not isinstance(item.get("image_id"), str)
            or not isinstance(item.get("sha256"), str) for item in images
        ) or [item["image_id"] for item in images] != extract_background_image_ids(text or ""):
            raise ArtifactIntegrityError("解析背景快照图片引用不一致")
        references = []
        for item in images:
            try:
                path = self._artifacts.resolve_series_image(
                    item["image_id"], series_id=lease.series_id, variant="model",
                )
                actual = self._artifacts.fingerprint(self._artifacts.uri_for_path(path))
            except (FileNotFoundError, ValueError) as exc:
                raise ArtifactIntegrityError("解析背景参考图已丢失或归属错误") from exc
            if actual.sha256 != item["sha256"]:
                raise ArtifactIntegrityError("解析背景参考图内容已改变")
            references.append(BackgroundImage(image_id=item["image_id"], path=path))
        self._jobs.assert_current_lease(lease)
        return text, references, False

    def _embed_text_chunks(
        self,
        lease: ParseJobLease,
        attempt_directory: Path,
        documents: Mapping[int, VideoChunkRetrievalDocument],
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> dict[int, PreparedEmbedding]:
        embeddings: dict[int, PreparedEmbedding] = {}
        for sequence_no in sorted(documents):
            document = documents[sequence_no]
            input_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
            video_chunk_id = derive_video_chunk_id(lease.video_id, sequence_no)
            committed_uri = _committed_embedding_uri(
                checkpoints.get(sequence_no),
                stage=VideoChunkCheckpointStage.TEXT_EMBEDDED,
                field_name="text_embedding_artifact_uri",
            )
            if committed_uri is not None:
                embedding = self._load_committed_embedding(
                    committed_uri,
                    video_chunk_id=video_chunk_id,
                    modality="text",
                    sequence_no=sequence_no,
                    input_hash=input_hash,
                )
            else:
                path = (
                    attempt_directory
                    / "embeddings"
                    / f"chunk-{sequence_no:04d}.text.json"
                )
                with self._measure_stage(
                    lease, sequence_no, VideoChunkTimingStage.TEXT_EMBEDDING,
                ):
                    embedding = self._load_or_embed(
                        path,
                        video_chunk_id=video_chunk_id,
                        modality="text",
                        sequence_no=sequence_no,
                        input_hash=input_hash,
                        create=lambda: self._embedder.embed_text(document.text),
                        lease=lease,
                    )
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.TEXT_EMBEDDED,
                    sequence_no=sequence_no,
                    text_embedding_artifact_uri=self._artifacts.uri_for_path(path),
                )
            embeddings[sequence_no] = embedding
        return embeddings

    def _embed_vision_chunks(
        self,
        lease: ParseJobLease,
        attempt_directory: Path,
        chunk_media_refs: Mapping[int, str],
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> dict[int, PreparedEmbedding]:
        embeddings: dict[int, PreparedEmbedding] = {}
        for sequence_no in sorted(chunk_media_refs):
            media_uri = chunk_media_refs[sequence_no]
            media = self._artifacts.resolve(media_uri)
            input_hash = self._artifacts.fingerprint(media_uri).sha256
            video_chunk_id = derive_video_chunk_id(lease.video_id, sequence_no)
            committed_uri = _committed_embedding_uri(
                checkpoints.get(sequence_no),
                stage=VideoChunkCheckpointStage.VISION_EMBEDDED,
                field_name="vision_embedding_artifact_uri",
            )
            if committed_uri is not None:
                embedding = self._load_committed_embedding(
                    committed_uri,
                    video_chunk_id=video_chunk_id,
                    modality="video",
                    sequence_no=sequence_no,
                    input_hash=input_hash,
                )
            else:
                path = (
                    attempt_directory
                    / "embeddings"
                    / f"chunk-{sequence_no:04d}.vision.json"
                )
                with self._measure_stage(
                    lease, sequence_no, VideoChunkTimingStage.VISION_EMBEDDING,
                ):
                    embedding = self._load_or_embed(
                        path,
                        video_chunk_id=video_chunk_id,
                        modality="video",
                        sequence_no=sequence_no,
                        input_hash=input_hash,
                        create=lambda media=media: self._embedder.embed_video(media),
                        lease=lease,
                    )
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints.get(sequence_no),
                    requested_stage=VideoChunkCheckpointStage.VISION_EMBEDDED,
                    sequence_no=sequence_no,
                    vision_embedding_artifact_uri=self._artifacts.uri_for_path(path),
                )
            embeddings[sequence_no] = embedding
        return embeddings

    def _load_or_embed(
        self,
        path: Path,
        *,
        video_chunk_id: str,
        modality: str,
        sequence_no: int,
        input_hash: str,
        create: Callable[[], EmbeddingResult],
        lease: ParseJobLease | None = None,
    ) -> PreparedEmbedding:
        if lease is None and path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("video_chunk_id") == video_chunk_id
                and payload.get("input_sha256") == input_hash
                and isinstance(payload.get("embedding"), dict)
            ):
                result = EmbeddingResult.model_validate(payload["embedding"])
                self._validate_embedding(result, modality)
                return _prepared_embedding(result, input_hash, video_chunk_id)

        with parse_chunk_scope(sequence_no, category="embedding"):
            result = create()
        if lease is not None:
            self._jobs.assert_current_lease(lease)
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

    def _load_committed_embedding(
        self,
        uri: str,
        *,
        video_chunk_id: str,
        modality: str,
        sequence_no: int,
        input_hash: str,
    ) -> PreparedEmbedding:
        try:
            path = self._artifacts.resolve(uri)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("embedding artifact must be a JSON object")
            if (
                payload.get("video_chunk_id") != video_chunk_id
                or payload.get("input_sha256") != input_hash
                or not isinstance(payload.get("embedding"), dict)
            ):
                raise ValueError("embedding artifact identity does not match")
            result = EmbeddingResult.model_validate(payload["embedding"])
            self._validate_embedding(result, modality)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ArtifactIntegrityError(
                f"committed {modality} embedding artifact is invalid for "
                f"VideoChunk {sequence_no}"
            ) from exc
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

    def _advance_stage(
        self,
        lease: ParseJobLease,
        stage: ParseJobStage,
    ) -> None:
        if not self._jobs.update_progress(
            lease,
            stage=stage,
        ):
            raise ParseJobLeaseLostError("parse job lease is no longer current")

    def _save_checkpoint(
        self,
        lease: ParseJobLease,
        current: VideoChunkCheckpointState | None,
        *,
        requested_stage: VideoChunkCheckpointStage,
        sequence_no: int,
        chunk_start_ms: int | None = None,
        chunk_end_ms: int | None = None,
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
                chunk_start_ms=(
                    current.chunk_start_ms
                    if chunk_start_ms is None and current is not None
                    else chunk_start_ms
                ),
                chunk_end_ms=(
                    current.chunk_end_ms
                    if chunk_end_ms is None and current is not None
                    else chunk_end_ms
                ),
                chunk_media_uri=chunk_media_uri,
                analysis_window_media_uri=analysis_window_media_uri,
                parse_artifact_uri=parse_artifact_uri,
                text_embedding_artifact_uri=text_embedding_artifact_uri,
                vision_embedding_artifact_uri=vision_embedding_artifact_uri,
            ),
        )


def _attempt_directory(work_directory: Path, lease_version: int) -> Path:
    return work_directory / "leases" / f"lease-{lease_version:08d}"


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
        if provenance.model != models.video_parser or provenance.input_mode != "video":
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
        "group_duration_ms": fallback.group_duration_ms,
        "max_frames_per_group": fallback.max_frames_per_group,
        "fallback_transcript_model": fallback.transcript_model,
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


_CHECKPOINT_ORDER = {
    stage: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
