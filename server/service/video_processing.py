from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns

from PIL import Image
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
from engine.parse.pipeline import (
    ParseChunkInput,
    ParsePipeline,
    ParsePipelineInput,
)
from engine.parse.schemas import AnalysisWindow

from ..config.video_processing import VideoProcessingConfig
from ..config.parse_profile import ParseConfig
from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from ..model.job import ParseJobStage, VideoChunkCheckpointStage
from ..mq.contracts import ParseJobLeaseLostError
from ..repository.job import ParseJobRepository
from ..repository.video import VideoRepository
from ..schema import VideoChunkKeyframeReference
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore
from ..utils.files import write_json_once
from .index_sync import IndexSyncService
from .parse_persistence import VideoParsePersistence
from .parse_usage import ParseUsageService


@dataclass(frozen=True, slots=True)
class _PreparedMedia:
    chunks: tuple[ParseChunkInput, ...]
    chunk_media_refs: dict[int, str]
    analysis_media_refs: dict[int, str]
    keyframes_by_sequence: dict[
        int,
        tuple[VideoChunkKeyframeReference, ...],
    ]


class VideoProcessingService:
    """Turn one claimed original Video into searchable MySQL/Qdrant facts."""

    def __init__(
        self,
        jobs: ParseJobRepository,
        videos: VideoRepository,
        index_sync: IndexSyncService,
        artifacts: ArtifactStore,
        pipeline: ParsePipeline,
        *,
        config: VideoProcessingConfig | None = None,
        parse_usage: ParseUsageService | None = None,
        monotonic_clock: Callable[[], int] = monotonic_ns,
        parse_config: ParseConfig | None = None,
        processor_factory: Callable[[ParseConfig], "VideoProcessingService"] | None = None,
    ) -> None:
        self._jobs = jobs
        self._videos = videos
        self._index_sync = index_sync
        self._artifacts = artifacts
        self._pipeline = pipeline
        self._config = config or VideoProcessingConfig()
        self._parse_usage = parse_usage
        self._monotonic_clock = monotonic_clock
        self._parse_config = parse_config
        self._processor_factory = processor_factory

    def process(self, lease: ParseJobLease) -> list[str]:
        if lease.parse_config is not None and self._processor_factory is not None:
            selected = ParseConfig.model_validate(lease.parse_config)
            return self._processor_factory(selected).process(lease)
        capture = (
            self._parse_usage.capture(lease)
            if self._parse_usage
            else nullcontext()
        )
        with capture:
            return self._process(lease)

    def _process(self, lease: ParseJobLease) -> list[str]:
        processing_started_ns = self._monotonic_clock()
        source_video = self._verified_source(lease)
        work_directory = self._artifacts.work_directory(lease.job_id)
        attempt_directory = _attempt_directory(
            work_directory,
            lease.lease_version,
        )
        checkpoints = {
            item.sequence_no: item
            for item in self._jobs.list_checkpoints(lease.job_id)
        }
        (
            series_bible,
            background_images,
            missing_legacy_background,
        ) = self._parse_background(lease, work_directory, checkpoints)
        if self._pipeline.models.transcript_mode != self._config.asr_mode:
            raise RuntimeError(
                "Parse pipeline described a different transcript mode"
            )
        self._advance_stage(lease, ParseJobStage.PREPARING)
        windows = self._plan_chunks(
            lease,
            source_video,
            attempt_directory,
            checkpoints,
        )
        media = self._prepare_media(
            lease,
            attempt_directory,
            windows,
            checkpoints,
        )
        persistence = VideoParsePersistence(
            self._jobs,
            self._videos,
            self._artifacts,
            lease=lease,
            attempt_directory=attempt_directory,
            checkpoints=checkpoints,
            chunk_media_refs=media.chunk_media_refs,
            analysis_window_media_refs=media.analysis_media_refs,
            keyframes_by_sequence=media.keyframes_by_sequence,
            config=self._config,
            parse_config=self._parse_config,
            missing_legacy_background=missing_legacy_background,
            processing_started_ns=processing_started_ns,
            monotonic_clock=self._monotonic_clock,
        )
        video_chunk_ids = self._pipeline.run(
            ParsePipelineInput(
                source_video=source_video,
                duration_ms=lease.duration_ms,
                chunk_duration_ms=self._config.chunk_duration_ms,
                context_overlap_ms=self._config.context_overlap_ms,
                chunks=media.chunks,
                series_bible=series_bible,
                background_images=tuple(background_images),
            ),
            persistence=persistence,
        )
        self._sync_index(lease, checkpoints)
        return video_chunk_ids

    def _plan_chunks(
        self,
        lease: ParseJobLease,
        source_video: Path,
        attempt_directory: Path,
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> list[AnalysisWindow]:
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
        if [window.sequence_no for window in windows] != list(range(len(windows))):
            raise RuntimeError("analysis windows must have contiguous sequence numbers")
        return windows

    def _prepare_media(
        self,
        lease: ParseJobLease,
        attempt_directory: Path,
        windows: list[AnalysisWindow],
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> _PreparedMedia:
        chunks: list[ParseChunkInput] = []
        chunk_media_refs: dict[int, str] = {}
        analysis_media_refs: dict[int, str] = {}
        keyframes_by_sequence: dict[
            int,
            tuple[VideoChunkKeyframeReference, ...],
        ] = {}
        published_filename_prefix = (
            f"{lease.job_id}-lease-{lease.lease_version:08d}"
        )
        for window in windows:
            sequence_no = window.sequence_no
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
                filename=f"{published_filename_prefix}-{canonical_path.name}",
            )
            analysis_artifact = self._artifacts.publish_video_artifact(
                window.media_path,
                series_id=lease.series_id,
                video_id=lease.video_id,
                category="analysis-windows",
                filename=f"{published_filename_prefix}-{window.media_path.name}",
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
            chunks.append(
                ParseChunkInput(
                    window=window,
                    canonical_media_path=self._artifacts.resolve(
                        chunk_artifact.uri
                    ),
                    canonical_media_sha256=self._artifacts.fingerprint(
                        chunk_artifact.uri
                    ).sha256,
                )
            )
        return _PreparedMedia(
            chunks=tuple(chunks),
            chunk_media_refs=chunk_media_refs,
            analysis_media_refs=analysis_media_refs,
            keyframes_by_sequence=keyframes_by_sequence,
        )

    def _sync_index(
        self,
        lease: ParseJobLease,
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> None:
        for sequence_no in sorted(checkpoints):
            video_chunk_id = checkpoints[sequence_no].video_chunk_id
            if _checkpoint_rank(checkpoints[sequence_no].stage) < _checkpoint_rank(
                VideoChunkCheckpointStage.INDEXED.value
            ):
                self._jobs.assert_current_lease(lease)
                self._index_sync.sync_video_chunks([video_chunk_id])
                self._jobs.assert_current_lease(lease)
                checkpoints[sequence_no] = self._save_checkpoint(
                    lease,
                    checkpoints[sequence_no],
                    requested_stage=VideoChunkCheckpointStage.INDEXED,
                    sequence_no=sequence_no,
                )

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
            context = self._parse_config.context if self._parse_config is not None else None
            source_text = (
                prepare_background_text(lease.character_bible, lease.important_extra_data)
                if context is None or context.background_text_enabled or context.background_images_enabled
                else None
            )
            text = source_text
            image_ids = (
                extract_background_image_ids(source_text or "")
                if context is None or context.background_images_enabled else []
            )
            if context is not None:
                if not context.background_text_enabled:
                    text = None
                elif text is not None and context.background_text_max_chars is not None:
                    text = text[: context.background_text_max_chars]
                if not context.background_images_enabled:
                    image_ids = []
                elif context.max_background_images is not None:
                    image_ids = image_ids[: context.max_background_images]
            images = []
            for image_id in image_ids:
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
                    "version": 2 if context is not None else 1,
                    "job_id": lease.job_id,
                    "workspace_id": lease.workspace_id,
                    "series_id": lease.series_id,
                    "content_hash": lease.content_hash,
                    "series_bible": text,
                    "images": images,
                },
            )

        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or snapshot.get("version") not in {1, 2} or any(
            snapshot.get(key) != expected for key, expected in {
                "job_id": lease.job_id, "workspace_id": lease.workspace_id,
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
        ) or (
            snapshot["version"] == 1
            and [item["image_id"] for item in images] != extract_background_image_ids(text or "")
        ) or len({item["image_id"] for item in images}) != len(images):
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
        selected = self._parse_config
        if selected is None:
            return text, references, False
        context = selected.context
        if not context.background_text_enabled:
            text = None
        elif text is not None and context.background_text_max_chars is not None:
            text = text[: context.background_text_max_chars]
        if not context.background_images_enabled:
            references = []
        elif context.max_background_images is not None:
            references = references[: context.max_background_images]
        if references:
            limited: list[BackgroundImage] = []
            for reference in references:
                with Image.open(reference.path) as image:
                    if max(image.size) <= context.background_image_max_edge:
                        limited.append(reference)
                        continue
                    output = work_directory / "background-input" / f"{reference.image_id}.png"
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if not output.is_file():
                        resized = image.copy()
                        resized.thumbnail((context.background_image_max_edge,) * 2)
                        resized.convert("RGB").save(output, format="PNG")
                    limited.append(BackgroundImage(image_id=reference.image_id, path=output))
            references = limited
        return text, references, False

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
        processing_duration_ms: int | None = None,
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
            processing_duration_ms=processing_duration_ms,
        )


def _attempt_directory(work_directory: Path, lease_version: int) -> Path:
    return work_directory / "leases" / f"lease-{lease_version:08d}"


def _checkpoint_rank(stage: str) -> int:
    return _CHECKPOINT_ORDER[VideoChunkCheckpointStage(stage)]


_CHECKPOINT_ORDER = {
    stage: order for order, stage in enumerate(VideoChunkCheckpointStage)
}
