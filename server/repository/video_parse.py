from __future__ import annotations

import math
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agent.video_parse_v2.input import ParseConfig
from agent.video_parse_v2.scheduler import (
    chunk_index_ready, summarize_video_task, voiceprint_preparation_min_chunks,
)
from engine.parsev2.schemas import DialogueEvent, VideoParseResult
from engine.parsev2.voiceprint import CharacterVoiceprint, Voiceprint, VoiceprintReference
from engine.parsev2.validation import (
    RefinementMergeConflictError,
    merge_refinement_content,
    parse_content_fingerprint,
    validate_result_time_range,
)
from engine.rag.contracts import (
    TranscriptCueView,
    VideoChunkEvidence,
    VideoChunkRetrievalIdentity,
)
from engine.rag.indexing.projection import project_chunk_index
from engine.rag.retrieval.contracts import ChunkRetrievalFacts

from ..constants.parse import (
    MAX_INDEX_GENERATION, RUNTIME_DETAIL_KEY, VOICEPRINT_REFS_PER_SPEAKER,
    VOICEPRINT_CONSISTENCY_MIN_REFS, VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN,
    VOICEPRINT_B0_MIN_AUDIO_MS,
)
from ..model.identifiers import derive_video_chunk_id
from ..model.dto.jobDTO import (
    ChunkParseContentState,
    ChunkExecutionState,
    ChunkParseLease,
    IndexableChunkState,
    ChunkIndexingSnapshot,
    IntervalMemberSnapshot,
    IntervalParseLease,
    ModelCallReservation,
    PrefixParseLease,
    PreparedVideoParseState,
    VideoParseTaskInputState,
    VideoParseTaskState,
    VideoParseLease,
    VideoChunkState,
)
from ..model.job import (
    ChunkTaskPhase,
    ChunkParseContent,
    ChunkParseTask,
    ChunkParseTaskStatus,
    IntervalParseContent,
    IntervalParseTask,
    IntervalParseTaskStatus,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoParseTaskStatus,
    interval_full_ready_mask,
    interval_ready_mask_bit,
    validate_interval_parse_task_members,
)
from ..model.video import Series, VideoChunk, Video
from ..utils.retry import task_retry_delay
from ..utils.time import (
    datetime_to_unix_time_ms,
    now_utc,
    unix_time_ms_to_datetime,
)
from ..utils.validation import canonical_uuid, require_nonempty_text
from .video import SeriesNotReadyError, SeriesScopeNotFoundError, VideoChunkNotFoundError


INDEX_EMBEDDINGS_DETAIL_KEY = "_index_embeddings_v2"
EVENT_INDEX_TARGET_DETAIL_KEY = "_event_index_target_v1"
_REFINE_SUBMITTED = {IntervalParseTaskStatus.COMPLETE.value}
_VOICEPRINT_PREPARATION_KEY = "voiceprint_preparation"


class VideoParseRepositoryError(RuntimeError):
    pass


class VideoParseTaskNotFoundError(VideoParseRepositoryError):
    pass


class VideoParseConflictError(VideoParseRepositoryError):
    pass


class VideoParseSnapshotConflictError(VideoParseConflictError):
    """A refinement input changed; refresh facts without rerunning observations."""

    def __init__(self, message: str, *, reason: str = "snapshot_changed",
                 context: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.context = dict(context or {})
        super().__init__(message)


class VoiceprintRegistrationRejectedError(VideoParseConflictError):
    """A valid preparation can continue by choosing a different reference."""

    def __init__(self, code: str, message: str, retry_hint: str) -> None:
        self.code = code
        self.retry_hint = retry_hint
        super().__init__(message)


class VideoParseLeaseLostError(VideoParseRepositoryError):
    pass


class ModelCallBudgetExhaustedError(VideoParseRepositoryError):
    pass


class ModelCallLeaseConflictError(VideoParseRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class VideoParseRetrievalScope:
    """Current queryable V2 task set and its shared index contract."""

    series_ids: tuple[str, ...]
    searched_video_ids: tuple[str, ...]
    parse_tasks_by_video_id: Mapping[str, str]
    series_by_video_id: Mapping[str, str]
    parse_config: ParseConfig
    index_generation: int
    partial: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def embedding_model(self) -> str:
        return self.parse_config.models.embedding.model


@dataclass(frozen=True, slots=True)
class HydratedRetrievalCandidate:
    facts: ChunkRetrievalFacts
    evidence: VideoChunkEvidence
    contexts: tuple[ChunkRetrievalFacts, ...] = ()


@dataclass(frozen=True, slots=True)
class RealtimeChunkState:
    chunk_parse_task_id: str
    video_chunk_id: str
    sequence_no: int
    start_ms: int
    end_ms: int
    status: str
    content: VideoParseResult | None
    content_version: int | None
    last_error: dict | None
    parent_error: str | None = None
    phase: int = ChunkTaskPhase.PARSE


def invalidate_series_prefixes_from(
    session: Session,
    *,
    series_id: str,
    episode_order: int,
    video_id: str,
    now: datetime,
) -> tuple[str, ...]:
    """Fence and reset the latest enabled Prefix chain from one episode.

    The caller owns the transaction so task creation, retry takeover, or an
    episode-order edit can invalidate dependent Series compact state
    atomically. Only parent rows are locked; content rows are deliberately
    outside this operation's lock set.
    """

    rows = list(session.execute(
        select(
            VideoParseTask.id,
            VideoParseTask.video_id,
            VideoParseTask.version,
            Video.episode_order,
        )
        .join(
            Video,
            Video.id == VideoParseTask.video_id,
        )
        .where(
            Video.series_id == series_id,
            VideoParseTask.is_deleted.is_(False),
            or_(
                Video.episode_order > episode_order,
                and_(
                    Video.episode_order == episode_order,
                    Video.id >= video_id,
                ),
            ),
        )
        .order_by(
            Video.episode_order,
            Video.id,
            VideoParseTask.version.desc(),
            VideoParseTask.id.desc(),
        )
    ))
    latest_by_video: dict[str, tuple[int, str, str]] = {}
    for task_id, task_video_id, _version, task_episode_order in rows:
        latest_by_video.setdefault(
            task_video_id,
            (task_episode_order, task_video_id, task_id),
        )

    locked: list[VideoParseTask] = []
    for _order, task_video_id, task_id in sorted(latest_by_video.values()):
        task = session.scalar(
            select(VideoParseTask)
            .where(
                VideoParseTask.id == task_id,
                VideoParseTask.is_deleted.is_(False),
            )
            .with_for_update()
        )
        if task is None:
            raise VideoParseConflictError(
                "Prefix invalidation frontier changed; retry the transaction"
            )
        if _latest_video_task_id(session, task_video_id) != task.id:
            raise VideoParseConflictError(
                "Prefix invalidation frontier changed; retry the transaction"
            )
        if task.parse_config.get("compact_enabled", False):
            locked.append(task)

    now_ms = datetime_to_unix_time_ms(now)
    for task in locked:
        prefix = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]["prefix"]
        expires_at_ms = prefix.get("lease_expire_time_ms")
        if (
            prefix.get("lease_owner") is not None
            and isinstance(expires_at_ms, int)
            and expires_at_ms > now_ms
        ):
            raise VideoParseConflictError(
                "cannot invalidate Series Prefix chain with an active lease"
            )

    invalidated: list[str] = []
    for task in locked:
        detail = _ensure_runtime(task.detail)
        runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
        prefix = deepcopy(runtime["prefix"])
        prefix.update({
            "next_chunk_sequence_no": 0,
            "video_prefix_compact": "",
            "series_prefix_compact": "",
            "lease_owner": None,
            "lease_expire_time_ms": None,
            "execution_task_id": None,
            "next_execute_time_ms": now_ms,
            "retry_count": 0,
            "last_error": None,
            "lease_version": int(prefix.get("lease_version", 0)) + 1,
        })
        runtime["prefix"] = prefix
        detail[RUNTIME_DETAIL_KEY] = runtime
        task.detail = detail
        task.update_time = datetime_to_unix_time_ms(now)
        invalidated.append(task.id)
    return tuple(invalidated)


class VideoParseRepository:
    """Transactional persistence for the V2 parse pipeline.

    Model and Elasticsearch calls must happen outside these methods. Every
    mutating method either fences a Worker lease or locks the parent task row.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure_realtime_task(
        self,
        *,
        tenant_id: str,
        video_id: str,
        default_parse_config: ParseConfig,
        now: datetime | None = None,
    ) -> VideoParseTaskInputState:
        """Reuse the latest frozen V2 task, or create an unaccepted base task.

        Existing tasks use parent -> Video lock order. New task creation locks
        the Video and retries if a parent appeared while acquiring that lock.
        Unaccepted tasks remain outside the full Parse delivery queues.
        """

        created_at = now if now is not None else now_utc()
        while True:
            with self._session_factory() as session:
                candidate_id = _latest_video_task_id(session, video_id)
            with self._session_factory.begin() as session:
                task = None
                if candidate_id is not None:
                    task = session.scalar(select(VideoParseTask).where(
                        VideoParseTask.id == candidate_id,
                        VideoParseTask.is_deleted.is_(False),
                    ).with_for_update())
                    if task is None:
                        continue
                video = session.scalar(select(Video).where(
                    Video.id == video_id,
                ).with_for_update())
                series = session.get(Series, video.series_id) if video is not None else None
                if video is None or series is None or series.tenant_id != tenant_id or video.upload_status != "COMPLETED":
                    raise VideoParseTaskNotFoundError("Video does not exist in this workspace")
                if _latest_video_task_id(session, video_id) != candidate_id:
                    # Release the Video lock before locking a newly observed
                    # parent so parent -> Video remains consistent everywhere.
                    continue
                runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY] if task else {}
                is_placeholder = task is not None and (
                    not runtime.get("accepted")
                    and task.status == VideoParseTaskStatus.PENDING.value
                    and runtime.get("execution_mode") in {None, "v1"}
                )
                is_v2 = task is not None and (
                    runtime.get("execution_mode") == "v2"
                    or (runtime.get("execution_mode") is None and task.pipeline_version == "v2")
                    or is_placeholder
                )
                if is_placeholder and session.scalar(select(SeriesParseJob.id.label("job_id")).where(
                    SeriesParseJob.video_id == video_id,
                    SeriesParseJob.status.in_([
                        ParseJobStatus.QUEUED.value, ParseJobStatus.RUNNING.value,
                        ParseJobStatus.RETRY_WAIT.value,
                    ]),
                ).limit(1)) is not None:
                    raise VideoParseConflictError("Video has an active legacy Parse worker")
                if is_v2:
                    config = ParseConfig.model_validate(task.parse_config)
                    if task.chunk_count != math.ceil(video.duration_ms / (config.chunk_duration * 1000)):
                        raise VideoParseConflictError("VideoParseTask has an incompatible Chunk count")
                    if not runtime.get("accepted"):
                        detail = _ensure_runtime(task.detail)
                        detail[RUNTIME_DETAIL_KEY].update({"execution_mode": "v2", "realtime_only": True})
                        task.detail = detail
                else:
                    if task is not None and task.status not in {
                        VideoParseTaskStatus.COMPLETE.value,
                        VideoParseTaskStatus.PARSING_FAILED.value,
                        VideoParseTaskStatus.INDEXING_FAILED.value,
                    }:
                        raise VideoParseConflictError("Video has an active task from another Parse pipeline")
                    config = default_parse_config
                    task = VideoParseTask(
                        id=str(uuid4()), video_id=video_id, pipeline_version="v2",
                        status=VideoParseTaskStatus.PENDING.value,
                        parse_config=config.model_dump(mode="json"),
                        chunk_count=math.ceil(video.duration_ms / (config.chunk_duration * 1000)),
                        version=1 if task is None else task.version + 1,
                        next_execute_time=created_at, attempt_count=0,
                        max_attempts=config.execution.max_retry_count + 1,
                        lease_version=0, processing_duration_ms=0,
                        processing_timing_complete=True, is_test=False, is_deleted=False,
                        detail={RUNTIME_DETAIL_KEY: {
                            "accepted": False, "execution_mode": "v2", "realtime_only": True,
                        }}, create_time=datetime_to_unix_time_ms(created_at), update_time=datetime_to_unix_time_ms(created_at),
                    )
                    session.add(task)
                session.flush()
                return _task_input_state(task, video, tenant_id=tenant_id, series=series)

    def load_realtime_chunks(
        self,
        video_parse_task_id: str,
        *,
        tenant_id: str,
        video_id: str,
        task_version: int,
    ) -> tuple[RealtimeChunkState, ...]:
        """Read current durable base evidence without requiring an index receipt."""

        with self._session_factory() as session:
            task = session.scalar(
                select(VideoParseTask)
                .join(Video, Video.id == VideoParseTask.video_id)
                .join(Series, Series.id == Video.series_id)
                .where(
                    VideoParseTask.id == video_parse_task_id,
                    VideoParseTask.video_id == video_id,
                    VideoParseTask.version == task_version,
                    VideoParseTask.is_deleted.is_(False),
                    Series.tenant_id == tenant_id,
                )
            )
            if task is None or _latest_video_task_id(session, video_id) != task.id:
                raise VideoParseConflictError("Realtime Parse task changed; retry with the latest task")
            parent_error = _video_failure_status(task)
            if task.status in {VideoParseTaskStatus.PARSING_FAILED.value, VideoParseTaskStatus.INDEXING_FAILED.value}:
                parent_error = task.status
            rows = session.execute(
                select(ChunkParseTask, VideoChunk, ChunkParseContent)
                .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
                .outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
                .where(ChunkParseTask.video_task_id == task.id)
                .order_by(VideoChunk.sequence_no)
            )
            return tuple(RealtimeChunkState(
                chunk_parse_task_id=child.id, video_chunk_id=chunk.id,
                sequence_no=chunk.sequence_no, start_ms=chunk.start_ms,
                end_ms=chunk.end_ms, status=child.status,
                content=(VideoParseResult.model_validate(content.content)
                         if content is not None and content.content else None),
                content_version=content.version if content is not None else None,
                last_error=deepcopy(child.last_error),
                parent_error=parent_error,
                phase=child.phase,
            ) for child, chunk, content in rows)

    def finish_realtime_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> None:
        """Release topology preparation; only accepted tasks enter full Parse."""

        finished_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, finished_at)
            runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
            task.status = (VideoParseTaskStatus.PARSING.value if runtime.get("accepted")
                           else VideoParseTaskStatus.PENDING.value)
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
            if not runtime.get("accepted"):
                task.capacity_acquired = False
            task.next_execute_time = finished_at
            task.update_time = datetime_to_unix_time_ms(finished_at)
            video = session.get(Video, task.video_id)
            if video is not None:
                video.parse_status = task.status

    def get_chunk_evidence(
        self, video_chunk_id: str, *, tenant_id: str,
    ) -> VideoChunkEvidence:
        """Read media evidence through the latest complete V2 receipt."""

        chunk_id = canonical_uuid(video_chunk_id, "video_chunk_id")
        workspace = require_nonempty_text(tenant_id, "tenant_id")
        with self._session_factory() as session:
            row = session.execute(
                select(Series, Video)
                .join(Video, Video.series_id == Series.id)
                .join(VideoChunk, VideoChunk.video_id == Video.id)
                .where(
                    VideoChunk.id == chunk_id,
                    Series.tenant_id == workspace,
                )
            ).one_or_none()
            if row is None:
                raise VideoChunkNotFoundError("videoChunkId does not exist in this workspace")
            series, video = row
            task_id = _latest_video_task_id(session, video.id)
            if task_id is None:
                raise VideoChunkNotFoundError("videoChunkId has no current V2 task")
            task = session.get(VideoParseTask, task_id)
            if task is None or task.is_deleted:
                raise VideoChunkNotFoundError("videoChunkId has no current V2 task")
            if task.status != VideoParseTaskStatus.COMPLETE.value:
                raise VideoParseConflictError("videoChunkId latest V2 task is not COMPLETE")
            content = session.scalar(
                select(ChunkParseContent)
                .join(
                    ChunkParseTask,
                    ChunkParseTask.id == ChunkParseContent.chunk_task_id,
                )
                .where(
                    ChunkParseTask.video_task_id == task_id,
                    ChunkParseTask.chunk_id == chunk_id,
                    ChunkParseTask.phase == ChunkTaskPhase.INDEX,
                    ChunkParseTask.status == ChunkParseTaskStatus.COMPLETE.value,
                    ChunkParseTask.indexed_content_version == ChunkParseContent.version,
                )
            )
            if content is None:
                raise VideoParseConflictError("videoChunkId has no COMPLETE V2 content")
            identity = VideoChunkRetrievalIdentity(
                tenant_id=workspace,
                series_id=series.id,
                video_id=video.id,
                parse_task_id=task_id,
                content_version=content.version,
                index_generation=task.target_index_generation,
            )
        return self.hydrate_retrieval_evidence([(chunk_id, identity)])[0]

    def resolve_retrieval_scope(
        self,
        series_ids: Sequence[str],
        *,
        tenant_id: str,
        require_sparse: bool = False,
    ) -> VideoParseRetrievalScope:
        """Select only Videos whose latest non-deleted V2 task is COMPLETE.

        A newer retry/reparse immediately removes its Video from the searchable
        scope. Every retained Chunk must have a publication for the current content
        version and the frozen target generation.
        """

        if isinstance(series_ids, (str, bytes)):
            raise ValueError("series_ids must be a sequence of IDs")
        try:
            ordered_series_ids = tuple(dict.fromkeys(
                canonical_uuid(item, "series_id") for item in series_ids
            ))
        except TypeError as exc:
            raise ValueError("series_ids must be a sequence of IDs") from exc
        if not ordered_series_ids:
            raise ValueError("series_ids must not be empty")
        workspace = require_nonempty_text(tenant_id, "tenant_id")

        with self._session_factory() as session:
            existing_series_ids = set(session.scalars(
                select(Series.id.label("series_id")).where(
                    Series.id.in_(ordered_series_ids),
                    Series.tenant_id == workspace,
                )
            ))
            missing = [
                series_id
                for series_id in ordered_series_ids
                if series_id not in existing_series_ids
            ]
            if missing:
                raise SeriesScopeNotFoundError(
                    "seriesId values do not exist in this workspace: "
                    + ", ".join(missing)
                )

            videos = list(session.scalars(
                select(Video)
                .where(Video.series_id.in_(ordered_series_ids))
                .order_by(
                    Video.episode_order,
                    Video.id,
                )
            ))
            if not videos:
                raise SeriesNotReadyError(
                    "selected Series have no COMPLETE Video Parse V2 task"
                )

            video_ids = [video.id for video in videos]
            latest_by_video: dict[str, VideoParseTask] = {}
            for task in session.scalars(
                select(VideoParseTask)
                .where(
                    VideoParseTask.video_id.in_(video_ids),
                    VideoParseTask.is_deleted.is_(False),
                )
                .order_by(
                    VideoParseTask.video_id,
                    VideoParseTask.version.desc(),
                    VideoParseTask.id.desc(),
                )
            ):
                latest_by_video.setdefault(task.video_id, task)

            selected = [
                (video, latest_by_video[video.id])
                for video in videos
                if video.id in latest_by_video
                and latest_by_video[video.id].status
                == VideoParseTaskStatus.COMPLETE.value
            ]
            if not selected:
                raise SeriesNotReadyError(
                    "selected Series have no COMPLETE Video Parse V2 task"
                )

            task_ids = [task.id for _video, task in selected]
            rows_by_task: dict[
                str,
                list[tuple[ChunkParseTask, VideoChunk, ChunkParseContent]],
            ] = {}
            for row in session.execute(
                select(
                    ChunkParseTask,
                    VideoChunk,
                    ChunkParseContent,
                )
                .join(
                    VideoChunk,
                    VideoChunk.id
                    == ChunkParseTask.chunk_id,
                )
                .join(
                    ChunkParseContent,
                    ChunkParseContent.chunk_task_id
                    == ChunkParseTask.id,
                )
                .where(
                    ChunkParseTask.video_task_id.in_(task_ids),
                    ChunkParseTask.phase == ChunkTaskPhase.INDEX,
                    ChunkParseTask.status
                    == ChunkParseTaskStatus.COMPLETE.value,
                    ChunkParseTask.indexed_content_version == ChunkParseContent.version,
                )
                .order_by(
                    ChunkParseTask.video_task_id,
                    VideoChunk.sequence_no,
                )
            ):
                rows_by_task.setdefault(row[0].video_task_id, []).append(row)

            shared_contract: tuple[int, str, str, int, float, int] | None = None
            selected_config: ParseConfig | None = None
            for video, task in selected:
                rows = rows_by_task.get(task.id, [])
                if (
                    len(rows) != task.chunk_count
                    or [chunk.sequence_no for _child, chunk, _content in rows]
                    != list(range(task.chunk_count))
                    or any(chunk.video_id != video.id for _child, chunk, _content in rows)
                ):
                    raise VideoParseConflictError(
                        "COMPLETE Video Parse V2 task has incomplete Chunk content"
                    )
                try:
                    config = ParseConfig.model_validate(task.parse_config)
                except ValueError as exc:
                    raise VideoParseConflictError(
                        "COMPLETE Video Parse V2 task has invalid fixed parse configuration"
                    ) from exc
                embedding = config.models.embedding
                for _child, chunk, content in rows:
                    if require_sparse and not config.indexing.sparse_enabled:
                        raise VideoParseConflictError("selected V2 task has no published Sparse representation")
                    contract = (
                        task.target_index_generation,
                        embedding.source,
                        embedding.encoder_model or embedding.model,
                        embedding.dimensions,
                        embedding.video_fps,
                        config.visual_input.max_height,
                    )
                    if shared_contract is None:
                        shared_contract = contract
                        selected_config = config
                    elif shared_contract != contract:
                        raise VideoParseConflictError(
                            "selected Video Parse V2 tasks use different index contracts"
                        )

            if shared_contract is None:
                raise VideoParseConflictError(
                    "selected Video Parse V2 tasks have no published Chunks"
                )
            assert selected_config is not None
            incomplete_scope = (
                len(selected) != len(videos)
                or {video.series_id for video in videos} != existing_series_ids
            )
            return VideoParseRetrievalScope(
                series_ids=ordered_series_ids,
                searched_video_ids=tuple(video.id for video, _task in selected),
                parse_tasks_by_video_id={
                    video.id: task.id for video, task in selected
                },
                series_by_video_id={
                    video.id: video.series_id for video, _task in selected
                },
                parse_config=selected_config,
                index_generation=shared_contract[0],
                partial=incomplete_scope,
                warnings=(
                    (
                        "部分所选 Series 没有视频，或部分视频的最新 Video Parse V2 "
                        "任务尚未完成，"
                        "本次结果只覆盖当前可检索视频。"
                    ),
                ) if incomplete_scope else (),
            )

    def validate_event_coverage(
        self, scope: VideoParseRetrievalScope, *, index_name: str, contract_id: str,
        video_chunk_ids: Sequence[str] | None = None,
    ) -> tuple[tuple[str, str, int], ...]:
        """Resolve paired, currently published Event sources within a trusted scope."""

        requested = None
        if video_chunk_ids is not None:
            if isinstance(video_chunk_ids, (str, bytes)):
                raise ValueError("video_chunk_ids must be a sequence")
            requested = {canonical_uuid(value, "video_chunk_id") for value in video_chunk_ids}
            if not requested:
                raise ValueError("video_chunk_ids must not be empty")
        sources = []
        with self._session_factory() as session:
            tasks = {task.id: task for task in session.scalars(select(VideoParseTask).where(
                VideoParseTask.id.in_(tuple(scope.parse_tasks_by_video_id.values())),
                VideoParseTask.is_deleted.is_(False),
            ))}
            if len(tasks) != len(scope.parse_tasks_by_video_id):
                raise VideoParseConflictError("Event retrieval scope changed")
            for parent in tasks.values():
                if (parent.status != VideoParseTaskStatus.COMPLETE.value
                        or _latest_video_task_id(session, parent.video_id) != parent.id):
                    raise VideoParseConflictError("Event source is no longer a COMPLETE publication")
            rows = session.execute(select(ChunkParseTask, ChunkParseContent, VideoChunk).join(
                ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
            ).join(
                VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
            ).where(ChunkParseTask.video_task_id.in_(tuple(tasks))))
            seen = set()
            counts = {task_id: 0 for task_id in tasks}
            for child, content, chunk in rows:
                if requested is not None and child.chunk_id not in requested:
                    continue
                seen.add(child.chunk_id)
                counts[child.video_task_id] += 1
                parent = tasks[child.video_task_id]
                if (child.phase != ChunkTaskPhase.INDEX
                        or child.status != ChunkParseTaskStatus.COMPLETE.value
                        or child.indexed_content_version != content.version):
                    raise VideoParseConflictError("Event source is no longer a COMPLETE publication")
                target = (parent.detail or {}).get(EVENT_INDEX_TARGET_DETAIL_KEY)
                if (not isinstance(target, Mapping) or target.get("index_name") != index_name
                        or target.get("contract_id") != contract_id):
                    raise VideoParseConflictError("Event index has not covered the selected Chunk scope")
                receipt = _index_receipt(content, parent.id, child.chunk_id)
                _require_event_publication(session, parent, child, content, receipt, target, chunk=chunk)
                sources.append((parent.id, child.chunk_id, content.version))
            if requested is not None and seen != requested:
                raise VideoParseConflictError("Event Chunk sub-scope is outside the authorized publication")
            if requested is None and any(counts[task_id] != task.chunk_count for task_id, task in tasks.items()):
                raise VideoParseConflictError("Event publication scope has missing Chunk content")
            if not sources:
                raise VideoParseConflictError("Event index has no covered Chunk sources")
        return tuple(sorted(sources))

    def event_index_status(self, video_parse_task_id: str) -> dict[str, Any]:
        """Report persisted Event coverage without changing task or index state."""
        task_id = canonical_uuid(video_parse_task_id, "video_parse_task_id")
        with self._session_factory() as session:
            parent = session.get(VideoParseTask, task_id)
            if parent is None or parent.is_deleted:
                raise VideoParseTaskNotFoundError(task_id)
            target = (parent.detail or {}).get(EVENT_INDEX_TARGET_DETAIL_KEY)
            rows = session.execute(select(ChunkParseTask, ChunkParseContent).outerjoin(
                ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
            ).where(ChunkParseTask.video_task_id == task_id).order_by(ChunkParseTask.chunk_id))
            chunks = []
            for child, content in rows:
                reason = None
                try:
                    if (target is None or content is None or child.phase != ChunkTaskPhase.INDEX
                            or child.status != ChunkParseTaskStatus.COMPLETE.value
                            or child.indexed_content_version != content.version):
                        raise VideoParseConflictError("Event source is not published")
                    receipt = _index_receipt(content, parent.id, child.chunk_id)
                    _require_event_publication(session, parent, child, content, receipt, target)
                except (TypeError, ValueError, VideoParseConflictError) as error:
                    reason = str(error)
                chunks.append({"video_chunk_id": child.chunk_id,
                               "version": content.version if content is not None else None,
                               "receipt_ready": reason is None, "reason": reason})
            ready = sum(item["receipt_ready"] for item in chunks)
            return {"video_parse_task_id": task_id, "target": deepcopy(target),
                    "task_complete": parent.status == VideoParseTaskStatus.COMPLETE.value,
                    "total_chunks": parent.chunk_count, "covered_chunks": ready,
                    "chunks": chunks}

    def hydrate_retrieval_evidence(
        self,
        candidates: Sequence[tuple[str, VideoChunkRetrievalIdentity]],
    ) -> list[VideoChunkEvidence]:
        """Keep the existing display-evidence interface for pinned V2 candidates."""

        return [item.evidence for item in self.hydrate_retrieval_candidates(candidates)]

    def hydrate_retrieval_candidates(
        self,
        candidates: Sequence[tuple[str, VideoChunkRetrievalIdentity]],
        *,
        include_context: bool = False,
    ) -> tuple[HydratedRetrievalCandidate, ...]:
        """Read formal facts behind the current Chunk publication version."""

        if isinstance(candidates, (str, bytes)):
            raise ValueError("candidates must be a sequence")
        if not isinstance(include_context, bool):
            raise ValueError("include_context must be a boolean")
        ordered = tuple(
            (
                canonical_uuid(video_chunk_id, "video_chunk_id"),
                VideoChunkRetrievalIdentity.model_validate(identity),
            )
            for video_chunk_id, identity in candidates
        )
        if not ordered:
            return ()
        if len({video_chunk_id for video_chunk_id, _identity in ordered}) != len(ordered):
            raise ValueError("candidates contain duplicate video_chunk_id values")

        task_ids = {identity.parse_task_id for _chunk_id, identity in ordered}
        chunk_ids = {video_chunk_id for video_chunk_id, _identity in ordered}
        with self._session_factory() as session:
            query = (
                select(
                    Series,
                    Video,
                    VideoChunk,
                    VideoParseTask,
                    ChunkParseTask,
                    ChunkParseContent,
                )
                .join(Video, Video.series_id == Series.id)
                .join(
                    VideoParseTask,
                    VideoParseTask.video_id == Video.id,
                )
                .join(
                    ChunkParseTask,
                    ChunkParseTask.video_task_id
                    == VideoParseTask.id,
                )
                .join(
                    VideoChunk,
                    VideoChunk.id
                    == ChunkParseTask.chunk_id,
                )
                .join(
                    ChunkParseContent,
                    ChunkParseContent.chunk_task_id
                    == ChunkParseTask.id,
                )
                .where(
                    VideoParseTask.id.in_(task_ids),
                    VideoParseTask.status
                    == VideoParseTaskStatus.COMPLETE.value,
                    VideoParseTask.is_deleted.is_(False),
                    ChunkParseTask.phase == ChunkTaskPhase.INDEX,
                    ChunkParseTask.status
                    == ChunkParseTaskStatus.COMPLETE.value,
                    ChunkParseTask.indexed_content_version == ChunkParseContent.version,
                )
            )
            rows = session.execute(
                query.where(VideoChunk.id.in_(chunk_ids))
            ).all()
            by_identity = {
                (task.id, chunk.id): (
                    series,
                    video,
                    chunk,
                    task,
                    _child,
                    content,
                )
                for series, video, chunk, task, _child, content in rows
            }

            neighbors_by_sequence: dict[tuple[str, int], tuple[Any, ...]] = {}
            if include_context and rows:
                neighbor_conditions = [
                    and_(
                        VideoParseTask.id == task.id,
                        VideoChunk.sequence_no.in_(
                            (chunk.sequence_no - 1, chunk.sequence_no + 1)
                        ),
                    )
                    for _series, _video, chunk, task, _child, _content in rows
                ]
                neighbors_by_sequence = {
                    (row[3].id, row[2].sequence_no): row
                    for row in session.execute(query.where(or_(*neighbor_conditions)))
                }

            hydrated: list[HydratedRetrievalCandidate] = []
            for video_chunk_id, identity in ordered:
                row = by_identity.get(
                    (identity.parse_task_id, video_chunk_id)
                )
                if row is None:
                    raise VideoParseConflictError(
                        "retrieval candidate is missing from COMPLETE V2 facts"
                    )
                series, video, chunk, task, child, content = row
                if chunk.video_id != video.id:
                    raise VideoParseConflictError(
                        "retrieval candidate Chunk belongs to a different Video"
                    )
                if _latest_video_task_id(session, video.id) != task.id:
                    raise VideoParseConflictError(
                        "retrieval candidate no longer belongs to the latest V2 task"
                    )
                _require_matching_retrieval_identity(
                    identity,
                    video_chunk_id=video_chunk_id,
                    child=child,
                    task=task,
                    series=series,
                    video=video,
                    content=content,
                )
                fact = _chunk_retrieval_facts(chunk, content, identity)
                contexts = []
                if include_context:
                    for sequence in (chunk.sequence_no - 1, chunk.sequence_no + 1):
                        if sequence < 0 or sequence >= task.chunk_count:
                            continue
                        neighbor = neighbors_by_sequence.get((task.id, sequence))
                        if neighbor is None:
                            raise VideoParseConflictError(
                                "retrieval context is missing from COMPLETE V2 facts"
                            )
                        (other_series, other_video, other_chunk,
                         _task, _child, other_content) = neighbor
                        if (
                            other_video.id != video.id
                            or other_chunk.video_id != video.id
                            or (
                                sequence < chunk.sequence_no
                                and other_chunk.end_ms != chunk.start_ms
                            )
                            or (
                                sequence > chunk.sequence_no
                                and other_chunk.start_ms != chunk.end_ms
                            )
                        ):
                            raise VideoParseConflictError(
                                "retrieval context is not a contiguous adjacent Chunk"
                            )
                        other_identity = VideoChunkRetrievalIdentity(
                            tenant_id=other_series.tenant_id,
                            series_id=other_series.id,
                            video_id=other_video.id,
                            parse_task_id=task.id,
                            content_version=other_content.version,
                            index_generation=task.target_index_generation,
                        )
                        contexts.append(_chunk_retrieval_facts(
                            other_chunk, other_content, other_identity
                        ))
                projection = project_chunk_index(
                    fact.result,
                    chunk_start_seconds=chunk.start_ms,
                    chunk_end_seconds=chunk.end_ms,
                )
                hydrated.append(HydratedRetrievalCandidate(
                    facts=fact,
                    evidence=_video_parse_evidence(
                        series, video, chunk, task, content, fact.result,
                        retrieval_text=projection.semantic_text,
                        identity=identity,
                    ),
                    contexts=tuple(contexts),
                ))
            return tuple(hydrated)

    def load_task_input(self, video_parse_task_id: str) -> VideoParseTaskInputState:
        with self._session_factory() as session:
            row = session.execute(
                select(VideoParseTask, Video, Series)
                .join(Video, Video.id == VideoParseTask.video_id)
                .join(Series, Series.id == Video.series_id)
                .where(
                    VideoParseTask.id == video_parse_task_id,
                    VideoParseTask.is_deleted.is_(False),
                )
            ).one_or_none()
            if row is None:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {video_parse_task_id!r} does not exist"
                )
            task, video, series = row
            generation = task.parse_config.get("indexing", {}).get("index_generation")
            if type(generation) is not int or not 1 <= generation <= MAX_INDEX_GENERATION:
                raise VideoParseConflictError("accepted task is missing a valid frozen index generation")
            return _task_input_state(task, video, tenant_id=series.tenant_id, series=series)

    def ensure_prepared(
        self,
        lease: VideoParseLease,
        video_chunk_ids: Sequence[str],
        *,
        now: datetime | None = None,
        _activate: bool = True,
    ) -> PreparedVideoParseState:
        """Idempotently freeze Chunk and Interval children for a claimed task."""

        prepared_at = now if now is not None else now_utc()
        ordered_chunk_ids = tuple(video_chunk_ids)
        if not ordered_chunk_ids or len(set(ordered_chunk_ids)) != len(ordered_chunk_ids):
            raise ValueError("video_chunk_ids must be a nonempty unique sequence")

        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, prepared_at)
            video = session.get(Video, task.video_id)
            if video is None:
                raise VideoParseConflictError("VideoParseTask Video no longer exists")
            chunk_rows = list(session.scalars(
                select(VideoChunk)
                .where(VideoChunk.id.in_(ordered_chunk_ids))
                .order_by(VideoChunk.sequence_no)
                .with_for_update()
            ))
            if (
                len(chunk_rows) != len(ordered_chunk_ids)
                or tuple(row.id for row in chunk_rows) != ordered_chunk_ids
                or any(row.video_id != task.video_id for row in chunk_rows)
                or [row.sequence_no for row in chunk_rows] != list(range(len(chunk_rows)))
                or len(chunk_rows) != task.chunk_count
            ):
                raise VideoParseConflictError(
                    "VideoParseTask chunks do not match its fixed ordered topology"
                )
            _validate_chunk_boundaries(video, task.parse_config, chunk_rows)

            existing_chunks = list(session.scalars(
                select(ChunkParseTask)
                .where(ChunkParseTask.video_task_id == task.id)
                .order_by(ChunkParseTask.id)
                .with_for_update()
            ))
            by_video_chunk = {item.chunk_id: item for item in existing_chunks}
            if len(by_video_chunk) != len(existing_chunks):
                raise VideoParseConflictError("duplicate ChunkParseTask topology")
            if set(by_video_chunk) - set(ordered_chunk_ids):
                raise VideoParseConflictError("existing ChunkParseTask topology is incompatible")

            chunk_tasks: list[ChunkParseTask] = []
            for chunk in chunk_rows:
                child = by_video_chunk.get(chunk.id)
                if child is None:
                    child = ChunkParseTask(
                        id=str(uuid4()),
                        video_task_id=task.id,
                        chunk_id=chunk.id,
                        status=ChunkParseTaskStatus.QUEUED.value,
                        next_execute_time=prepared_at,
                        retry_count=0,
                        lease_version=0,
                        detail={},
                        create_time=datetime_to_unix_time_ms(prepared_at),
                        update_time=datetime_to_unix_time_ms(prepared_at),
                    )
                    session.add(child)
                    session.flush()
                chunk_tasks.append(child)

            interval_specs = _interval_specs(
                len(chunk_tasks), _interval_size(task.parse_config)
            )
            existing_intervals = list(session.scalars(
                select(IntervalParseTask)
                .where(IntervalParseTask.video_task_id == task.id)
                .order_by(IntervalParseTask.start_sequence_no)
                .with_for_update()
            ))
            by_start = {item.start_sequence_no: item for item in existing_intervals}
            if len(by_start) != len(existing_intervals):
                raise VideoParseConflictError("duplicate IntervalParseTask topology")
            if set(by_start) - {start for start, _count in interval_specs}:
                raise VideoParseConflictError("existing IntervalParseTask topology is incompatible")

            intervals: list[IntervalParseTask] = []
            committed_understanding = {result.chunk_task_id for result in session.scalars(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id.in_([child.id for child in chunk_tasks]),
            )) if result.content}
            for start, count in interval_specs:
                members = chunk_tasks[start:start + count]
                member_ids = [member.id for member in members]
                ready_mask = sum(
                    interval_ready_mask_bit(index, count)
                    for index, member in enumerate(members) if member.id in committed_understanding
                )
                interval = by_start.get(start)
                if interval is None:
                    interval = IntervalParseTask(
                        id=str(uuid4()),
                        chunk_task_id=member_ids[0],
                        video_task_id=task.id,
                        chunk_task_ids=member_ids,
                        start_sequence_no=start,
                        chunk_count=count,
                        ready_mask=ready_mask,
                        status=IntervalParseTaskStatus.WAITING.value,
                        next_execute_time=prepared_at,
                        retry_count=0,
                        lease_version=0,
                        create_time=datetime_to_unix_time_ms(prepared_at),
                        update_time=datetime_to_unix_time_ms(prepared_at),
                    )
                    session.add(interval)
                else:
                    validate_interval_parse_task_members(
                        chunk_parse_task_id=interval.chunk_task_id,
                        video_parse_task_id=task.id,
                        start_sequence_no=start,
                        chunk_count=count,
                        chunk_parse_task_ids=interval.chunk_task_ids,
                        members=[
                            (member, chunk_rows[start + index].sequence_no)
                            for index, member in enumerate(members)
                        ],
                    )
                    if tuple(interval.chunk_task_ids) != tuple(member_ids):
                        raise VideoParseConflictError(
                            "existing IntervalParseTask members are incompatible"
                        )
                    interval.ready_mask = ready_mask
                intervals.append(interval)

            if _activate:
                task.status = VideoParseTaskStatus.PARSING.value
                task.last_error = None
            task.update_time = datetime_to_unix_time_ms(prepared_at)
            task.detail = _ensure_runtime(task.detail)
            video.parse_status = (
                VideoParseTaskStatus.PARSING.value if _activate else task.status
            )
            video.parse_progress = 0
            session.flush()
            return PreparedVideoParseState(
                video_parse_task_id=task.id,
                chunk_parse_task_ids=tuple(item.id for item in chunk_tasks),
                interval_parse_task_ids=tuple(item.id for item in intervals),
            )

    def ensure_video_chunks(
        self,
        lease: VideoParseLease,
        plans: Sequence[tuple[int, int, int]],
        *,
        now: datetime | None = None,
    ) -> tuple[VideoChunkState, ...]:
        """Create the immutable source-time topology while holding Prepare lease."""

        prepared_at = now if now is not None else now_utc()
        normalized = tuple(plans)
        if [item[0] for item in normalized] != list(range(len(normalized))):
            raise ValueError("plans must have consecutive sequence numbers")
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, prepared_at)
            if task.status != VideoParseTaskStatus.PREPARING.value:
                raise VideoParseConflictError("Video Chunk preparation is already complete")
            video = session.get(Video, task.video_id)
            if video is None:
                raise VideoParseConflictError("VideoParseTask Video no longer exists")
            if len(normalized) != task.chunk_count:
                raise VideoParseConflictError("Chunk plan count changed after task creation")
            chunk_duration = _positive_int(task.parse_config.get("chunk_duration"), "chunk_duration") * 1000
            expected = [
                (index, index * chunk_duration,
                 min(video.duration_ms, (index + 1) * chunk_duration))
                for index in range(task.chunk_count)
            ]
            if list(normalized) != expected:
                raise VideoParseConflictError("Chunk plans do not match the frozen parse config")
            existing = {
                item.sequence_no: item
                for item in session.scalars(
                    select(VideoChunk)
                    .where(VideoChunk.video_id == video.id)
                    .order_by(VideoChunk.sequence_no)
                    .with_for_update()
                )
            }
            if set(existing) - set(range(task.chunk_count)):
                raise VideoParseConflictError("Video has an incompatible fixed topology")
            chunks: list[VideoChunk] = []
            for sequence_no, start_seconds, end_seconds in normalized:
                chunk = existing.get(sequence_no)
                if chunk is None:
                    source_ref = video.proxy_uri or video.source_uri
                    chunk = VideoChunk(
                        id=derive_video_chunk_id(video.id, sequence_no),
                        video_id=video.id,
                        sequence_no=sequence_no,
                        start_ms=start_seconds,
                        end_ms=end_seconds,
                        window_start_ms=start_seconds,
                        window_end_ms=end_seconds,
                        chunk_media_ref=source_ref,
                        analysis_window_media_ref=source_ref,
                        transcript=[],
                        baseline_visual_caption=None,
                        keyframe_uris=[],
                        create_time=datetime_to_unix_time_ms(prepared_at),
                        update_time=datetime_to_unix_time_ms(prepared_at),
                    )
                    session.add(chunk)
                elif (chunk.start_ms, chunk.end_ms) != (
                    start_seconds, end_seconds
                ):
                    raise VideoParseConflictError(
                        "existing Video Chunk topology differs from the plan"
                    )
                chunks.append(chunk)
            session.flush()
            return tuple(_video_chunk_state(item) for item in chunks)

    def ensure_parse_tasks(
        self,
        lease: VideoParseLease,
        video_chunk_ids: Sequence[str],
        interval_specs: Sequence[tuple[int, int]],
        *,
        now: datetime | None = None,
    ) -> PreparedVideoParseState:
        expected = _interval_specs(
            len(video_chunk_ids), _interval_size(lease.parse_config)
        )
        if tuple(interval_specs) != expected:
            raise VideoParseConflictError(
                "Interval plans do not match the frozen parse config"
            )
        return self.ensure_prepared(
            lease,
            video_chunk_ids,
            now=now,
            _activate=False,
        )

    def activate_video_task(
        self,
        lease: VideoParseLease,
        *,
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        activated_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, activated_at)
            if task.status != VideoParseTaskStatus.PREPARING.value:
                raise VideoParseConflictError("only PREPARING tasks can be activated")
            chunk_count = len(list(session.scalars(
                select(ChunkParseTask.id).where(
                    ChunkParseTask.video_task_id == task.id
                )
            )))
            interval_count = len(list(session.scalars(
                select(IntervalParseTask.id).where(
                    IntervalParseTask.video_task_id == task.id
                )
            )))
            if chunk_count != task.chunk_count or interval_count != len(
                _interval_specs(task.chunk_count, _interval_size(task.parse_config))
            ):
                raise VideoParseConflictError("VideoParseTask preparation is incomplete")
            task.status = VideoParseTaskStatus.PARSING.value
            task.last_error = None
            task.update_time = datetime_to_unix_time_ms(activated_at)
            video = session.get(Video, task.video_id)
            if video is not None:
                video.parse_status = task.status
                video.parse_progress = 0
            session.flush()
            return _task_state(task)

    def begin_voiceprint_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Freeze the task's eligible Understanding sources under the Video lease."""
        started_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, started_at)
            if parent.is_deleted or _latest_video_task_id(session, parent.video_id) != parent.id:
                raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
            if (parent.parse_config.get("voiceprint") is None
                and parent.parse_config.get("voiceprint_mode", "off") == "off"):
                raise VideoParseConflictError("voiceprint preparation is disabled")
            detail = _ensure_runtime(parent.detail)
            voiceprint = Voiceprint.model_validate(detail.get("voiceprint", {}))
            if voiceprint.ready:
                return None
            if parent.status != VideoParseTaskStatus.PARSING.value:
                raise VideoParseConflictError("voiceprint preparation requires a PARSING task")
            if parent.parse_config.get("voiceprint_mode", "off") != "off":
                return _voiceprint_scoring_snapshot(session, parent, lease, started_at)
            runtime = detail[RUNTIME_DETAIL_KEY]
            phase = dict(runtime.get(_VOICEPRINT_PREPARATION_KEY) or {})
            rows = list(session.execute(select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
                VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
            ).join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
                ChunkParseTask.video_task_id == parent.id,
                VideoChunk.video_id == parent.video_id,
                ChunkParseContent.content.is_not(None),
            ).order_by(VideoChunk.sequence_no)))
            required = voiceprint_preparation_min_chunks(parent.chunk_count)
            if len(rows) < required:
                raise VideoParseConflictError("voiceprint preparation needs three quarters of the Understanding results")
            same_execution = (phase.get("active") is True
                              and phase.get("lease_version") == lease.lease_version
                              and phase.get("worker_id") == lease.worker_id)
            if same_execution:
                selected = set(phase["source_chunk_ids"])
                rows = [row for row in rows if row[1].id in selected]
                if len(rows) != len(selected):
                    raise VideoParseSnapshotConflictError("Voiceprint source snapshot is incomplete")
            else:
                speakers = sorted({event["speaker"] for _task, _chunk, result in rows
                                   for event in dict(result.content or {}).get("timeline", [])
                                   if event.get("event_type") == "DIALOGUE"
                                   and isinstance(event.get("speaker"), str) and event["speaker"].strip()})
                phase.update({"active": True, "lease_version": lease.lease_version,
                              "worker_id": lease.worker_id,
                              "source_chunk_ids": [chunk.id for _task, chunk, _result in rows],
                              "speaker_list": speakers})
                # A failed attempt's partial registrations never become the next snapshot.
                voiceprint = Voiceprint()
            runtime[_VOICEPRINT_PREPARATION_KEY] = phase
            detail["voiceprint"] = voiceprint.model_dump(mode="json")
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(started_at)
            video = session.get(Video, parent.video_id)
            if video is None:
                raise VideoParseConflictError("Voiceprint source Video no longer exists")
            sources = {}
            for _task, chunk, result in rows:
                for event in dict(result.content or {}).get("timeline", []):
                    if (event.get("event_type") != "DIALOGUE"
                        or not isinstance(event.get("speaker"), str) or not event["speaker"].strip()):
                        continue
                    fingerprint = _voiceprint_event_fingerprint(event)
                    sources[f"{chunk.id}:{fingerprint}"] = {
                        "chunk_id": chunk.id, "source_version": result.version,
                        "event_fingerprint": fingerprint, "speaker": event["speaker"],
                        "start_ms": event["start_seconds"], "end_ms": event["end_seconds"],
                        "text_variants": deepcopy(event["text_variants"]),
                    }
            return {
                "member_sequence_nos": {chunk.id: chunk.sequence_no for _task, chunk, _result in rows},
                "member_contents": {chunk.id: deepcopy(result.content) for _task, chunk, result in rows},
                "member_versions": {chunk.id: result.version for _task, chunk, result in rows},
                "member_evidence": {chunk.id: deepcopy(result.detail or {}) for _task, chunk, result in rows},
                "sources": sources, "speaker_list": list(phase["speaker_list"]),
                "source_media_ref": video.source_uri,
                "voiceprint": voiceprint.model_dump(mode="json"),
            }

    def bind_voiceprint_encoder(
        self, lease: VideoParseLease, *, model: Mapping[str, str], now: datetime | None = None,
    ) -> None:
        """Fence the encoder identity across interrupted preparation attempts."""
        bound_at = now if now is not None else now_utc()
        identity = dict(model)
        if (set(identity) != {"name", "encoder_revision"}
            or any(not isinstance(value, str) or not value.strip() for value in identity.values())):
            raise ValueError("voiceprint encoder requires a model name and immutable revision")
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, bound_at)
            detail, phase = _voiceprint_active_preparation(parent, lease)
            if _latest_video_task_id(session, parent.video_id) != parent.id:
                raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
            if phase.get("encoder_model") is not None and phase["encoder_model"] != identity:
                raise VideoParseSnapshotConflictError("Voiceprint encoder revision differs from the frozen task")
            phase["encoder_model"] = identity
            detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(bound_at)

    def finish_voiceprint_scoring(
        self, lease: VideoParseLease, *, preparation: Mapping[str, Any],
        artifact: Mapping[str, str],
        check_active: Callable[[], None] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Publish ready atomically; check_active must only check local cancellation.

        The locked parent fences the lease; a callback renewing it on another
        connection would wait on this transaction's own row lock.
        """
        finished_at = now if now is not None else now_utc()
        payload = deepcopy(dict(preparation))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        descriptor = dict(artifact)
        checksum = hashlib.sha256(encoded).hexdigest()
        if (set(descriptor) != {"location", "checksum"}
            or not isinstance(descriptor.get("location"), str) or not descriptor["location"].strip()
            or descriptor.get("checksum") != checksum):
            raise VideoParseConflictError("voiceprint artifact descriptor or checksum does not match preparation")
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, finished_at)
            if check_active is not None:
                check_active()
            if parent.parse_config.get("voiceprint_mode", "off") == "off":
                raise VideoParseConflictError("voiceprint scoring is disabled")
            detail, phase = _voiceprint_active_preparation(parent, lease)
            if _latest_video_task_id(session, parent.video_id) != parent.id:
                raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
            snapshot = _voiceprint_scoring_snapshot(session, parent, lease, finished_at)
            sources = snapshot["sources"]
            lines = payload.get("lines")
            samples = payload.get("samples")
            model = payload.get("model")
            if (not isinstance(payload.get("method_version"), str) or not payload["method_version"].strip()
                or not isinstance(payload.get("parameters"), dict)
                or not isinstance(model, dict) or not isinstance(model.get("name"), str)
                or not model["name"].strip() or not isinstance(model.get("encoder_revision"), str)
                or not model["encoder_revision"].strip()
                or not isinstance(lines, list) or not isinstance(samples, list)):
                raise VideoParseConflictError("voiceprint scoring result is incomplete")
            if phase.get("encoder_model") is not None and phase["encoder_model"] != model:
                raise VideoParseSnapshotConflictError("Voiceprint result encoder differs from the frozen task")
            line_ids = [line.get("source_id") for line in lines if isinstance(line, dict)]
            if len(line_ids) != len(lines) or len(set(line_ids)) != len(lines) or set(line_ids) != set(sources):
                raise VideoParseConflictError("voiceprint scoring must cover every fixed dialogue exactly once")
            sample_ids = set()
            for sample in samples:
                source_id = sample.get("source_id") if isinstance(sample, dict) else None
                source = sources.get(source_id)
                if source is None or source_id in sample_ids:
                    raise VideoParseConflictError("voiceprint sample is outside the fixed source set or duplicated")
                if sample.get("id") != source_id or "text_variants" in sample:
                    raise VideoParseConflictError("voiceprint samples must use source IDs without transcript copies")
                sample_ids.add(source_id)
                if source["end_ms"] - source["start_ms"] < VOICEPRINT_B0_MIN_AUDIO_MS:
                    raise VideoParseConflictError("voiceprint sample is shorter than the encoder minimum")
                if (sample.get("label") != source["speaker"]
                    or any(sample.get(key) != source[key] for key in (
                        "start_ms", "end_ms", "source_version", "event_fingerprint",
                    )) or sample.get("model") != model["name"]
                    or sample.get("encoder_revision") != model["encoder_revision"]):
                    raise VideoParseSnapshotConflictError("voiceprint sample source identity changed")
                try:
                    validated = VoiceprintReference.model_validate({
                        **{key: source[key] for key in (
                            "chunk_id", "start_ms", "end_ms", "source_version", "event_fingerprint",
                        )},
                        "vector": sample.get("vector"), "pcm_sha256": sample.get("pcm_sha256"),
                        "duration_ms": source["end_ms"] - source["start_ms"],
                        "selection_basis": "Understanding label; B0 leave-one-out reference pool",
                    })
                except ValidationError as error:
                    raise VideoParseConflictError("voiceprint sample has an invalid CAM++ embedding") from error
                if not math.isclose(sum(value * value for value in validated.vector), 1.0,
                                    rel_tol=1e-5, abs_tol=1e-5):
                    raise VideoParseConflictError("voiceprint sample embedding must be L2-normalized")
            for line in lines:
                source = sources[line["source_id"]]
                if "vector" in line or "text_variants" in line:
                    raise VideoParseConflictError("voiceprint scored lines cannot duplicate embeddings or transcripts")
                if any(line.get(key) != source[key] for key in ("start_ms", "end_ms")):
                    raise VideoParseSnapshotConflictError("voiceprint scored interval changed")
                candidates = line.get("candidates")
                if (line.get("duration_ms") != source["end_ms"] - source["start_ms"]
                    or line.get("band") not in {None, "ACCEPT", "CONFLICT", "NO_VOICE"}
                    or not isinstance(line.get("reason"), str) or not line["reason"]
                    or not isinstance(candidates, list) or len(candidates) > 3):
                    raise VideoParseConflictError("voiceprint scored line is incomplete")
                names = []
                for candidate in candidates:
                    if (not isinstance(candidate, dict) or not isinstance(candidate.get("speaker"), str)
                        or not candidate["speaker"].strip()
                        or type(candidate.get("similarity")) not in {int, float}
                        or not -1 <= candidate["similarity"] <= 1):
                        raise VideoParseConflictError("voiceprint candidate must contain a named cosine score")
                    reference_ids = candidate.get("reference_ids")
                    if ("src" in candidate or "vector" in candidate or "text_variants" in candidate
                        or not isinstance(reference_ids, list)
                        or any(not isinstance(reference_id, str) for reference_id in reference_ids)
                        or len(set(reference_ids)) != len(reference_ids)
                        or not set(reference_ids).issubset(sample_ids)
                        or "reference_count" in candidate and candidate["reference_count"] != len(reference_ids)):
                        raise VideoParseConflictError("voiceprint candidates must reference the shared sample pool")
                    names.append(candidate["speaker"])
                if len(set(names)) != len(names) or candidates != sorted(
                    candidates, key=lambda candidate: (-candidate["similarity"], candidate["speaker"]),
                ):
                    raise VideoParseConflictError("voiceprint candidates must rank distinct characters")
                expected_gap = candidates[0]["similarity"] - candidates[1]["similarity"] if len(candidates) > 1 else None
                if (expected_gap is None and line.get("gap") is not None
                    or expected_gap is not None and (type(line.get("gap")) not in {int, float}
                        or not math.isclose(line["gap"], expected_gap, rel_tol=0, abs_tol=1e-9))):
                    raise VideoParseConflictError("voiceprint gap must compare the first two distinct characters")
                if source["end_ms"] - source["start_ms"] < VOICEPRINT_B0_MIN_AUDIO_MS and candidates:
                    raise VideoParseConflictError("voiceprint cannot score audio below the encoder minimum")
            if payload.get("failed_chunks") != snapshot["failed_chunks"]:
                raise VideoParseSnapshotConflictError("voiceprint failed chunk set changed")
            timing = payload.get("timing")
            if not isinstance(timing, dict) or any(timing.get(key) != value
                                                   for key, value in snapshot["timing"].items()):
                raise VideoParseSnapshotConflictError("voiceprint preparation timing does not match its snapshot")
            ready_at_ms = datetime_to_unix_time_ms(finished_at)
            previous_at_ms = snapshot["timing"]["previous_75_percent_at_ms"]
            detail = _ensure_runtime(parent.detail)
            phase = dict(detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY])
            phase["active"] = False
            phase["timing"] = {**timing, "ready_at_ms": ready_at_ms,
                               "ready_minus_previous_75_percent_ms": ready_at_ms - previous_at_ms
                               if previous_at_ms is not None else None}
            detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
            manifest = Voiceprint.model_validate({
                "ready": True, "method_version": payload["method_version"], "model": model,
                "parameters": payload["parameters"], "line_count": len(lines), "sample_count": len(samples),
                **descriptor,
            }).model_dump(mode="json")
            detail["voiceprint"] = manifest
            if check_active is not None:
                check_active()
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(finished_at)
            session.flush()
            if check_active is not None:
                check_active()
            return deepcopy(manifest)

    def load_voiceprint_scoring(
        self, lease: VideoParseLease | IntervalParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read the committed manifest; artifact I/O belongs to the service."""
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            if isinstance(lease, IntervalParseLease):
                interval = _interval_for_lease(session, lease, loaded_at)
                parent = session.get(VideoParseTask, interval.video_task_id)
            else:
                parent = _locked_video_lease(session, lease, loaded_at)
            if (parent is None or parent.is_deleted
                or _latest_video_task_id(session, parent.video_id) != parent.id):
                raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
            voiceprint = dict((parent.detail or {}).get("voiceprint") or {})
            if parent.parse_config.get("voiceprint_mode", "off") == "off" or voiceprint.get("ready") is not True:
                return None
            if "location" not in voiceprint:
                return None
            return Voiceprint.model_validate(voiceprint).model_dump(mode="json")

    def precheck_voiceprint_registration(
        self, lease: VideoParseLease, *, source_ref: str, speaker: str,
        start_ms: int, end_ms: int, model: str, encoder_revision: str,
        replace_source_ref: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        checked_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, checked_at)
            _detail, _voiceprint, _version, existing = _check_voiceprint_registration(
                session, parent, lease, source_ref=source_ref, speaker=speaker,
                start_ms=start_ms, end_ms=end_ms, model=model, encoder_revision=encoder_revision,
                replace_source_ref=replace_source_ref,
            )
            return existing

    def register_voiceprint(
        self, lease: VideoParseLease, *, source_ref: str, speaker: str,
        start_ms: int, end_ms: int, selection_basis: str, confidence: float | None,
        model: str, encoder_revision: str, vector: Sequence[float], pcm_sha256: str,
        duration_ms: int, replace_source_ref: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        registered_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, registered_at)
            detail, voiceprint, source_version, existing = _check_voiceprint_registration(
                session, parent, lease, source_ref=source_ref, speaker=speaker,
                start_ms=start_ms, end_ms=end_ms, model=model, encoder_revision=encoder_revision,
                replace_source_ref=replace_source_ref,
            )
            if existing is not None:
                return existing
            if any(ref.pcm_sha256 == pcm_sha256 for item in voiceprint.voiceprintList for ref in item.references):
                raise VoiceprintRegistrationRejectedError(
                    "VOICEPRINT_DUPLICATE_AUDIO", "The same PCM audio is already registered in this preparation.",
                    "Choose another independent recording; a new source_ref does not make identical audio a new case.",
                )
            chunk_id, _, fingerprint = source_ref.partition(":")
            reference = VoiceprintReference(
                chunk_id=chunk_id, start_ms=start_ms, end_ms=end_ms, vector=list(vector),
                source_version=source_version, event_fingerprint=fingerprint,
                pcm_sha256=pcm_sha256, duration_ms=duration_ms, selection_basis=selection_basis,
            )
            record = next((item for item in voiceprint.voiceprintList if item.speaker == speaker
                           and item.model.name == model and item.model.encoder_revision == encoder_revision), None)
            if record is None:
                # 首版不推断同人异名：保留 Agent 的人物标签，避免额外身份归一流程。
                record = CharacterVoiceprint(speaker=speaker, model={"name": model, "encoder_revision": encoder_revision},
                                             references=[], confidence=confidence)
                voiceprint.voiceprintList.append(record)
            record.references.append(reference)
            detail["voiceprint"] = voiceprint.model_dump(mode="json")
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(registered_at)
            count = len({(ref.chunk_id, ref.event_fingerprint) for item in voiceprint.voiceprintList
                         if item.speaker == speaker for ref in item.references})
            receipt = _voiceprint_registration_receipt(record, reference, refs_count=count, added=True)
            if replace_source_ref is not None:
                receipt["replaced_source_ref"] = replace_source_ref
            return receipt

    def unregister_voiceprint(
        self, lease: VideoParseLease, *, source_ref: str, now: datetime | None = None,
    ) -> dict[str, Any]:
        removed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, removed_at)
            detail, phase = _voiceprint_active_preparation(parent, lease)
            chunk_id, separator, _fingerprint = source_ref.partition(":")
            if not separator or chunk_id not in phase["source_chunk_ids"]:
                raise VoiceprintRegistrationRejectedError(
                    "INVALID_VOICEPRINT_SOURCE", "Source is outside this preparation.",
                    "Use a registered source_ref returned by get_speaker_list.",
                )
            voiceprint = Voiceprint.model_validate(detail["voiceprint"])
            for record in voiceprint.voiceprintList:
                reference = next((ref for ref in record.references
                                  if f"{ref.chunk_id}:{ref.event_fingerprint}" == source_ref), None)
                if reference is None:
                    continue
                record.references.remove(reference)
                if not record.references:
                    voiceprint.voiceprintList.remove(record)
                detail["voiceprint"] = voiceprint.model_dump(mode="json")
                parent.detail = detail
                parent.update_time = datetime_to_unix_time_ms(removed_at)
                count = sum(len(item.references) for item in voiceprint.voiceprintList if item.speaker == record.speaker)
                return {"removed": True, "speaker": record.speaker, "source_ref": source_ref,
                        "refs_count": count, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER}
            return {"removed": False, "speaker": None, "source_ref": source_ref,
                    "refs_count": None, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER}

    def get_voiceprint_speaker_list(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, loaded_at)
            detail = _ensure_runtime(parent.detail)
            voiceprint = Voiceprint.model_validate(detail["voiceprint"])
            phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
            if not voiceprint.ready:
                detail, phase = _voiceprint_active_preparation(parent, lease)
            references = _valid_voiceprint_references(session, parent, voiceprint, for_update=False)
            speakers = set(phase["speaker_list"])
            progress = _voiceprint_reference_progress(references, speakers)
            registered = {speaker for speaker, refs in progress.items() if refs}
            rows = session.execute(select(VideoChunk.id, ChunkParseContent.content).join(
                ChunkParseTask, ChunkParseTask.chunk_id == VideoChunk.id,
            ).join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
                ChunkParseTask.video_task_id == parent.id, VideoChunk.video_id == parent.video_id,
                VideoChunk.id.in_(phase["source_chunk_ids"]),
            ).order_by(VideoChunk.sequence_no))
            by_speaker = {speaker: [] for speaker in sorted(speakers)}
            for chunk_id, content in rows:
                for speaker in {event.get("speaker") for event in dict(content or {}).get("timeline", [])
                                if event.get("event_type") == "DIALOGUE"
                                and isinstance(event.get("speaker"), str) and event["speaker"].strip()} & speakers:
                    by_speaker[speaker].append(chunk_id)
            return {"speaker_list": [{"speaker": speaker, "chunk_ids": chunk_ids,
                                      "refs_count": len(progress[speaker]),
                                      "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
                                      "references": progress[speaker]}
                                     for speaker, chunk_ids in by_speaker.items()],
                    "speaker_list_size": len(speakers), "registered_speaker_count": len(registered),
                    "minimum_speaker_count": len(speakers) // 2 + 1 if speakers else 0,
                    "missing_speakers": sorted(speakers - registered)}

    def finish_voiceprint_preparation(
        self, lease: VideoParseLease, *, skip_reasons: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        finished_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, finished_at)
            detail = _ensure_runtime(parent.detail)
            voiceprint = Voiceprint.model_validate(detail.get("voiceprint", {}))
            phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
            if not voiceprint.ready:
                detail, phase = _voiceprint_active_preparation(parent, lease)
            references = _valid_voiceprint_references(session, parent, voiceprint, for_update=True)
            speakers = set(phase.get("speaker_list", []))
            progress = _voiceprint_reference_progress(references, speakers)
            counts = {speaker: len(refs) for speaker, refs in progress.items()}
            registered = {speaker for speaker, count in counts.items() if count}
            missing = sorted(speakers - registered)
            reasons = {name: reason.strip() for name, reason in dict(skip_reasons or {}).items()
                       if name in counts and counts[name] < VOICEPRINT_REFS_PER_SPEAKER
                       and isinstance(reason, str) and reason.strip()}
            missing_reasons = sorted(name for name, count in counts.items()
                                     if count < VOICEPRINT_REFS_PER_SPEAKER and name not in reasons)
            result = {"registered_speaker_count": len(registered), "speaker_list_size": len(speakers),
                      "minimum_speaker_count": len(speakers) // 2 + 1 if speakers else 0,
                      "missing_speakers": missing,
                      "skip_reasons": reasons, "reference_counts": counts,
                      "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
                      "missing_reason_speakers": [] if voiceprint.ready else missing_reasons}
            if not voiceprint.ready:
                if len(references) != sum(len(record.references) for record in voiceprint.voiceprintList):
                    raise VideoParseSnapshotConflictError("Voiceprint registered source identity changed")
                result.update(_voiceprint_consistency(references, speakers))
                if result["suspect_references"]:
                    return {"ready": False, **result,
                            "message": "Some references are acoustically inconsistent with their speaker group. "
                                       "Review suspect_references and explicitly replace or unregister them, then retry. "
                                       "skip_reasons cannot bypass these references; cosine is not identity probability."}
                if speakers and len(registered) * 2 <= len(speakers):
                    return {"ready": False, **result,
                            "message": "Register valid references for strictly more than half the speaker list."}
                if missing_reasons:
                    return {"ready": False, **result,
                            "message": f"Aim for {VOICEPRINT_REFS_PER_SPEAKER} independent references per speaker, "
                                       "or explain each shortfall in skip_reasons."}
            voiceprint.ready = True
            phase["active"] = False
            detail["voiceprint"] = voiceprint.model_dump(mode="json")
            detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(finished_at)
            return {"ready": True, **result, "message": "Voiceprint preparation is ready."}

    def stop_voiceprint_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> bool:
        """Normal cancellation returns the phase without charging a failure."""
        stopped_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_video_lease(session, lease, stopped_at)
            detail = _ensure_runtime(parent.detail)
            phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
            if phase.get("lease_version") != lease.lease_version or phase.get("worker_id") != lease.worker_id:
                return False
            phase["active"] = False
            detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
            parent.detail = detail
            return True

    def load_voiceprint_references(
        self, lease: IntervalParseLease, *, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            interval = _interval_for_lease(session, lease, loaded_at)
            parent = session.get(VideoParseTask, interval.video_task_id)
            voiceprint = Voiceprint.model_validate(dict(parent.detail or {}).get("voiceprint", {}))
            if not voiceprint.ready:
                return []
            return _valid_voiceprint_references(session, parent, voiceprint, for_update=False)

    def commit_chunk_result(
        self,
        lease: ChunkParseLease,
        *,
        content: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        committed_at = now if now is not None else now_utc()
        payload = deepcopy(dict(content))
        if not payload:
            raise ValueError("content must not be empty")
        with self._session_factory.begin() as session:
            intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            task = _locked_chunk_lease(session, lease, committed_at)
            if lease.phase != ChunkTaskPhase.PARSE:
                raise VideoParseLeaseLostError("only Parse leases can commit understanding")
            chunk = session.get(VideoChunk, task.chunk_id)
            if chunk is None:
                raise VideoParseConflictError("ChunkParseTask VideoChunk no longer exists")
            payload = validate_result_time_range(
                VideoParseResult.model_validate(payload),
                start_seconds=chunk.window_start_ms,
                end_seconds=chunk.window_end_ms,
            ).model_dump(mode="json")
            result = session.scalar(
                select(ChunkParseContent)
                .where(ChunkParseContent.chunk_task_id == task.id)
                .with_for_update()
            )
            result_detail = deepcopy(dict(detail or {}))
            result_detail["scope"] = [chunk.sequence_no]
            result_detail["parse_committed_lease_version"] = lease.lease_version
            if result is None:
                result = ChunkParseContent(
                    id=str(uuid4()),
                    chunk_task_id=task.id,
                    content=payload,
                    version=1,
                    detail=result_detail,
                    create_time=datetime_to_unix_time_ms(committed_at),
                    update_time=datetime_to_unix_time_ms(committed_at),
                )
                session.add(result)
            else:
                # A valid lease can only reach this branch after an explicitly
                # recovered incomplete commit. Never replace a COMPLETE result.
                result.content = payload
                result.detail = result_detail
                result.update_time = datetime_to_unix_time_ms(committed_at)

            touched: list[str] = []
            for interval in intervals:
                if task.id not in interval.chunk_task_ids:
                    continue
                index = interval.chunk_task_ids.index(task.id)
                was_ready = (
                    interval.ready_mask
                    == interval_full_ready_mask(interval.chunk_count)
                )
                interval.ready_mask |= interval_ready_mask_bit(index, interval.chunk_count)
                if (
                    not was_ready
                    and interval.ready_mask
                    == interval_full_ready_mask(interval.chunk_count)
                ):
                    # `next_execute_time` doubles as the durable ready-at
                    # ordering key. An Interval that becomes ready later must
                    # not be hidden behind old, incomplete windows.
                    interval.next_execute_time = committed_at
                interval.update_time = datetime_to_unix_time_ms(committed_at)
                touched.append(interval.id)

            task.phase = ChunkTaskPhase.REFINE
            task.status = ChunkParseTaskStatus.WAITING.value
            task.retry_count = 0
            task.finish_time = None
            task.lease_owner = None
            task.lease_expire_time = None
            task.last_error = None
            task.update_time = datetime_to_unix_time_ms(committed_at)
            session.flush()
            parent = session.get(VideoParseTask, task.video_task_id)
            runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
            if runtime.get("realtime_only") and not runtime.get("accepted"):
                if not session.scalar(select(ChunkParseTask.id).where(
                    ChunkParseTask.video_task_id == parent.id,
                    ChunkParseTask.lease_owner.is_not(None),
                ).limit(1)) and parent.lease_owner is None:
                    parent.capacity_acquired = False
            return tuple(touched)

    def load_chunk_execution(
        self,
        lease: ChunkParseLease,
        *,
        now: datetime | None = None,
    ) -> ChunkExecutionState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            task = session.get(ChunkParseTask, lease.chunk_parse_task_id)
            if not _matching_chunk_lease(task, lease, loaded_at):
                raise VideoParseLeaseLostError(
                    "ChunkParseTask lease is no longer current"
                )
            row = session.execute(
                select(
                    VideoChunk,
                    VideoParseTask,
                    Video,
                    Series,
                )
                .select_from(VideoChunk)
                .join(
                    VideoParseTask,
                    VideoParseTask.id == task.video_task_id,
                )
                .join(Video, Video.id == VideoParseTask.video_id)
                .join(Series, Series.id == Video.series_id)
                .where(VideoChunk.id == task.chunk_id)
            ).one_or_none()
            if row is None:
                raise VideoParseConflictError("Chunk execution identity is incomplete")
            chunk, parent, video, series = row
            source_media_ref = (
                video.proxy_uri or video.source_uri
            )
            return ChunkExecutionState(
                chunk_parse_task_id=task.id,
                video_parse_task_id=task.video_task_id,
                video_chunk_id=chunk.id,
                sequence_no=chunk.sequence_no,
                start_seconds=chunk.start_ms,
                end_seconds=chunk.end_ms,
                analysis_window_start_seconds=chunk.window_start_ms,
                analysis_window_end_seconds=chunk.window_end_ms,
                source_media_ref=source_media_ref,
                chunk_media_ref=chunk.chunk_media_ref,
                analysis_window_media_ref=chunk.analysis_window_media_ref,
                task_input=_task_input_state(
                    parent, video, tenant_id=series.tenant_id, series=series
                ),
            )

    def load_interval_members(
        self,
        lease: IntervalParseLease,
        *,
        now: datetime | None = None,
    ) -> IntervalMemberSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            interval = _interval_for_lease(session, lease, loaded_at)
            rows = _member_content_rows(session, interval, for_update=False)
            return IntervalMemberSnapshot(
                interval_parse_task_id=interval.id,
                video_parse_task_id=interval.video_task_id,
                start_sequence_no=interval.start_sequence_no,
                chunk_count=interval.chunk_count,
                members=tuple(_content_state(*row) for row in rows),
            )

    def commit_interval_refinement(
        self,
        lease: IntervalParseLease,
        *,
        interval_content: Mapping[str, Any],
        member_updates: Mapping[str, Mapping[str, Any]],
        input_versions: Mapping[str, int],
        input_fingerprints: Mapping[str, str],
        input_contents: Mapping[str, Mapping[str, Any]],
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Merge independent event edits under current leases and commit once."""

        committed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            locked_intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            interval = _locked_interval_lease(
                session, lease, committed_at, IntervalParseTaskStatus.REFINING.value
            )
            if session.get(IntervalParseContent, interval.id) is not None:
                raise VideoParseConflictError("Interval refinement was already committed")
            unexpected = set(member_updates) - set(interval.chunk_task_ids)
            if unexpected:
                raise VideoParseConflictError("refinement updates a nonmember Chunk")

            rows = _member_content_rows(session, interval, for_update=True)
            expected_members = set(interval.chunk_task_ids)
            if (set(input_versions) != expected_members or set(input_fingerprints) != expected_members
                    or set(input_contents) != expected_members):
                raise VideoParseConflictError("refinement input identity must cover every Interval member")
            voiceprint_decisions = deepcopy(dict(detail or {}).get("voiceprint_decisions", []))
            if any(record["member_chunk_id"] not in expected_members for record in voiceprint_decisions):
                raise VideoParseConflictError("voice decision refers to a nonmember Chunk")
            for chunk_task, _chunk, result in rows:
                version = input_versions[chunk_task.id]
                if (type(version) is not int or version < 1
                        or parse_content_fingerprint(input_contents[chunk_task.id]) != input_fingerprints[chunk_task.id]):
                    raise VideoParseConflictError("Refine base content does not match its input identity")
                if (result.version < version or result.version == version
                        and parse_content_fingerprint(dict(result.content or {})) != input_fingerprints[chunk_task.id]):
                    raise VideoParseSnapshotConflictError(
                        "Refine member content snapshot changed", reason="member_snapshot_changed",
                        context={"chunk_task_id": chunk_task.id, "chunk_id": chunk_task.chunk_id,
                                 "base_version": version, "current_version": result.version,
                                 "base_fingerprint": input_fingerprints[chunk_task.id],
                                 "current_fingerprint": parse_content_fingerprint(dict(result.content or {}))},
                    )
            consulted_scope = []
            for _chunk_task, chunk, result in rows:
                consulted_scope = _ordered_int_union(
                    consulted_scope, _ordered_int_union(dict(result.detail or {}).get("scope"), [chunk.sequence_no]),
                )
            parent = session.get(VideoParseTask, interval.video_task_id)
            video = session.get(Video, parent.video_id) if parent is not None else None
            if video is None or consulted_scope[0] < 0 or consulted_scope[-1] >= parent.chunk_count:
                raise VideoParseConflictError("refinement scope exceeds its fixed Video topology")
            chunk_duration = parent.parse_config.get("chunk_duration")
            if type(chunk_duration) is not int or chunk_duration <= 0:
                raise VideoParseConflictError("frozen parse config has an invalid chunk_duration")
            chunk_duration_ms = chunk_duration * 1000
            start_ms = consulted_scope[0] * chunk_duration_ms
            end_ms = min(video.duration_ms, (consulted_scope[-1] + 1) * chunk_duration_ms)
            candidates = {}
            for chunk_task, chunk, result in rows:
                base = deepcopy(dict(input_contents[chunk_task.id]))
                current = deepcopy(dict(result.content or {}))
                changed = member_updates.get(chunk_task.id, {})
                if set(changed) - {"persons", "timeline"}:
                    raise VideoParseConflictError("refinement can only commit persons and timeline")
                proposed = validate_result_time_range(
                    VideoParseResult.model_validate({**base, **deepcopy(dict(changed))}),
                    start_seconds=start_ms, end_seconds=end_ms,
                ).model_dump(mode="json")
                try:
                    merged = merge_refinement_content(base, current, proposed)
                    candidates[chunk_task.id] = validate_result_time_range(
                        VideoParseResult.model_validate(merged), start_seconds=start_ms, end_seconds=end_ms,
                    ).model_dump(mode="json")
                except (RefinementMergeConflictError, ValidationError) as error:
                    context = {"chunk_task_id": chunk_task.id, "chunk_id": chunk.id,
                               "sequence_no": chunk.sequence_no, "base_version": input_versions[chunk_task.id],
                               "current_version": result.version}
                    if isinstance(error, RefinementMergeConflictError):
                        context.update(error.context)
                        reason = error.reason
                        message = str(error)
                    else:
                        reason = "merged_validation_failed"
                        message = "Merged Refine result failed validation"
                        context["validation_errors"] = [{"location": list(item["loc"]), "type": item["type"]}
                                                        for item in error.errors(include_input=False)]
                    raise VideoParseSnapshotConflictError(
                        f"Refine concurrent event edits conflict: {message}", reason=reason, context=context,
                    ) from error
            for record in voiceprint_decisions:
                matching = [event for event in candidates[record["member_chunk_id"]]["timeline"]
                            if event["event_type"] == "DIALOGUE" and (
                                event["start_seconds"], event["end_seconds"]
                            ) == (record["start_ms"], record["end_ms"])]
                if len(matching) != 1 or matching[0]["speaker"] != record["final_speaker"]:
                    raise VideoParseSnapshotConflictError(
                        "Merged Refine speaker contradicts its voice decision",
                        reason="voice_decision_merge_conflict",
                        context={"chunk_task_id": record["member_chunk_id"],
                                 "event_ref": record["event_ref"]},
                    )
            member_scope = list(
                range(
                    interval.start_sequence_no,
                    interval.start_sequence_no + interval.chunk_count,
                )
            )
            for chunk_task, _chunk, result in rows:
                current = deepcopy(dict(result.content or {}))
                updated = candidates[chunk_task.id]
                content_changed = updated != current
                if content_changed:
                    result.content = updated
                    result.version += 1
                current_detail = deepcopy(dict(result.detail or {}))
                current_detail["scope"] = _ordered_int_union(
                    current_detail.get("scope"), member_scope
                )
                decisions = [record for record in voiceprint_decisions
                             if record["member_chunk_id"] == chunk_task.id]
                if decisions:
                    current_detail["voiceprint_decisions"] = [
                        *current_detail.get("voiceprint_decisions", []),
                        *[{**record, "interval_parse_task_id": interval.id} for record in decisions],
                    ]
                result.detail = current_detail
                result.update_time = datetime_to_unix_time_ms(committed_at)

            session.add(IntervalParseContent(
                interval_task_id=interval.id,
                content=deepcopy(dict(interval_content)),
                detail={
                    **deepcopy(dict(detail or {})),
                    "refinement_committed_lease_version": lease.lease_version,
                },
                create_time=datetime_to_unix_time_ms(committed_at),
                update_time=datetime_to_unix_time_ms(committed_at),
            ))
            interval.status = IntervalParseTaskStatus.COMPLETE.value
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.last_error = None
            interval.finish_time = committed_at
            interval.next_execute_time = committed_at
            interval.update_time = datetime_to_unix_time_ms(committed_at)

            session.flush()
            queued = _advance_ready_chunks(
                session, interval.video_task_id, interval.chunk_task_ids,
                committed_at, locked_intervals=locked_intervals,
            )
            return tuple(queued)

    def bind_event_index_target(
        self, lease: ChunkParseLease, configured_target: Mapping[str, Any],
        *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Freeze one Event build target at the first leased INDEX execution."""

        target = _normalize_event_index_target(configured_target)
        bound_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_parent_task(session, lease.video_parse_task_id)
            _locked_chunk_lease(session, lease, bound_at)
            if lease.phase != ChunkTaskPhase.INDEX:
                raise VideoParseConflictError("Event target requires an INDEX lease")
            detail = deepcopy(dict(parent.detail or {}))
            existing = detail.get(EVENT_INDEX_TARGET_DETAIL_KEY)
            if existing is not None:
                return _normalize_event_index_target(existing)
            detail[EVENT_INDEX_TARGET_DETAIL_KEY] = target
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(bound_at)
            return deepcopy(target)

    def load_chunk_indexing_snapshot(
        self, lease: ChunkParseLease, *, event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ChunkIndexingSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            child = session.get(ChunkParseTask, lease.chunk_parse_task_id)
            if lease.phase != ChunkTaskPhase.INDEX or not _matching_chunk_lease(child, lease, loaded_at):
                raise VideoParseLeaseLostError("Chunk Index lease is no longer current")
            parent = session.get(VideoParseTask, lease.video_parse_task_id)
            video = session.get(Video, parent.video_id) if parent is not None else None
            series = session.get(Series, video.series_id) if video is not None else None
            chunk = session.get(VideoChunk, child.chunk_id)
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == child.id,
            ))
            if parent is None or parent.is_deleted or video is None or series is None or chunk is None or content is None:
                raise VideoParseConflictError("Chunk Index identity or content is incomplete")
            if event_target is not None:
                _require_event_index_target(parent, event_target)
            covering = _submitted_intervals_by_member(session, parent.id).get(child.id, ())
            refined = set(session.scalars(select(IntervalParseContent.interval_task_id).where(
                IntervalParseContent.interval_task_id.in_([item.id for item in covering]),
            )))
            if not chunk_index_ready(bool(content.content), [
                item.status == IntervalParseTaskStatus.COMPLETE.value and item.id in refined
                for item in covering
            ]):
                raise VideoParseConflictError("Chunk Index dependencies are incomplete")
            return ChunkIndexingSnapshot(
                video_parse_task_id=parent.id,
                target_index_generation=parent.target_index_generation,
                chunk=IndexableChunkState(
                    chunk_parse_task_id=child.id, video_chunk_id=chunk.id,
                    sequence_no=chunk.sequence_no, content=deepcopy(dict(content.content or {})),
                    content_version=content.version, content_detail=deepcopy(dict(content.detail or {})),
                    source_media_ref=video.proxy_uri or video.source_uri,
                    chunk_media_ref=chunk.chunk_media_ref,
                    analysis_window_media_ref=chunk.analysis_window_media_ref,
                    tenant_id=series.tenant_id, series_id=series.id, video_id=video.id,
                    start_seconds=chunk.start_ms, end_seconds=chunk.end_ms,
                ),
            )

    def complete_chunk_indexing(
        self, lease: ChunkParseLease, content_version: int, index_generation: int,
        *, event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        """Record a publication only after the host proves its exact ES visibility."""
        if type(content_version) is not int or content_version < 1 or type(index_generation) is not int or index_generation < 1:
            raise VideoParseConflictError("indexed Chunk versions must be positive integers")
        completed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_parent_task(session, lease.video_parse_task_id)
            child = _locked_chunk_lease(session, lease, completed_at)
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == child.id,
            ).with_for_update())
            if lease.phase != ChunkTaskPhase.INDEX or content is None or content.version != content_version:
                raise VideoParseConflictError("indexed Chunk content or phase changed")
            if parent.target_index_generation != index_generation:
                raise VideoParseConflictError("indexed Chunk target generation changed")
            _require_event_index_target(parent, event_target)
            receipt = _index_receipt(content, parent.id, child.chunk_id)
            if receipt["index_generation"] != index_generation:
                raise VideoParseConflictError("indexed Chunk checkpoint generation changed")
            if event_target is not None:
                _require_event_publication(session, parent, child, content, receipt, event_target)
            detail = deepcopy(dict(content.detail or {}))
            committed = list(detail.get("index_committed_lease_versions", []))
            if lease.lease_version not in committed:
                committed.append(lease.lease_version)
            detail["index_committed_lease_versions"] = committed
            content.detail = detail
            child.indexed_content_version = content_version
            child.status = ChunkParseTaskStatus.COMPLETE.value
            child.finish_time = completed_at
            child.lease_owner = None
            child.lease_expire_time = None
            child.last_error = None
            child.update_time = datetime_to_unix_time_ms(completed_at)
            session.flush()
            # The queue can also finish this aggregation if the coordinator exits.
            _aggregate_video_task(session, parent.id, completed_at)
            return _task_state(parent)

    def save_index_embeddings(
        self,
        lease: ChunkParseLease,
        chunk_parse_task_id: str,
        content_version: int,
        representations: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
        event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Persist reusable encoding results behind the current index lease."""

        if (
            isinstance(content_version, bool)
            or not isinstance(content_version, int)
            or content_version <= 0
        ):
            raise ValueError("content_version must be a positive integer")
        saved_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            parent = _locked_parent_task(session, lease.video_parse_task_id)
            child = _locked_chunk_lease(session, lease, saved_at)
            _require_event_index_target(parent, event_target)
            if lease.phase != ChunkTaskPhase.INDEX or child.id != chunk_parse_task_id:
                raise VideoParseConflictError("embedding target is not the leased Index Chunk")
            content = session.scalar(
                select(ChunkParseContent)
                .where(ChunkParseContent.chunk_task_id == chunk_parse_task_id)
                .with_for_update()
            )
            if content is None or content.version != content_version:
                raise VideoParseConflictError("embedding target content version changed")
            detail = deepcopy(dict(content.detail or {}))
            cached = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
            cached_representations: dict[str, Any] = {}
            selected_receipt: dict[str, Any] | None = None
            if receipt is not None:
                child = session.get(ChunkParseTask, chunk_parse_task_id)
                if child is None or child.video_task_id != lease.video_parse_task_id:
                    raise VideoParseConflictError("embedding target belongs to a different task")
                selected_receipt = _validate_index_receipt(
                    receipt, lease.video_parse_task_id, child.chunk_id, content_version
                )
                if selected_receipt["index_generation"] != parent.target_index_generation:
                    raise VideoParseConflictError("embedding target generation changed")
                try:
                    cached_receipt = _index_receipt(
                        content, lease.video_parse_task_id, child.chunk_id
                    )
                except VideoParseConflictError:
                    cached_receipt = None
                if (
                    cached_receipt is not None
                    and cached_receipt["index_generation"] == selected_receipt["index_generation"]
                    and isinstance(cached, Mapping)
                    and isinstance(cached.get("representations"), Mapping)
                ):
                    cached_representations = deepcopy(dict(cached["representations"]))
            incoming = deepcopy(dict(representations))
            if "events" in incoming:
                if not isinstance(incoming["events"], Mapping):
                    raise VideoParseConflictError("Event encoding cache must be an object")
                old_events = cached_representations.get("events", {})
                incoming["events"] = {
                    **(dict(old_events) if isinstance(old_events, Mapping) else {}),
                    **dict(incoming["events"]),
                }
            cached_representations.update(incoming)
            envelope: dict[str, Any] = {
                "content_version": content_version,
                "representations": cached_representations,
            }
            if selected_receipt is not None:
                envelope["receipt"] = selected_receipt
            detail[INDEX_EMBEDDINGS_DETAIL_KEY] = envelope
            content.detail = detail
            content.update_time = datetime_to_unix_time_ms(saved_at)

    def request_index_rebuild(
        self, video_parse_task_id: str, *, event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Requeue released Chunks while preserving Parse and Refine results."""
        requested_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = _locked_parent_task(session, video_parse_task_id)
            intervals = _locked_parent_intervals(session, task.id)
            chunks = list(session.scalars(select(ChunkParseTask).where(
                ChunkParseTask.video_task_id == task.id,
            ).order_by(ChunkParseTask.id).with_for_update()))
            if _latest_video_task_id(session, task.video_id) != task.id:
                raise VideoParseConflictError("only the latest VideoParseTask can rebuild its index")
            if event_target is not None:
                detail = deepcopy(dict(task.detail or {}))
                detail[EVENT_INDEX_TARGET_DETAIL_KEY] = _normalize_event_index_target(event_target)
                task.detail = detail
            failure = _video_failure_status(task) or (
                task.status if task.status in {"PARSING_FAILED", "INDEXING_FAILED"} else None
            )
            if failure and (failure != "INDEXING_FAILED" or task.status != "INDEXING_FAILED"
                            or any(child.lease_owner is not None for child in [*chunks, *intervals])):
                raise VideoParseConflictError(
                    "index rebuild requires a settled indexing failure; parsing failures require retry"
                )
            # Repeating the same request while already queued must not reset its budget.
            targets = [child.id for child in chunks if child.phase == ChunkTaskPhase.INDEX and child.status != "QUEUED"]
            reset = _advance_ready_chunks(
                session, task.id, targets, requested_at,
                locked_intervals=intervals, invalidate_index=True,
            )
            if reset and failure:
                detail = _ensure_runtime(task.detail)
                detail[RUNTIME_DETAIL_KEY].pop("failure_status", None)
                task.detail = detail
                task.status = VideoParseTaskStatus.PARSING.value
            _aggregate_video_task(session, task.id, requested_at)
            if reset:
                task.next_execute_time = requested_at
                task.last_error = None
            return reset

    def aggregate_task(
        self,
        lease: VideoParseLease,
        *,
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        aggregated_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            _locked_video_lease(session, lease, aggregated_at)
            task = _aggregate_video_task(
                session,
                lease.video_parse_task_id,
                aggregated_at,
                release_terminal_lease=True,
            )
            session.flush()
            return _task_state(task)

    def record_execution_timing(
        self,
        lease: ChunkParseLease | IntervalParseLease,
        *,
        started_at_ms: int,
        elapsed_ms: int,
        finished: bool = False,
        outcome: str = "running",
        now: datetime | None = None,
    ) -> bool:
        """Sample one fenced parsing/indexing execution without changing its task state."""

        for name, value in (("started_at_ms", started_at_ms), ("elapsed_ms", elapsed_ms)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(finished) is not bool:
            raise ValueError("finished must be a boolean")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("outcome must be nonempty text")
        if isinstance(lease, ChunkParseLease):
            phase = "understand" if lease.phase == ChunkTaskPhase.PARSE else "index"
            kind, task_id = "chunk", lease.chunk_parse_task_id
            model = ChunkParseTask
        elif isinstance(lease, IntervalParseLease) and lease.phase == "REFINING":
            phase = "refine"
            kind, task_id = "interval", lease.interval_parse_task_id
            model = IntervalParseTask
        else:
            raise ValueError("timing supports only Chunk Parse/Index and Interval Refine")

        sampled_at = now if now is not None else now_utc()
        identity = {
            "phase": phase, "execution_kind": kind, "execution_task_id": task_id,
            "lease_version": lease.lease_version, "worker_id": lease.worker_id,
            "started_at_ms": started_at_ms,
        }
        key = f"{phase}:{task_id}:{lease.lease_version}"
        with self._session_factory.begin() as session:
            parent = _locked_parent_task(session, lease.video_parse_task_id)
            child = session.scalar(
                select(model).where(model.id == task_id).with_for_update()
            )
            if child is None or child.video_task_id != lease.video_parse_task_id:
                return False
            if isinstance(lease, ChunkParseLease):
                active = _matching_chunk_lease(child, lease, sampled_at)
                result = session.scalar(select(ChunkParseContent).where(
                    ChunkParseContent.chunk_task_id == child.id,
                ))
                receipt = result.detail if result is not None and isinstance(result.detail, dict) else {}
                committed = child.chunk_id == lease.video_chunk_id and (
                    receipt.get("parse_committed_lease_version") == lease.lease_version
                    if lease.phase == ChunkTaskPhase.PARSE else lease.lease_version in receipt.get("index_committed_lease_versions", [])
                )
            else:
                active = _matching_interval_lease(child, lease, sampled_at)
                content = session.get(IntervalParseContent, task_id) if finished else None
                receipt = content.detail if content is not None and isinstance(content.detail, dict) else {}
                committed_version = receipt.get("refinement_committed_lease_version") == lease.lease_version
                committed = bool(
                    committed_version
                    and tuple(child.chunk_task_ids) == lease.chunk_parse_task_ids
                    and child.start_sequence_no == lease.start_sequence_no
                    and child.chunk_count == lease.chunk_count
                )
            if not active and not (finished and committed):
                return False
            detail = deepcopy(dict(parent.detail or {}))
            runtime = detail.get(RUNTIME_DETAIL_KEY, {})
            if not isinstance(runtime, dict):
                return False
            timing = runtime.get("execution_timing", {"version": 1, "entries": {}})
            if not isinstance(timing, dict) or timing.get("version") != 1:
                return False
            entries = timing.get("entries")
            if not isinstance(entries, dict):
                return False
            entry = entries.get(key)
            if entry is None:
                # A completion receipt may close an existing sample, never
                # invent a start for work that was not observed while active.
                if not active:
                    return False
                entry = {**identity, "elapsed_ms": 0, "finished": False, "outcome": "running"}
            elif not isinstance(entry, dict) or any(
                entry.get(name) != value for name, value in identity.items()
            ):
                return False
            if entry.get("finished") is True:
                return True
            if type(entry.get("elapsed_ms")) is not int or entry["elapsed_ms"] < 0:
                return False
            entries[key] = {
                **entry, "elapsed_ms": max(entry["elapsed_ms"], elapsed_ms),
                "finished": finished, "outcome": outcome,
            }
            timing["entries"] = entries
            runtime["execution_timing"] = timing
            detail[RUNTIME_DETAIL_KEY] = runtime
            parent.detail = detail
            parent.update_time = datetime_to_unix_time_ms(sampled_at)
            return True

    def reserve_model_call(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ModelCallReservation:
        if not call_id.strip():
            raise ValueError("call_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        reserved_at = now if now is not None else now_utc()
        expires_at = reserved_at + lease_duration
        now_ms = datetime_to_unix_time_ms(reserved_at)
        identity = _model_execution_identity(lease)
        with self._session_factory.begin() as session:
            task = _lock_model_execution_lease(session, lease, reserved_at)
            execution = _execution_config(task.parse_config)
            # Keep the legacy snapshot value for reporting; cumulative calls do not limit admission.
            max_calls = _positive_int(execution.get("max_model_calls"), "max_model_calls")
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = {
                key: value for key, value in model_calls["active"].items()
                if isinstance(value, dict) and value.get("lease_expire_time_ms", 0) > now_ms
            }
            used = list(model_calls["used_call_ids"])
            existing = active.get(call_id)
            if existing is not None:
                if (
                    existing.get("worker_id") != lease.worker_id
                    or _stored_model_execution_identity(existing) != identity
                ):
                    raise ModelCallLeaseConflictError(
                        "call_id is reserved by another execution lease"
                    )
                first = False
            else:
                first = call_id not in used
                if first:
                    used.append(call_id)
            active[call_id] = {
                "worker_id": lease.worker_id,
                "execution_kind": identity[0],
                "execution_task_id": identity[1],
                "execution_lease_version": identity[2],
                "lease_expire_time_ms": datetime_to_unix_time_ms(expires_at),
            }
            model_calls["used_call_ids"] = used
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(reserved_at)
            return ModelCallReservation(
                video_parse_task_id=task.id,
                call_id=call_id,
                worker_id=lease.worker_id,
                first_reservation=first,
                used_calls=len(used),
                max_calls=max_calls,
                active_calls=len(active),
                lease_expire_time=expires_at,
            )

    def renew_model_call(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        call_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """Extend an existing live slot without recreating an expired slot."""

        if not call_id.strip():
            raise ValueError("call_id must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        renewed_at = now if now is not None else now_utc()
        renewed_at_ms = datetime_to_unix_time_ms(renewed_at)
        identity = _model_execution_identity(lease)
        with self._session_factory.begin() as session:
            task = _lock_model_execution_lease(session, lease, renewed_at)
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = deepcopy(model_calls["active"])
            slot = active.get(call_id)
            expires_at_ms = (
                slot.get("lease_expire_time_ms") if isinstance(slot, dict) else None
            )
            if (
                not isinstance(slot, dict)
                or slot.get("worker_id") != lease.worker_id
                or _stored_model_execution_identity(slot) != identity
                or not isinstance(expires_at_ms, int)
                or expires_at_ms <= renewed_at_ms
            ):
                return False
            expires_at = renewed_at + lease_duration
            slot["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
            active[call_id] = slot
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(renewed_at)
            return True

    def release_model_call(
        self,
        video_parse_task_id: str,
        call_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == video_parse_task_id)
                .with_for_update()
            )
            if task is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            model_calls = deepcopy(runtime["model_calls"])
            active = deepcopy(model_calls["active"])
            slot = active.get(call_id)
            if slot is None or slot.get("worker_id") != worker_id:
                return False
            del active[call_id]
            model_calls["active"] = active
            runtime["model_calls"] = model_calls
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(released_at)
            return True

    def claim_next_prefix(
        self,
        series_id: str,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> PrefixParseLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        claimed_at = now if now is not None else now_utc()
        now_ms = datetime_to_unix_time_ms(claimed_at)
        expires_at = claimed_at + lease_duration
        with self._session_factory() as session:
            frontier = _series_prefix_frontier(session, series_id)
        if frontier is None:
            return None
        frontier_task_id, _previous_compact = frontier

        with self._session_factory.begin() as session:
            # Lock only the already selected frontier. If another worker owns
            # it, return instead of skipping to a later episode or old task.
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == frontier_task_id)
                .with_for_update(skip_locked=True)
            )
            if task is None or task.is_deleted:
                return None
            current_frontier = _series_prefix_frontier(session, series_id)
            if current_frontier is None or current_frontier[0] != task.id:
                return None
            previous_compact = current_frontier[1]
            config = task.parse_config
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            if runtime.get("accepted") is not True:
                return None
            prefix = deepcopy(runtime["prefix"])
            next_execute_ms = prefix.get("next_execute_time_ms", 0)
            if isinstance(next_execute_ms, int) and next_execute_ms > now_ms:
                return None
            execution = _execution_config(config)
            max_retries = _nonnegative_int(
                execution.get("max_retry_count"),
                "max_retry_count",
            )
            cursor = int(prefix["next_chunk_sequence_no"])
            owner = prefix.get("lease_owner")
            lease_expire_ms = prefix.get("lease_expire_time_ms")
            if (
                owner is not None
                and isinstance(lease_expire_ms, int)
                and lease_expire_ms > now_ms
            ):
                return None
            if owner is not None:
                # Claim-time recovery is the Prefix equivalent of the table
                # lease reaper. Count every abandoned execution before a new
                # owner is allowed to take over, otherwise crashes can retry
                # forever without consuming the configured budget.
                prefix["retry_count"] = int(prefix.get("retry_count", 0)) + 1
                prefix["lease_owner"] = None
                prefix["lease_expire_time_ms"] = None
                prefix["execution_task_id"] = None
                prefix["last_error"] = _error_detail("worker lease expired")
                prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(
                    claimed_at + task_retry_delay(
                        initial_delay_seconds=execution["retry_initial_delay_seconds"],
                        max_delay_seconds=execution["retry_max_delay_seconds"],
                        failure_count=prefix["retry_count"],
                    )
                )
            if owner is not None or int(prefix.get("retry_count", 0)) > max_retries:
                runtime["prefix"] = prefix
                detail[RUNTIME_DETAIL_KEY] = runtime
                task.detail = detail
                task.update_time = datetime_to_unix_time_ms(claimed_at)
                return None
            if cursor >= task.chunk_count or task.status not in {
                VideoParseTaskStatus.PARSING.value,
                VideoParseTaskStatus.INDEXING.value,
                VideoParseTaskStatus.INDEXING_FAILED.value,
                VideoParseTaskStatus.COMPLETE.value,
            }:
                return None
            chunk_row = session.execute(
                select(ChunkParseTask, VideoChunk, ChunkParseContent)
                .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
                .join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
                .where(
                    ChunkParseTask.video_task_id == task.id,
                    VideoChunk.sequence_no == cursor,
                    ChunkParseContent.content.is_not(None),
                )
            ).one_or_none()
            if chunk_row is None:
                return None
            chunk_task, chunk, content = chunk_row
            covering = _submitted_intervals_by_member(session, task.id).get(
                chunk_task.id, ()
            )
            refined = set(session.scalars(select(IntervalParseContent.interval_task_id).where(
                IntervalParseContent.interval_task_id.in_([item.id for item in covering]),
            )))
            if not chunk_index_ready(bool(content.content), [
                item.status in _REFINE_SUBMITTED and item.id in refined for item in covering
            ]):
                return None
            prefix["lease_owner"] = worker_id
            prefix["lease_version"] = int(prefix.get("lease_version", 0)) + 1
            prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(expires_at)
            prefix["execution_task_id"] = chunk_task.id
            prefix["series_prefix_compact"] = previous_compact
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(claimed_at)
            return PrefixParseLease(
                video_parse_task_id=task.id,
                chunk_parse_task_id=chunk_task.id,
                video_chunk_id=chunk.id,
                sequence_no=chunk.sequence_no,
                worker_id=worker_id,
                lease_version=prefix["lease_version"],
                lease_expire_time=expires_at,
                retry_count=int(prefix.get("retry_count", 0)),
                content_version=content.version,
                content=deepcopy(dict(content.content or {})),
                content_detail=deepcopy(dict(content.detail or {})),
                series_prefix_compact=previous_compact,
                video_prefix_compact=str(prefix.get("video_prefix_compact", "")),
            )

    def claim_next_prefix_any(
        self,
        worker_id: str,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> PrefixParseLease | None:
        """Periodic recovery scan across Series after notifications are lost."""

        claimed_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            series_ids = list(session.scalars(
                select(Video.series_id)
                .join(
                    VideoParseTask,
                    VideoParseTask.video_id == Video.id,
                )
                .where(VideoParseTask.is_deleted.is_(False))
                .distinct()
                .order_by(Video.series_id)
            ))
        for series_id in series_ids:
            lease = self.claim_next_prefix(
                series_id,
                worker_id,
                lease_duration=lease_duration,
                now=claimed_at,
            )
            if lease is not None:
                return lease
        return None

    def heartbeat_prefix(
        self,
        lease: PrefixParseLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        heartbeat_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, heartbeat_at)
            if prefix is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(
                heartbeat_at + lease_duration
            )
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(heartbeat_at)
            return True

    def load_prefix_content(
        self, lease: PrefixParseLease, *, now: datetime | None = None,
    ) -> ChunkParseContentState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            task = session.get(VideoParseTask, lease.video_parse_task_id)
            if _current_prefix_lease(task, lease, loaded_at) is None:
                raise VideoParseLeaseLostError("Prefix lease is no longer current")
            row = session.execute(
                select(ChunkParseTask, VideoChunk, ChunkParseContent)
                .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
                .join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
                .where(ChunkParseTask.id == lease.chunk_parse_task_id,
                       ChunkParseTask.video_task_id == lease.video_parse_task_id)
            ).one_or_none()
            if row is None:
                raise VideoParseConflictError("Prefix target no longer exists")
            return _content_state(*row)

    def commit_prefix(
        self,
        lease: PrefixParseLease,
        *,
        content_updates: Mapping[str, Any],
        video_prefix_compact: str,
        series_prefix_compact: str | None = None,
        input_version: int | None = None,
        input_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        committed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            locked_intervals = _locked_parent_intervals(
                session, lease.video_parse_task_id
            )
            chunk_task = session.scalar(
                select(ChunkParseTask)
                .where(
                    ChunkParseTask.id == lease.chunk_parse_task_id,
                    ChunkParseTask.video_task_id
                    == lease.video_parse_task_id,
                )
                .with_for_update()
            )
            content = session.scalar(
                select(ChunkParseContent)
                .where(
                    ChunkParseContent.chunk_task_id
                    == lease.chunk_parse_task_id
                )
                .with_for_update()
            )
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == lease.video_parse_task_id)
                .with_for_update()
            )
            prefix = _current_prefix_lease(task, lease, committed_at)
            if prefix is None:
                raise VideoParseLeaseLostError("Prefix lease is no longer current")
            if _latest_video_task_id(session, task.video_id) != task.id:
                raise VideoParseLeaseLostError(
                    "Prefix lease task was superseded by a newer parse version"
                )
            if chunk_task is None or content is None:
                raise VideoParseConflictError("Prefix target no longer exists")
            expected_version = lease.content_version if input_version is None else input_version
            expected_fingerprint = (
                parse_content_fingerprint(lease.content) if input_fingerprint is None else input_fingerprint
            )
            if (content.version != expected_version
                    or parse_content_fingerprint(dict(content.content or {})) != expected_fingerprint):
                raise VideoParseSnapshotConflictError("Prefix target content snapshot changed")
            current = deepcopy(dict(content.content or {}))
            updated = deepcopy(current)
            for field, value in content_updates.items():
                if field not in current:
                    raise VideoParseConflictError(
                        f"Prefix cannot add unknown field {field!r}"
                    )
                updated[field] = deepcopy(value)
            chunk = session.get(VideoChunk, chunk_task.chunk_id)
            if chunk is None:
                raise VideoParseConflictError("Prefix target media identity is missing")
            scope = _ordered_int_union(dict(content.detail or {}).get("scope"), [chunk.sequence_no])
            if scope[-1] >= task.chunk_count:
                raise VideoParseConflictError("Prefix scope exceeds its fixed Video topology")
            video = session.get(Video, task.video_id)
            if video is None:
                raise VideoParseConflictError("Prefix source Video is missing")
            chunk_duration = task.parse_config.get("chunk_duration")
            if type(chunk_duration) is not int or chunk_duration <= 0:
                raise VideoParseConflictError("frozen parse config has an invalid chunk_duration")
            chunk_duration_ms = chunk_duration * 1000
            updated = validate_result_time_range(
                VideoParseResult.model_validate(updated),
                start_seconds=scope[0] * chunk_duration_ms,
                end_seconds=min(video.duration_ms, (scope[-1] + 1) * chunk_duration_ms),
            ).model_dump(mode="json")
            changed = updated != current
            reset: tuple[str, ...] = ()
            if changed:
                content.content = updated
                content.version += 1
                reset = _advance_ready_chunks(
                    session,
                    task.id,
                    [chunk_task.id],
                    committed_at,
                    locked_intervals=locked_intervals,
                    invalidate_index=True,
                )
            content_detail = deepcopy(dict(content.detail or {}))
            content_detail["compact"] = video_prefix_compact
            content.detail = content_detail
            content.update_time = datetime_to_unix_time_ms(committed_at)

            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["next_chunk_sequence_no"] = lease.sequence_no + 1
            prefix["video_prefix_compact"] = video_prefix_compact
            if series_prefix_compact is not None:
                prefix["series_prefix_compact"] = series_prefix_compact
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["retry_count"] = 0
            prefix["last_error"] = None
            committed_leases = deepcopy(prefix.get("committed_leases", {}))
            committed_leases[str(lease.lease_version)] = {
                "execution_task_id": lease.chunk_parse_task_id,
                "sequence_no": lease.sequence_no,
            }
            prefix["committed_leases"] = committed_leases
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(committed_at)
            _aggregate_video_task(session, task.id, committed_at)
            if changed and _video_failure_status(task) is None:
                task.next_execute_time = committed_at
            return reset

    def retry_prefix(
        self,
        lease: PrefixParseLease,
        *,
        error: str,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, failed_at)
            if prefix is None:
                return False
            execution = _execution_config(task.parse_config)
            failure_count = int(prefix.get("retry_count", 0)) + 1
            retry_at = failed_at + task_retry_delay(
                initial_delay_seconds=execution["retry_initial_delay_seconds"],
                max_delay_seconds=execution["retry_max_delay_seconds"],
                failure_count=failure_count,
            )
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(retry_at)
            prefix["retry_count"] = failure_count
            prefix["last_error"] = _error_detail(error)
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(failed_at)
            return True

    def release_prefix(
        self,
        lease: PrefixParseLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Release a cancelled Prefix execution without recording a failure."""

        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if (
                task is None
                or _latest_video_task_id(session, task.video_id) != task.id
            ):
                return False
            prefix = _current_prefix_lease(task, lease, released_at)
            if prefix is None:
                return False
            detail = _ensure_runtime(task.detail)
            runtime = deepcopy(detail[RUNTIME_DETAIL_KEY])
            prefix["lease_owner"] = None
            prefix["lease_expire_time_ms"] = None
            prefix["execution_task_id"] = None
            prefix["next_execute_time_ms"] = datetime_to_unix_time_ms(released_at)
            runtime["prefix"] = prefix
            detail[RUNTIME_DETAIL_KEY] = runtime
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(released_at)
            return True


def _task_input_state(
    task: VideoParseTask,
    video: Video,
    *,
    tenant_id: str,
    series: Series | None = None,
) -> VideoParseTaskInputState:
    return VideoParseTaskInputState(
        task=_task_state(task),
        series_id=video.series_id,
        episode_order=video.episode_order,
        original_filename=video.filename,
        original_source_uri=video.source_uri,
        inference_proxy_uri=video.proxy_uri,
        content_hash=video.hash,
        total_bytes=video.size_bytes,
        duration_seconds=video.duration_ms,
        tenant_id=tenant_id,
        background=(
            {"series_id": series.id, "description": series.description,
             "important_extra_data": deepcopy(series.important_extra_data),
             "character_bible": deepcopy(series.character_bible)}
            if series is not None else {}
        ),
    )


def _video_chunk_state(chunk: VideoChunk) -> VideoChunkState:
    return VideoChunkState(
        video_chunk_id=chunk.id,
        sequence_no=chunk.sequence_no,
        start_seconds=chunk.start_ms,
        end_seconds=chunk.end_ms,
        chunk_media_ref=chunk.chunk_media_ref,
        analysis_window_media_ref=chunk.analysis_window_media_ref,
    )


def _task_state(task: VideoParseTask) -> VideoParseTaskState:
    return VideoParseTaskState(
        video_parse_task_id=task.id,
        created_by_user_id=task.creator_id,
        video_id=task.video_id,
        pipeline_version=task.pipeline_version,
        status=task.status,
        parse_config=deepcopy(dict(task.parse_config)),
        chunk_count=task.chunk_count,
        version=task.version,
        next_execute_time=task.next_execute_time,
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        lease_owner=task.lease_owner,
        lease_version=task.lease_version,
        lease_expire_time=task.lease_expire_time,
        heartbeat_time=task.heartbeat_time,
        start_time=task.start_time,
        finish_time=task.finish_time,
        processing_duration_ms=task.processing_duration_ms,
        processing_timing_complete=task.processing_timing_complete,
        last_error=deepcopy(task.last_error),
        is_test=task.is_test,
        detail=deepcopy(dict(task.detail)),
        create_time=task.create_date,
        update_time=task.update_date,
    )


def _locked_video_lease(
    session: Session, lease: VideoParseLease, now: datetime
) -> VideoParseTask:
    task = session.scalar(
        select(VideoParseTask)
        .where(VideoParseTask.id == lease.video_parse_task_id)
        .with_for_update()
    )
    if (
        task is None
        or task.lease_owner != lease.worker_id
        or task.lease_version != lease.lease_version
        or task.lease_expire_time is None
        or task.lease_expire_time <= now
    ):
        raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    return task


def _locked_chunk_lease(
    session: Session, lease: ChunkParseLease, now: datetime
) -> ChunkParseTask:
    task = session.scalar(
        select(ChunkParseTask)
        .where(ChunkParseTask.id == lease.chunk_parse_task_id)
        .with_for_update()
    )
    if not _matching_chunk_lease(task, lease, now):
        raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    return task


def _matching_chunk_lease(
    task: ChunkParseTask | None,
    lease: ChunkParseLease,
    now: datetime,
) -> bool:
    return bool(
        task is not None
        and task.video_task_id == lease.video_parse_task_id
        and task.chunk_id == lease.video_chunk_id
        and task.phase == lease.phase
        and task.status == ChunkParseTaskStatus.RUNNING.value
        and task.lease_owner == lease.worker_id
        and task.lease_version == lease.lease_version
        and task.lease_expire_time is not None
        and task.lease_expire_time > now
    )


def _interval_for_lease(
    session: Session, lease: IntervalParseLease, now: datetime
) -> IntervalParseTask:
    interval = session.get(IntervalParseTask, lease.interval_parse_task_id)
    if not _matching_interval_lease(interval, lease, now):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def _locked_interval_lease(
    session: Session,
    lease: IntervalParseLease,
    now: datetime,
    expected_status: str,
) -> IntervalParseTask:
    interval = session.scalar(
        select(IntervalParseTask)
        .where(IntervalParseTask.id == lease.interval_parse_task_id)
        .with_for_update()
    )
    if (
        not _matching_interval_lease(interval, lease, now)
        or interval.status != expected_status
    ):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def _matching_interval_lease(
    interval: IntervalParseTask | None,
    lease: IntervalParseLease,
    now: datetime,
) -> bool:
    return bool(
        interval is not None
        and interval.video_task_id == lease.video_parse_task_id
        and tuple(interval.chunk_task_ids) == lease.chunk_parse_task_ids
        and interval.start_sequence_no == lease.start_sequence_no
        and interval.chunk_count == lease.chunk_count
        and interval.status == lease.phase
        and interval.lease_owner == lease.worker_id
        and interval.lease_version == lease.lease_version
        and interval.lease_expire_time is not None
        and interval.lease_expire_time > now
    )


def _member_content_rows(
    session: Session,
    interval: IntervalParseTask,
    *,
    for_update: bool,
) -> list[tuple[ChunkParseTask, VideoChunk, ChunkParseContent]]:
    statement = (
        select(ChunkParseTask, VideoChunk, ChunkParseContent)
        .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
        .join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
        .where(ChunkParseTask.id.in_(interval.chunk_task_ids))
        .order_by(VideoChunk.sequence_no)
    )
    if for_update:
        statement = statement.with_for_update()
    rows = list(session.execute(statement))
    if (
        len(rows) != interval.chunk_count
        or tuple(row[0].id for row in rows) != tuple(interval.chunk_task_ids)
    ):
        raise VideoParseConflictError("Interval member content is incomplete")
    return rows


def _content_state(
    task: ChunkParseTask,
    chunk: VideoChunk,
    content: ChunkParseContent,
) -> ChunkParseContentState:
    return ChunkParseContentState(
        chunk_parse_task_id=task.id,
        video_chunk_id=chunk.id,
        sequence_no=chunk.sequence_no,
        content=deepcopy(dict(content.content or {})),
        version=content.version,
        detail=deepcopy(dict(content.detail or {})),
    )


def _locked_parent_intervals(
    session: Session, video_parse_task_id: str
) -> list[IntervalParseTask]:
    _locked_parent_task(session, video_parse_task_id)
    return list(session.scalars(
        select(IntervalParseTask)
        .where(IntervalParseTask.video_task_id == video_parse_task_id)
        .order_by(
            IntervalParseTask.start_sequence_no,
            IntervalParseTask.id,
        )
        .with_for_update()
    ))


def _submitted_intervals_by_member(
    session: Session,
    video_parse_task_id: str,
    *,
    for_update: bool = False,
) -> dict[str, tuple[IntervalParseTask, ...]]:
    intervals = _locked_parent_intervals(session, video_parse_task_id) if for_update else list(
        session.scalars(
            select(IntervalParseTask)
            .where(IntervalParseTask.video_task_id == video_parse_task_id)
            .order_by(
                IntervalParseTask.start_sequence_no,
                IntervalParseTask.id,
            )
        )
    )
    grouped: dict[str, list[IntervalParseTask]] = {}
    for interval in intervals:
        for task_id in interval.chunk_task_ids:
            grouped.setdefault(task_id, []).append(interval)
    return {key: tuple(value) for key, value in grouped.items()}


def _locked_parent_task(session: Session, video_parse_task_id: str) -> VideoParseTask:
    parent = session.scalar(select(VideoParseTask).where(
        VideoParseTask.id == video_parse_task_id,
    ).with_for_update())
    if parent is None or parent.is_deleted:
        raise VideoParseTaskNotFoundError(f"VideoParseTask {video_parse_task_id!r} does not exist")
    return parent


def _advance_ready_chunks(
    session: Session, video_parse_task_id: str, member_ids: Sequence[str], now: datetime,
    *, locked_intervals: Sequence[IntervalParseTask] | None = None,
    invalidate_index: bool = False,
) -> tuple[str, ...]:
    """Apply Agent readiness to every member in the same result transaction."""
    _locked_parent_task(session, video_parse_task_id)
    intervals = (list(locked_intervals) if locked_intervals is not None
                 else _locked_parent_intervals(session, video_parse_task_id))
    refined = set(session.scalars(select(IntervalParseContent.interval_task_id).where(
        IntervalParseContent.interval_task_id.in_([item.id for item in intervals]),
    )))
    rows = session.execute(select(ChunkParseTask, ChunkParseContent).join(
        ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
    ).where(ChunkParseTask.video_task_id == video_parse_task_id,
            ChunkParseTask.id.in_(tuple(member_ids))).order_by(ChunkParseTask.id).with_for_update()).all()
    queued = []
    for child, content in rows:
        covering = [item for item in intervals if child.id in item.chunk_task_ids]
        if not chunk_index_ready(bool(content.content), [
            item.status == "COMPLETE" and item.id in refined for item in covering
        ]):
            continue
        if not invalidate_index and not (child.phase == ChunkTaskPhase.REFINE and child.status == "WAITING"):
            continue
        child.phase = ChunkTaskPhase.INDEX
        child.status = ChunkParseTaskStatus.QUEUED.value
        child.retry_count = 0
        # Revocation is monotonic even when no worker currently owns this row.
        if invalidate_index:
            child.lease_version += 1
        child.lease_owner = None
        child.lease_expire_time = None
        child.next_execute_time = now
        child.start_time = None
        child.finish_time = None
        child.last_error = None
        child.update_time = datetime_to_unix_time_ms(now)
        queued.append(child.id)
    return tuple(queued)


def _video_failure_status(task: VideoParseTask) -> str | None:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    status = runtime.get("failure_status") if isinstance(runtime, dict) else None
    return status if status in {
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    } else None


def _aggregate_video_task(
    session: Session, video_parse_task_id: str, now: datetime,
    *, release_terminal_lease: bool = False, failure_status: str | None = None,
) -> VideoParseTask:
    task = _locked_parent_task(session, video_parse_task_id)
    session.flush()
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    chunk_rows = list(session.execute(select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
        VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
    ).outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
        ChunkParseTask.video_task_id == task.id,
    ).order_by(VideoChunk.sequence_no)))
    intervals = list(session.scalars(select(IntervalParseTask).where(
        IntervalParseTask.video_task_id == task.id,
    ).order_by(IntervalParseTask.start_sequence_no)))
    chunks = [child for child, _chunk, _content in chunk_rows]
    if (runtime.get("realtime_only") is True and runtime.get("accepted") is not True
        and failure_status is None and _video_failure_status(task) is None):
        task.status = "PREPARING" if task.status == "PREPARING" or task.lease_owner is not None else "PENDING"
        task.finish_time = None
        if task.lease_owner is None and not any(child.lease_owner for child in chunks):
            task.capacity_acquired = False
        _touch_if_modified(session, task, now)
        return task
    refined = set(session.scalars(select(IntervalParseContent.interval_task_id).where(
        IntervalParseContent.interval_task_id.in_([item.id for item in intervals]),
    )))
    expected = _interval_specs(task.chunk_count, _interval_size(task.parse_config))
    topology_complete = (
        len(chunks) == task.chunk_count
        and [chunk.sequence_no for _, chunk, _ in chunk_rows] == list(range(task.chunk_count))
        and all(chunk.video_id == task.video_id for _, chunk, _ in chunk_rows)
        and [(item.start_sequence_no, item.chunk_count) for item in intervals] == list(expected)
        and all(tuple(item.chunk_task_ids) == tuple(
            child.id for child in chunks[item.start_sequence_no:item.start_sequence_no + item.chunk_count]
        ) for item in intervals)
    )
    recorded_failure = _video_failure_status(task) or (
        task.status if task.status in {"PARSING_FAILED", "INDEXING_FAILED"} else None
    )
    scoring_pending = (task.parse_config.get("voiceprint_mode", "off") != "off"
                       and dict((task.detail or {}).get("voiceprint") or {}).get("ready") is not True)
    parsing_failed = ("PARSING_FAILED" in {failure_status, recorded_failure}
                      or not scoring_pending and any(
                          item.phase == ChunkTaskPhase.PARSE and item.status == "FAILED" for item in chunks)
                      or any(item.status == "PARSING_FAILED" for item in intervals))
    indexing_failed = ("INDEXING_FAILED" in {failure_status, recorded_failure}
                       or any(item.phase == ChunkTaskPhase.INDEX and item.status == "FAILED" for item in chunks))
    status = summarize_video_task(
        topology_complete=topology_complete,
        understanding_complete=all(content is not None and bool(content.content)
                                   for _, _, content in chunk_rows),
        refinement_complete=all(item.status == "COMPLETE" and item.id in refined for item in intervals),
        indexing_complete=all(
            child.phase == ChunkTaskPhase.INDEX and child.status == "COMPLETE" and content is not None
            and child.indexed_content_version == content.version
            for child, _, content in chunk_rows
        ),
        parsing_failed=parsing_failed, indexing_failed=indexing_failed,
    )
    previous_status = task.status
    failure = status if status in {"PARSING_FAILED", "INDEXING_FAILED"} else None
    if failure:
        detail = _ensure_runtime(task.detail)
        detail[RUNTIME_DETAIL_KEY]["failure_status"] = failure
        task.detail = detail
        if previous_status not in {"PARSING_FAILED", "INDEXING_FAILED"} and (
            any(child.lease_owner is not None for child in [*chunks, *intervals]) or not release_terminal_lease
        ):
            status = "PARSING" if failure == "PARSING_FAILED" else "INDEXING"
    elif status == "COMPLETE" and task.lease_owner is not None and not release_terminal_lease:
        status = "INDEXING"
    task.status = status
    if status in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"}:
        if previous_status != status or task.finish_time is None:
            task.finish_time = now
        task.capacity_acquired = False
        if release_terminal_lease:
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
    else:
        task.finish_time = None
    _touch_if_modified(session, task, now)
    video = session.get(Video, task.video_id)
    if video is not None:
        video.parse_status = status
        video.parse_progress = 100 if status == "COMPLETE" else 0
    return task


def _touch_if_modified(session: Session, task: VideoParseTask, now: datetime) -> None:
    # Scheduler sweeps re-aggregate unchanged tasks every tick; an update_time-only
    # UPDATE still logs the whole row (and its large detail) to the binlog.
    if session.is_modified(task):
        task.update_time = datetime_to_unix_time_ms(now)


def _interval_specs(chunk_count: int, interval_size: int) -> tuple[tuple[int, int], ...]:
    if chunk_count < interval_size:
        return ((0, chunk_count),)
    return tuple((start, interval_size) for start in range(chunk_count - interval_size + 1))


def _interval_size(parse_config: Mapping[str, Any]) -> int:
    chunk_duration = _positive_int(parse_config.get("chunk_duration"), "chunk_duration")
    interval_duration = _positive_int(
        parse_config.get("interval_duration"), "interval_duration"
    )
    size = math.ceil(interval_duration / chunk_duration)
    if size > 63:
        raise VideoParseConflictError("interval_size exceeds the 63-member bitmap limit")
    return size


def _validate_chunk_boundaries(
    video: Video,
    parse_config: Mapping[str, Any],
    chunks: Sequence[VideoChunk],
) -> None:
    duration = _positive_int(parse_config.get("chunk_duration"), "chunk_duration") * 1000
    expected = [
        (sequence, sequence * duration, min(video.duration_ms, (sequence + 1) * duration))
        for sequence in range(len(chunks))
    ]
    actual = [(item.sequence_no, item.start_ms, item.end_ms) for item in chunks]
    if actual != expected:
        raise VideoParseConflictError("Video chunks do not match parse_config.chunk_duration")


def _ordered_int_union(existing: Any, additions: Sequence[int]) -> list[int]:
    values: list[int] = []
    if isinstance(existing, list):
        values.extend(item for item in existing if isinstance(item, int) and not isinstance(item, bool))
    values.extend(additions)
    return sorted(set(values))


def _voiceprint_event_fingerprint(event: Mapping[str, Any]) -> str:
    """Unrelated canvas edits do not invalidate a still-identical voice anchor."""
    identity = {key: event.get(key) for key in (
        "event_type", "speaker", "start_seconds", "end_seconds", "text_variants",
    )}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _voiceprint_scoring_snapshot(
    session: Session, parent: VideoParseTask, lease: VideoParseLease, started_at: datetime,
) -> dict[str, Any]:
    """Freeze every terminal Understanding source; scoring remains execution-local."""
    rows = list(session.execute(select(ChunkParseTask, VideoChunk, ChunkParseContent).join(
        VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id,
    ).outerjoin(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
        ChunkParseTask.video_task_id == parent.id, VideoChunk.video_id == parent.video_id,
    ).order_by(VideoChunk.sequence_no).with_for_update()))
    committed = [(task, chunk, result) for task, chunk, result in rows
                 if result is not None and result.content is not None]
    failed = [(task, chunk) for task, chunk, result in rows
              if (result is None or result.content is None)
              and task.phase == ChunkTaskPhase.PARSE and task.status == "FAILED"]
    if (len(rows) != parent.chunk_count
        or [chunk.sequence_no for _task, chunk, _result in rows] != list(range(parent.chunk_count))
        or len(committed) + len(failed) != parent.chunk_count):
        raise VideoParseConflictError("voiceprint scoring needs every Understanding chunk to be terminal")
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    versions = {chunk.id: result.version for _task, chunk, result in committed}
    fingerprints = {chunk.id: parse_content_fingerprint(result.content) for _task, chunk, result in committed}
    failed_chunks = [{"chunk_task_id": task.id, "chunk_id": chunk.id,
                      "sequence_no": chunk.sequence_no, "last_error": deepcopy(task.last_error)}
                     for task, chunk in failed]
    same_execution = (phase.get("active") is True and phase.get("lease_version") == lease.lease_version
                      and phase.get("worker_id") == lease.worker_id)
    if same_execution:
        if (phase.get("source_versions") != versions or phase.get("source_fingerprints") != fingerprints
            or phase.get("failed_chunks") != failed_chunks):
            raise VideoParseSnapshotConflictError("Voiceprint source snapshot changed during scoring")
    else:
        existing_models = {json.dumps(record["model"], sort_keys=True)
                           for record in dict(detail.get("voiceprint") or {}).get("voiceprintList", [])}
        if len(existing_models) > 1:
            raise VideoParseSnapshotConflictError("Voiceprint sources contain incompatible encoder revisions")
        if existing_models:
            identity = json.loads(next(iter(existing_models)))
            if phase.get("encoder_model") is not None and phase["encoder_model"] != identity:
                raise VideoParseSnapshotConflictError("Voiceprint encoder differs from the frozen task")
            phase["encoder_model"] = identity
        committed_times = sorted(result.create_time for _task, _chunk, result in committed)
        previous_count = voiceprint_preparation_min_chunks(parent.chunk_count)
        previous_at = committed_times[previous_count - 1] if len(committed_times) >= previous_count else None
        terminal_times = [*committed_times, *(datetime_to_unix_time_ms(task.finish_time)
                                             for task, _chunk in failed if task.finish_time is not None)]
        terminal_at = max(terminal_times, default=datetime_to_unix_time_ms(started_at))
        phase.update({"active": True, "lease_version": lease.lease_version, "worker_id": lease.worker_id,
                      "source_chunk_ids": list(versions), "source_versions": versions,
                      "source_fingerprints": fingerprints, "failed_chunks": failed_chunks,
                      "timing": {"previous_75_percent_at_ms": previous_at,
                                 "all_understanding_terminal_at_ms": terminal_at,
                                 "extra_gate_wait_ms": terminal_at - previous_at if previous_at is not None else None,
                                 "preparation_started_at_ms": datetime_to_unix_time_ms(started_at)}})
        detail["voiceprint"] = Voiceprint().model_dump(mode="json")
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(started_at)
    video = session.get(Video, parent.video_id)
    if video is None:
        raise VideoParseConflictError("Voiceprint source Video no longer exists")
    sources = {}
    for task, chunk, result in committed:
        for position, event in enumerate(dict(result.content).get("timeline", [])):
            if event.get("event_type") != "DIALOGUE":
                continue
            fingerprint = _voiceprint_event_fingerprint(event)
            source_id = f"{chunk.id}:{position}:{fingerprint}"
            sources[source_id] = {"source_id": source_id, "chunk_task_id": task.id, "chunk_id": chunk.id,
                                  "event_index": position, "position": position,
                                  "source_version": result.version, "event_fingerprint": fingerprint,
                                  "speaker": event.get("speaker"), "start_ms": event["start_seconds"],
                                  "end_ms": event["end_seconds"],
                                  "text_variants": deepcopy(event["text_variants"])}
    speakers = sorted({source["speaker"] for source in sources.values() if source["speaker"]})
    return {"member_sequence_nos": {chunk.id: chunk.sequence_no for _task, chunk, _result in committed},
            "member_contents": {chunk.id: deepcopy(result.content) for _task, chunk, result in committed},
            "member_versions": versions,
            "member_evidence": {chunk.id: deepcopy(result.detail or {}) for _task, chunk, result in committed},
            "sources": sources, "speaker_list": speakers, "source_media_ref": video.source_uri,
            "voiceprint": deepcopy(detail["voiceprint"]), "failed_chunks": failed_chunks,
            "timing": deepcopy(phase["timing"])}


def _voiceprint_active_preparation(
    parent: VideoParseTask, lease: VideoParseLease,
) -> tuple[dict[str, Any], dict[str, Any]]:
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if (parent.is_deleted or (parent.parse_config.get("voiceprint") is None
                             and parent.parse_config.get("voiceprint_mode", "off") == "off")
        or dict(detail.get("voiceprint") or {}).get("ready") is True
        or phase.get("active") is not True or phase.get("lease_version") != lease.lease_version
        or phase.get("worker_id") != lease.worker_id):
        raise VideoParseLeaseLostError("Voiceprint preparation execution is no longer active")
    return detail, phase


def _voiceprint_reference_progress(
    references: Sequence[Mapping[str, Any]], speakers: set[str],
) -> dict[str, list[dict[str, Any]]]:
    progress = {speaker: {} for speaker in sorted(speakers)}
    for ref in references:
        if ref["speaker"] in progress:
            source_ref = f'{ref["chunk_id"]}:{ref["event_fingerprint"]}'
            progress[ref["speaker"]].setdefault(source_ref, {
                "source_ref": source_ref, "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
            })
    return {speaker: list(refs.values()) for speaker, refs in progress.items()}


def _voiceprint_registration_receipt(
    record: CharacterVoiceprint, reference: VoiceprintReference, *, refs_count: int, added: bool,
) -> dict[str, Any]:
    return {"added": added, "speaker": record.speaker, "model": record.model.model_dump(mode="json"),
            "refs_count": refs_count, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
            "reference": {**reference.model_dump(mode="json"),
                          "source_ref": f"{reference.chunk_id}:{reference.event_fingerprint}"}}


def _check_voiceprint_registration(
    session: Session, parent: VideoParseTask, lease: VideoParseLease, *, source_ref: str,
    speaker: str, start_ms: int, end_ms: int, model: str, encoder_revision: str,
    replace_source_ref: str | None = None,
) -> tuple[dict[str, Any], Voiceprint, int, dict[str, Any] | None]:
    detail, phase = _voiceprint_active_preparation(parent, lease)
    chunk_id, separator, fingerprint = source_ref.partition(":")
    if not separator or chunk_id not in phase["source_chunk_ids"] or speaker not in phase["speaker_list"]:
        raise VideoParseConflictError("voiceprint source or speaker is outside this preparation")
    result = session.scalar(select(ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).where(
        ChunkParseTask.video_task_id == parent.id,
        VideoChunk.video_id == parent.video_id, VideoChunk.id == chunk_id,
    ).with_for_update())
    if result is None:
        raise VideoParseSnapshotConflictError("Voiceprint source no longer exists")
    event = next((event for event in dict(result.content or {}).get("timeline", [])
                  if event.get("event_type") == "DIALOGUE"
                  and _voiceprint_event_fingerprint(event) == fingerprint), None)
    if (event is None or event.get("speaker") != speaker
        or type(start_ms) is not int or type(end_ms) is not int
        or not event["start_seconds"] <= start_ms < end_ms <= event["end_seconds"]):
        raise VideoParseSnapshotConflictError("Voiceprint source identity or authorized segment changed")
    voiceprint = Voiceprint.model_validate(detail["voiceprint"])
    references = _valid_voiceprint_references(session, parent, voiceprint, for_update=True)
    if len(references) != sum(len(record.references) for record in voiceprint.voiceprintList):
        raise VideoParseSnapshotConflictError("Voiceprint registered source identity changed")
    count = len(_voiceprint_reference_progress(references, {speaker})[speaker])
    replacement = None
    if replace_source_ref is not None:
        old_chunk_id, old_separator, _old_fingerprint = replace_source_ref.partition(":")
        if not old_separator or old_chunk_id not in phase["source_chunk_ids"]:
            raise VoiceprintRegistrationRejectedError(
                "INVALID_VOICEPRINT_SOURCE", "Replacement source is outside this preparation.",
                "Use the old reference's source_ref returned by get_speaker_list.",
            )
        replacement = next(((item, ref) for item in voiceprint.voiceprintList for ref in item.references
                            if f"{ref.chunk_id}:{ref.event_fingerprint}" == replace_source_ref), None)
        if replacement is not None and (
            replacement[0].speaker != speaker or replacement[0].model.name != model
            or replacement[0].model.encoder_revision != encoder_revision
        ):
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_REPLACEMENT_MISMATCH", "Replacement must preserve the speaker and encoder version.",
                "Choose another authorized reference for the same speaker; do not move references between groups.",
            )
    for record in voiceprint.voiceprintList:
        for ref in record.references:
            if (ref.chunk_id, ref.event_fingerprint) != (chunk_id, fingerprint):
                continue
            if (record.speaker == speaker and record.model.name == model
                and record.model.encoder_revision == encoder_revision
                and (ref.start_ms, ref.end_ms) == (start_ms, end_ms)
                and (replacement is None or replacement[1] is ref)):
                receipt = _voiceprint_registration_receipt(
                    record, ref, refs_count=count, added=False,
                )
                if replace_source_ref is not None:
                    receipt["replaced_source_ref"] = replace_source_ref
                return detail, voiceprint, result.version, receipt
            if replacement is not None and replacement[1] is ref:
                continue  # Explicit replacement may refine this source's subrange without adding a case.
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_SOURCE_ALREADY_REGISTERED",
                f"Source {source_ref} already has a reference at {ref.start_ms}–{ref.end_ms} ms.",
                "Choose a different source_ref; another subrange of the same dialogue is not an independent case.",
            )
    if replace_source_ref is not None and replacement is None:
        raise VoiceprintRegistrationRejectedError(
            "VOICEPRINT_REFERENCE_NOT_FOUND", "The reference to replace is no longer registered.",
            "Refresh get_speaker_list; append normally if no existing reference needs replacing.",
        )
    for ref in references:
        if replacement is not None and f'{ref["chunk_id"]}:{ref["event_fingerprint"]}' == replace_source_ref:
            continue
        if ref["start_ms"] < end_ms and start_ms < ref["end_ms"]:
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_REFERENCE_OVERLAP",
                f'Requested range overlaps {ref["speaker"]}\'s reference at {ref["start_ms"]}–{ref["end_ms"]} ms.',
                "Choose another clean single-speaker range that does not overlap any registered reference.",
            )
    if replacement is not None:
        # This is a detached copy: only the final registration transaction persists the replacement.
        replacement[0].references.remove(replacement[1])
    return detail, voiceprint, result.version, None


def _voiceprint_consistency(references: Sequence[Mapping[str, Any]], speakers: set[str]) -> dict[str, Any]:
    """Flag low within-speaker mean similarity; this does not establish a character's name."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for ref in references:
        key = (ref["speaker"], ref["model"]["name"], ref["model"]["encoder_revision"])
        groups.setdefault(key, []).append(ref)
    suspects = []
    checked_speakers = set()
    thresholds = {"minimum_references": VOICEPRINT_CONSISTENCY_MIN_REFS,
                  "max_candidate_mean": VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN}
    for (speaker, _model, _revision), refs in groups.items():
        count = len(refs)
        if count < VOICEPRINT_CONSISTENCY_MIN_REFS:
            continue
        checked_speakers.add(speaker)
        vectors = []
        for ref in refs:
            norm = math.hypot(*ref["vector"])
            vectors.append([value / norm for value in ref["vector"]])
        pair_sums = [0.0] * count
        for i, vector in enumerate(vectors):
            for j in range(i):
                similarity = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(vector, vectors[j]))))
                pair_sums[i] += similarity
                pair_sums[j] += similarity
        for index, ref in enumerate(refs):
            candidate_mean = pair_sums[index] / (count - 1)
            if candidate_mean <= VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN:
                suspects.append({"speaker": speaker, "source_ref": f'{ref["chunk_id"]}:{ref["event_fingerprint"]}',
                                 "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
                                 "model": ref["model"], "duration_ms": ref["duration_ms"],
                                 "reference_count": count, "candidate_mean": candidate_mean})
    return {"suspect_references": suspects,
            "consistency": {"similarity_metric": "cosine", "similarity_is_probability": False,
                            "thresholds": thresholds, "checked_speakers": sorted(checked_speakers),
                            "insufficient_speakers": sorted(speakers - checked_speakers)}}


def _valid_voiceprint_references(
    session: Session, parent: VideoParseTask, voiceprint: Voiceprint, *, for_update: bool,
) -> list[dict[str, Any]]:
    chunk_ids = {ref.chunk_id for item in voiceprint.voiceprintList for ref in item.references}
    if not chunk_ids:
        return []
    if voiceprint.ready:
        # 约定：准备完成的声源标签与音频区间视为正确的静态成果。
        # 不复核可变 timeline，不因其文本、speaker 或时间编辑作废参考或重跑 Refine。
        owned_chunks = set(session.scalars(select(VideoChunk.id).join(
            ChunkParseTask, ChunkParseTask.chunk_id == VideoChunk.id,
        ).where(ChunkParseTask.video_task_id == parent.id, VideoChunk.video_id == parent.video_id,
                VideoChunk.id.in_(sorted(chunk_ids)))))
        return [{"speaker": record.speaker, "model": record.model.model_dump(mode="json"),
                 "confidence": record.confidence, "source_start_ms": reference.start_ms,
                 "source_end_ms": reference.end_ms, **reference.model_dump(mode="json")}
                for record in voiceprint.voiceprintList for reference in record.references
                if reference.chunk_id in owned_chunks]
    statement = select(VideoChunk.id, ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.chunk_id == VideoChunk.id,
    ).join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id).where(
        ChunkParseTask.video_task_id == parent.id, VideoChunk.video_id == parent.video_id,
        VideoChunk.id.in_(sorted(chunk_ids)),
    ).order_by(VideoChunk.id)
    if for_update:
        statement = statement.with_for_update()
    rows = {chunk_id: result for chunk_id, result in session.execute(statement)}
    valid = []
    for record in voiceprint.voiceprintList:
        for reference in record.references:
            result = rows.get(reference.chunk_id)
            if result is None or result.version < reference.source_version:
                continue
            event = next((event for event in dict(result.content or {}).get("timeline", [])
                          if event.get("event_type") == "DIALOGUE"
                          and _voiceprint_event_fingerprint(event) == reference.event_fingerprint), None)
            if (event is None or event.get("speaker") != record.speaker
                or not event["start_seconds"] <= reference.start_ms < reference.end_ms <= event["end_seconds"]):
                continue
            value = {"speaker": record.speaker, "model": record.model.model_dump(mode="json"),
                     "confidence": record.confidence, "source_start_ms": event["start_seconds"],
                     "source_end_ms": event["end_seconds"], **reference.model_dump(mode="json")}
            if value not in valid:
                valid.append(value)
    return valid


def _record_voiceprint_failure(
    parent: VideoParseTask, now: datetime, *, pending_failure: bool = False,
) -> tuple[int | None, bool]:
    """Persist real preparation failures, including interrupted active executions."""
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if dict(detail.get("voiceprint") or {}).get("ready") is True:
        return None, False
    enabled = (parent.parse_config.get("voiceprint") is not None
               or parent.parse_config.get("voiceprint_mode", "off") != "off")
    if phase.get("active") is not True and (not pending_failure or not enabled):
        return None, False
    failures = phase.get("failures", 0)
    if type(failures) is not int or failures < 0:
        raise ValueError("persisted Voiceprint failure count must be a nonnegative integer")
    failures += 1
    phase.update({"failures": failures, "active": False})
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    execution = _execution_config(parent.parse_config)
    parent.next_execute_time = now + task_retry_delay(
        execution["retry_initial_delay_seconds"], execution["retry_max_delay_seconds"], failures,
    )
    return failures, failures > execution["max_retry_count"]


def _ensure_runtime(detail_value: Mapping[str, Any] | None) -> dict[str, Any]:
    detail = deepcopy(dict(detail_value or {}))
    raw = detail.get(RUNTIME_DETAIL_KEY)
    runtime = deepcopy(raw) if isinstance(raw, dict) else {}
    model_calls = runtime.get("model_calls")
    if not isinstance(model_calls, dict):
        model_calls = {}
    used = model_calls.get("used_call_ids")
    active = model_calls.get("active")
    runtime["model_calls"] = {
        "used_call_ids": list(used) if isinstance(used, list) else [],
        "active": deepcopy(active) if isinstance(active, dict) else {},
    }
    prefix = runtime.get("prefix")
    if not isinstance(prefix, dict):
        prefix = {}
    committed_leases = prefix.get("committed_leases")
    if not isinstance(committed_leases, dict):
        committed_leases = {}
    runtime["prefix"] = {
        "next_chunk_sequence_no": int(prefix.get("next_chunk_sequence_no", 0)),
        "lease_owner": prefix.get("lease_owner"),
        "lease_version": int(prefix.get("lease_version", 0)),
        "lease_expire_time_ms": prefix.get("lease_expire_time_ms"),
        "execution_task_id": prefix.get("execution_task_id"),
        "next_execute_time_ms": prefix.get("next_execute_time_ms", 0),
        "retry_count": int(prefix.get("retry_count", 0)),
        "video_prefix_compact": str(prefix.get("video_prefix_compact", "")),
        "series_prefix_compact": str(prefix.get("series_prefix_compact", "")),
        "last_error": deepcopy(prefix.get("last_error")),
        "committed_leases": deepcopy(committed_leases),
    }
    if "accepted" in runtime:
        runtime["accepted"] = runtime["accepted"] is True
    detail[RUNTIME_DETAIL_KEY] = runtime
    return detail


def _series_prefix_frontier(
    session: Session,
    series_id: str,
) -> tuple[str, str] | None:
    """Return the first unfinished latest Video task and prior Series compact."""

    rows = session.execute(
        select(VideoParseTask, Video)
        .join(Video, Video.id == VideoParseTask.video_id)
        .where(
            Video.series_id == series_id,
            VideoParseTask.is_deleted.is_(False),
        )
        .order_by(
            Video.episode_order,
            Video.id,
            VideoParseTask.version.desc(),
            VideoParseTask.id,
        )
    ).all()
    latest: dict[str, tuple[VideoParseTask, Video]] = {}
    for task, video in rows:
        latest.setdefault(video.id, (task, video))

    previous_compact = ""
    for task, _video in latest.values():
        if not task.parse_config.get("compact_enabled", False):
            continue
        runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
        if runtime.get("accepted") is not True:
            return None
        prefix = runtime["prefix"]
        if int(prefix["next_chunk_sequence_no"]) < task.chunk_count:
            return task.id, previous_compact
        previous_compact = str(prefix.get("series_prefix_compact", ""))
    return None


def _latest_video_task_id(session: Session, video_id: str) -> str | None:
    return session.scalar(
        select(VideoParseTask.id)
        .where(
            VideoParseTask.video_id == video_id,
            VideoParseTask.is_deleted.is_(False),
        )
        .order_by(
            VideoParseTask.version.desc(),
            VideoParseTask.id,
        )
        .limit(1)
    )


def _index_receipt(
    content: ChunkParseContent,
    parse_task_id: str,
    video_chunk_id: str,
) -> dict[str, Any]:
    detail = content.detail if isinstance(content.detail, Mapping) else {}
    envelope = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
    receipt = envelope.get("receipt") if isinstance(envelope, Mapping) else None
    if not isinstance(receipt, Mapping):
        raise VideoParseConflictError(
            "COMPLETE Video Parse V2 Chunk has no index receipt"
        )
    version = envelope.get("content_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != content.version
    ):
        raise VideoParseConflictError(
            "Video Parse V2 index receipt differs from current content version"
        )
    return _validate_index_receipt(receipt, parse_task_id, video_chunk_id, content.version)


def _normalize_event_index_target(target: Mapping[str, Any]) -> dict[str, Any]:
    from engine.rag.indexing.events import EventIndexingContract

    if (not isinstance(target, Mapping)
            or set(target) != {"index_name", "contract_id", "contract"}
            or not isinstance(target.get("index_name"), str)
            or not target["index_name"].strip()):
        raise VideoParseConflictError("Event index target is incomplete")
    try:
        contract = EventIndexingContract.model_validate(target["contract"])
    except (TypeError, ValueError) as error:
        raise VideoParseConflictError("Event index target contract is invalid") from error
    if contract.contract_id != target["contract_id"]:
        raise VideoParseConflictError("Event index target fingerprint differs")
    return {"index_name": target["index_name"], "contract_id": contract.contract_id,
            "contract": contract.as_dict()}


def _require_event_index_target(parent: VideoParseTask, expected: Mapping[str, Any] | None) -> None:
    current = (parent.detail or {}).get(EVENT_INDEX_TARGET_DETAIL_KEY)
    if current is None and expected is None:
        return
    if expected is None or current != _normalize_event_index_target(expected):
        raise VideoParseConflictError("Event index target changed during execution")


def _require_event_publication(
    session: Session, parent: VideoParseTask, child: ChunkParseTask,
    content: ChunkParseContent, receipt: Mapping[str, Any], target: Mapping[str, Any],
    *, chunk: VideoChunk | None = None,
) -> None:
    from engine.rag.indexing.events import (
        EventIndexingContract, build_event_index_receipt, plan_event_index,
    )

    _require_event_index_target(parent, target)
    contract = EventIndexingContract.model_validate(target["contract"])
    chunk = chunk if chunk is not None else session.get(VideoChunk, child.chunk_id)
    if chunk is None:
        raise VideoParseConflictError("Event source Chunk no longer exists")
    plans = plan_event_index(
        parse_task_id=parent.id, video_chunk_id=child.chunk_id, version=content.version,
        result=VideoParseResult.model_validate(content.content),
        chunk_start_ms=chunk.start_ms, chunk_end_ms=chunk.end_ms, contract=contract,
    )
    expected = build_event_index_receipt(target["index_name"], contract, plans).as_dict()
    if receipt.get("events") != expected:
        raise VideoParseConflictError("Event publication is incomplete or differs from the current source")


def _validate_index_receipt(
    receipt: Mapping[str, Any],
    parse_task_id: str,
    video_chunk_id: str,
    content_version: int,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise VideoParseConflictError("Video Parse V2 index receipt must be an object")
    expected = {
        "parse_task_id": parse_task_id,
        "video_chunk_id": video_chunk_id,
        "content_version": content_version,
    }
    if any(receipt.get(name) != value for name, value in expected.items()):
        raise VideoParseConflictError(
            "Video Parse V2 index receipt differs from current Chunk identity"
        )
    version = receipt.get("content_version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise VideoParseConflictError(
            "Video Parse V2 index receipt has invalid content_version"
        )
    generation = receipt.get("index_generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise VideoParseConflictError(
            "Video Parse V2 index receipt has invalid index_generation"
        )
    normalized: dict[str, Any] = {**expected, "index_generation": generation}
    if receipt.get("events") is not None:
        from engine.rag.indexing.events import EventIndexReceipt
        try:
            normalized["events"] = EventIndexReceipt.model_validate(receipt["events"]).as_dict()
        except (TypeError, ValueError) as error:
            raise VideoParseConflictError("Event index receipt is invalid") from error
    if receipt.get("ordered_frames_manifest") is not None:
        normalized["ordered_frames_manifest"] = deepcopy(receipt["ordered_frames_manifest"])
    return normalized


def _require_matching_retrieval_identity(
    identity: VideoChunkRetrievalIdentity,
    *,
    video_chunk_id: str,
    child: ChunkParseTask,
    task: VideoParseTask,
    series: Series,
    video: Video,
    content: ChunkParseContent,
) -> None:
    mismatches: list[str] = []
    for name, actual, expected in (
        ("tenantId", identity.tenant_id, series.tenant_id),
        ("seriesId", identity.series_id, series.id),
        ("videoId", identity.video_id, video.id),
        ("parseTaskId", identity.parse_task_id, child.video_task_id),
        ("videoChunkId", video_chunk_id, child.chunk_id),
        ("contentVersion", identity.content_version, content.version),
        ("indexGeneration", identity.index_generation, task.target_index_generation),
    ):
        if actual != expected:
            mismatches.append(name)
    if mismatches:
        raise VideoParseConflictError(
            "retrieval candidate disagrees with current MySQL fields: "
            + ", ".join(mismatches)
        )


def _chunk_retrieval_facts(
    chunk: VideoChunk,
    content: ChunkParseContent,
    identity: VideoChunkRetrievalIdentity,
) -> ChunkRetrievalFacts:
    try:
        return ChunkRetrievalFacts(
            video_chunk_id=chunk.id,
            retrieval_identity=identity,
            sequence_no=chunk.sequence_no,
            start_seconds=chunk.start_ms,
            end_seconds=chunk.end_ms,
            result=VideoParseResult.model_validate(content.content),
        )
    except ValueError as exc:
        raise VideoParseConflictError(
            "retrieval candidate content is not a valid V2 result"
        ) from exc


def _video_parse_evidence(
    series: Series,
    video: Video,
    chunk: VideoChunk,
    task: VideoParseTask,
    content: ChunkParseContent,
    result: VideoParseResult,
    *,
    retrieval_text: str,
    identity: VideoChunkRetrievalIdentity,
) -> VideoChunkEvidence:
    transcript: list[TranscriptCueView] = []
    for event in result.timeline:
        if not isinstance(event, DialogueEvent):
            continue
        variants = [
            item for item in event.text_variants if item.role == "ORIGINAL"
        ] or event.text_variants[:1]
        transcript.extend(
            TranscriptCueView(
                speaker=event.speaker,
                text=variant.text,
                start_seconds=event.start_seconds,
                end_seconds=event.end_seconds,
            )
            for variant in variants
        )

    events = [event.model_dump(mode="json") for event in result.timeline]
    event_text = "\n".join(event.description for event in result.timeline).strip()
    detail = content.detail if isinstance(content.detail, Mapping) else {}
    compact = detail.get("compact")
    video_compact = compact.strip() if isinstance(compact, str) else ""
    summary = (
        retrieval_text.strip()
        or video_compact
        or event_text
        or "该片段没有结构化文字事件。"
    )
    models = task.parse_config.get("models")
    understanding = (
        models.get("understanding") if isinstance(models, Mapping) else None
    )
    model_provider = (
        understanding.get("source") if isinstance(understanding, Mapping) else None
    )
    model_name = (
        understanding.get("model") if isinstance(understanding, Mapping) else None
    )
    return VideoChunkEvidence(
        series_id=series.id,
        tenant_id=series.tenant_id,
        series_name=series.name,
        series_description=series.description,
        series_status="READY",
        important_extra_data=series.important_extra_data,
        character_bible=series.character_bible,
        video_id=video.id,
        episode_order=video.episode_order,
        original_filename=video.filename,
        original_source_uri=video.source_uri,
        inference_proxy_uri=video.proxy_uri,
        content_hash=video.hash,
        duration_seconds=video.duration_ms,
        upload_status=video.upload_status,
        parse_status=task.status,
        video_chunk_id=chunk.id,
        sequence_no=chunk.sequence_no,
        start_seconds=chunk.start_ms,
        end_seconds=chunk.end_ms,
        analysis_window_start_seconds=chunk.window_start_ms,
        analysis_window_end_seconds=chunk.window_end_ms,
        chunk_media_ref=chunk.chunk_media_ref,
        analysis_window_media_ref=chunk.analysis_window_media_ref,
        transcript=transcript,
        baseline_visual_caption=chunk.baseline_visual_caption,
        keyframe_uris=chunk.keyframe_uris,
        all_time_events=events,
        all_time_event_text=event_text,
        retrieval_text=retrieval_text,
        video_compact=video_compact,
        context_before_compact="",
        context_after_compact=video_compact,
        window_summary=summary,
        model_provider=(
            model_provider if isinstance(model_provider, str) else None
        ),
        model_name=model_name if isinstance(model_name, str) else None,
        pipeline_version=task.pipeline_version,
        is_fallback=False,
        parse_provenance={
            "parseTaskId": identity.parse_task_id,
            "contentVersion": identity.content_version,
            "indexGeneration": identity.index_generation,
        },
        parsed_at=datetime_to_unix_time_ms(content.update_date),
    )


def _current_prefix_lease(
    task: VideoParseTask | None,
    lease: PrefixParseLease,
    now: datetime,
) -> dict[str, Any] | None:
    if task is None or task.is_deleted:
        return None
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    prefix = deepcopy(runtime["prefix"])
    expires_ms = prefix.get("lease_expire_time_ms")
    if (
        prefix.get("lease_owner") != lease.worker_id
        or prefix.get("lease_version") != lease.lease_version
        or not isinstance(expires_ms, int)
        or unix_time_ms_to_datetime(expires_ms) <= now
        or prefix.get("next_chunk_sequence_no") != lease.sequence_no
        or prefix.get("execution_task_id") != lease.chunk_parse_task_id
    ):
        return None
    return prefix


def _model_execution_identity(
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
) -> tuple[str, str, int]:
    if isinstance(lease, VideoParseLease):
        return "video", lease.video_parse_task_id, lease.lease_version
    if isinstance(lease, ChunkParseLease):
        return "chunk", lease.chunk_parse_task_id, lease.lease_version
    if isinstance(lease, IntervalParseLease):
        return "interval", lease.interval_parse_task_id, lease.lease_version
    return "prefix", lease.chunk_parse_task_id, lease.lease_version


def _stored_model_execution_identity(
    slot: Mapping[str, Any],
) -> tuple[str, str, int] | None:
    kind = slot.get("execution_kind")
    task_id = slot.get("execution_task_id")
    version = slot.get("execution_lease_version")
    if (
        not isinstance(kind, str)
        or not isinstance(task_id, str)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        return None
    return kind, task_id, version


def _lock_model_execution_lease(
    session: Session,
    lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
    now: datetime,
) -> VideoParseTask:
    """Serialize the task budget and then fence the actual execution lease."""
    parent = _locked_parent_task(session, lease.video_parse_task_id)

    if isinstance(lease, ChunkParseLease):
        task = session.scalar(
            select(ChunkParseTask)
            .where(
                ChunkParseTask.id == lease.chunk_parse_task_id,
                ChunkParseTask.video_task_id
                == lease.video_parse_task_id,
            )
            .with_for_update()
        )
        if not _matching_chunk_lease(task, lease, now):
            raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    elif isinstance(lease, IntervalParseLease):
        interval = session.scalar(
            select(IntervalParseTask)
            .where(
                IntervalParseTask.id == lease.interval_parse_task_id,
                IntervalParseTask.video_task_id
                == lease.video_parse_task_id,
            )
            .with_for_update()
        )
        if (
            not _matching_interval_lease(interval, lease, now)
            or interval.status != lease.phase
        ):
            raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    if isinstance(lease, VideoParseLease):
        if (
            parent.lease_owner != lease.worker_id
            or parent.lease_version != lease.lease_version
            or parent.lease_expire_time is None
            or parent.lease_expire_time <= now
        ):
            raise VideoParseLeaseLostError("VideoParseTask lease is no longer current")
    elif isinstance(lease, PrefixParseLease):
        if (
            _current_prefix_lease(parent, lease, now) is None
            or _latest_video_task_id(session, parent.video_id) != parent.id
        ):
            raise VideoParseLeaseLostError("Prefix lease is no longer current")
    return parent


def _execution_config(parse_config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = parse_config.get("execution")
    if not isinstance(value, dict):
        raise VideoParseConflictError("parse_config.execution is missing")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise VideoParseConflictError(f"parse_config.{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VideoParseConflictError(
            f"parse_config.{name} must be a nonnegative integer"
        )
    return value


def _error_detail(error: str) -> dict[str, str]:
    return {"message": (error.strip() or "video parse worker failed")[:8192]}
