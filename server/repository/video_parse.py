from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agent.video_parse_v2.input import ParseConfig
from engine.parsev2.schemas import DialogueEvent, VideoParseResult
from engine.parsev2.validation import parse_content_fingerprint, validate_result_time_range
from engine.rag.contracts import (
    TranscriptCueView,
    VideoChunkEvidence,
    VideoChunkRetrievalIdentity,
)
from engine.rag.indexing.projection import project_chunk_index
from engine.rag.retrieval.contracts import ChunkRetrievalFacts
from engine.rag.encoding import validate_sparse_weights

from ..model.identifiers import derive_video_chunk_id
from ..model.dto.jobDTO import (
    ChunkParseContentState,
    ChunkExecutionState,
    ChunkParseLease,
    IndexableChunkState,
    IntervalIndexingSnapshot,
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


RUNTIME_DETAIL_KEY = "_worker_runtime_v2"
INDEX_EMBEDDINGS_DETAIL_KEY = "_index_embeddings_v2"
_REFINE_SUBMITTED = {
    IntervalParseTaskStatus.INDEXING.value,
    IntervalParseTaskStatus.INDEXING_FAILED.value,
    IntervalParseTaskStatus.COMPLETE.value,
}


class VideoParseRepositoryError(RuntimeError):
    pass


class VideoParseTaskNotFoundError(VideoParseRepositoryError):
    pass


class VideoParseConflictError(VideoParseRepositoryError):
    pass


class VideoParseSnapshotConflictError(VideoParseConflictError):
    """A refinement input changed; refresh facts without rerunning observations."""


class VideoParseLeaseLostError(VideoParseRepositoryError):
    pass


class ModelCallBudgetExhaustedError(VideoParseRepositoryError):
    pass


class ModelCallConcurrencyBusyError(VideoParseRepositoryError):
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
            runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
            parent_error = None
            if runtime.get("accepted") is True:
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
                         if child.status == ChunkParseTaskStatus.COMPLETE.value and content is not None else None),
                content_version=content.version if content is not None else None,
                last_error=deepcopy(child.last_error),
                parent_error=parent_error,
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
                    ChunkParseTask.status == ChunkParseTaskStatus.COMPLETE.value,
                )
            )
            if content is None:
                raise VideoParseConflictError("videoChunkId has no COMPLETE V2 content")
            receipt = _index_receipt(content, task_id, chunk_id)
            identity = VideoChunkRetrievalIdentity(
                tenant_id=workspace,
                series_id=series.id,
                video_id=video.id,
                parse_task_id=task_id,
                content_version=content.version,
                index_generation=receipt["index_generation"],
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
        scope. Every retained task must have one complete, internally consistent
        index receipt per Chunk so query execution never guesses an ES contract.
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
                    ChunkParseTask.status
                    == ChunkParseTaskStatus.COMPLETE.value,
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
                    receipt = _index_receipt(content, task.id, chunk.id)
                    if require_sparse:
                        envelope = (content.detail or {}).get(INDEX_EMBEDDINGS_DETAIL_KEY)
                        representations = envelope.get("representations") if isinstance(envelope, Mapping) else None
                        sparse = representations.get("sparse") if isinstance(representations, Mapping) else None
                        if not isinstance(sparse, Mapping) or sparse.get("channel") != "sparse":
                            raise VideoParseConflictError("selected V2 task has no completed Sparse representation")
                        try:
                            validate_sparse_weights(sparse.get("weights"))
                        except ValueError as error:
                            raise VideoParseConflictError("selected V2 task has an invalid Sparse representation") from error
                    contract = (
                        receipt["index_generation"],
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
                    "selected Video Parse V2 tasks have no index receipt"
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
        """Read formal facts and display evidence behind the same current receipt."""

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
                    ChunkParseTask.status
                    == ChunkParseTaskStatus.COMPLETE.value,
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
                series, video, chunk, task, content = row
                if chunk.video_id != video.id:
                    raise VideoParseConflictError(
                        "retrieval candidate Chunk belongs to a different Video"
                    )
                if _latest_video_task_id(session, video.id) != task.id:
                    raise VideoParseConflictError(
                        "retrieval candidate no longer belongs to the latest V2 task"
                    )
                receipt = _index_receipt(content, task.id, chunk.id)
                _require_matching_retrieval_identity(
                    identity,
                    video_chunk_id=video_chunk_id,
                    receipt=receipt,
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
                        other_receipt = _index_receipt(
                            other_content, task.id, other_chunk.id
                        )
                        if other_receipt["index_generation"] != identity.index_generation:
                            raise VideoParseConflictError(
                                "retrieval context uses a different index generation"
                            )
                        other_identity = VideoChunkRetrievalIdentity(
                            tenant_id=other_series.tenant_id,
                            series_id=other_series.id,
                            video_id=other_video.id,
                            parse_task_id=task.id,
                            content_version=other_content.version,
                            index_generation=other_receipt["index_generation"],
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
            for start, count in interval_specs:
                members = chunk_tasks[start:start + count]
                member_ids = [member.id for member in members]
                interval = by_start.get(start)
                if interval is None:
                    ready_mask = 0
                    for index, member in enumerate(members):
                        if member.status == ChunkParseTaskStatus.COMPLETE.value:
                            ready_mask |= interval_ready_mask_bit(index, count)
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

            task.status = ChunkParseTaskStatus.COMPLETE.value
            task.finish_time = committed_at
            task.lease_owner = None
            task.lease_expire_time = None
            task.last_error = None
            task.update_time = datetime_to_unix_time_ms(committed_at)
            session.flush()
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
        detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Fence every input snapshot, validate all candidates, and commit once."""

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
            if set(input_versions) != expected_members or set(input_fingerprints) != expected_members:
                raise VideoParseConflictError("refinement input identity must cover every Interval member")
            for chunk_task, _chunk, result in rows:
                if (type(input_versions[chunk_task.id]) is not int
                        or result.version != input_versions[chunk_task.id]
                        or parse_content_fingerprint(dict(result.content or {})) != input_fingerprints[chunk_task.id]):
                    raise VideoParseSnapshotConflictError("Refine member content snapshot changed")
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
            for chunk_task, _chunk, result in rows:
                current = deepcopy(dict(result.content or {}))
                changed = member_updates.get(chunk_task.id, {})
                if set(changed) - {"persons", "timeline"}:
                    raise VideoParseConflictError("refinement can only commit persons and timeline")
                candidate = {**current, **deepcopy(dict(changed))}
                candidates[chunk_task.id] = validate_result_time_range(
                    VideoParseResult.model_validate(candidate), start_seconds=start_ms, end_seconds=end_ms,
                ).model_dump(mode="json")
            member_scope = list(
                range(
                    interval.start_sequence_no,
                    interval.start_sequence_no + interval.chunk_count,
                )
            )
            changed_member_ids: list[str] = []
            for chunk_task, _chunk, result in rows:
                current = deepcopy(dict(result.content or {}))
                updated = candidates[chunk_task.id]
                content_changed = updated != current
                if content_changed:
                    result.content = updated
                    result.version += 1
                    changed_member_ids.append(chunk_task.id)
                current_detail = deepcopy(dict(result.detail or {}))
                current_detail["scope"] = _ordered_int_union(
                    current_detail.get("scope"), member_scope
                )
                result.detail = current_detail
                result.update_time = datetime_to_unix_time_ms(committed_at)

            session.add(IntervalParseContent(
                interval_task_id=interval.id,
                content=deepcopy(dict(interval_content)),
                detail={
                    **deepcopy(dict(detail or {})),
                    "indexed_versions": {},
                    "refinement_committed_lease_version": lease.lease_version,
                },
                create_time=datetime_to_unix_time_ms(committed_at),
                update_time=datetime_to_unix_time_ms(committed_at),
            ))
            interval.status = IntervalParseTaskStatus.INDEXING.value
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.last_error = None
            interval.finish_time = None
            interval.next_execute_time = committed_at
            interval.update_time = datetime_to_unix_time_ms(committed_at)

            reset_intervals = _reset_indexing_for_changed_members(
                session,
                interval.video_task_id,
                changed_member_ids,
                committed_at,
                exclude_interval_id=interval.id,
                locked_intervals=locked_intervals,
            )
            session.flush()
            return tuple(sorted({interval.id, *reset_intervals}))

    def load_interval_indexing_snapshot(
        self,
        lease: IntervalParseLease,
        *,
        now: datetime | None = None,
    ) -> IntervalIndexingSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            interval = _interval_for_lease(session, lease, loaded_at)
            if interval.status != IntervalParseTaskStatus.INDEXING.value:
                raise VideoParseLeaseLostError("Interval is no longer indexing")
            result = session.get(IntervalParseContent, interval.id)
            if result is None:
                raise VideoParseConflictError("INDEXING Interval has no Refine result")
            indexed_versions = _indexed_versions(result.detail)
            submitted = _submitted_intervals_by_member(
                session, interval.video_task_id
            )
            parent = session.get(VideoParseTask, interval.video_task_id)
            video = session.get(Video, parent.video_id) if parent is not None else None
            series = session.get(Series, video.series_id) if video is not None else None
            if parent is None or video is None or series is None:
                raise VideoParseConflictError("Interval parent identity is incomplete")

            chunks: list[IndexableChunkState] = []
            source_media_ref = (
                video.proxy_uri or video.source_uri
            )
            for chunk_task, chunk, content in _member_content_rows(
                session, interval, for_update=False
            ):
                covering = submitted.get(chunk_task.id, ())
                if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                    continue
                chunks.append(IndexableChunkState(
                    chunk_parse_task_id=chunk_task.id,
                    video_chunk_id=chunk.id,
                    sequence_no=chunk.sequence_no,
                    content=deepcopy(dict(content.content or {})),
                    content_version=content.version,
                    content_detail=deepcopy(dict(content.detail or {})),
                    source_media_ref=source_media_ref,
                    chunk_media_ref=chunk.chunk_media_ref,
                    analysis_window_media_ref=chunk.analysis_window_media_ref,
                    tenant_id=series.tenant_id,
                    series_id=series.id,
                    video_id=video.id,
                    start_seconds=chunk.start_ms,
                    end_seconds=chunk.end_ms,
                ))
            return IntervalIndexingSnapshot(
                interval_parse_task_id=interval.id,
                video_parse_task_id=interval.video_task_id,
                chunks=tuple(chunks),
                indexed_versions=indexed_versions,
            )

    def complete_interval_indexing(
        self,
        lease: IntervalParseLease,
        indexed_versions: Mapping[str, int],
        *,
        dependency_poll_delay: timedelta = timedelta(seconds=1),
        now: datetime | None = None,
    ) -> VideoParseTaskState:
        if dependency_poll_delay <= timedelta(0):
            raise ValueError("dependency_poll_delay must be positive")
        completed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            _locked_parent_intervals(session, lease.video_parse_task_id)
            interval = _locked_interval_lease(
                session, lease, completed_at, IntervalParseTaskStatus.INDEXING.value
            )
            result = session.get(
                IntervalParseContent, interval.id, with_for_update=True
            )
            if result is None:
                raise VideoParseConflictError("INDEXING Interval has no Refine result")
            recorded = _indexed_versions(result.detail)
            submitted = _submitted_intervals_by_member(
                session, interval.video_task_id, for_update=True
            )
            rows = _member_content_rows(session, interval, for_update=True)
            current_versions = {task.id: content.version for task, _chunk, content in rows}
            for chunk_task_id, version in indexed_versions.items():
                if chunk_task_id not in interval.chunk_task_ids:
                    raise VideoParseConflictError("indexed Chunk is not an Interval member")
                covering = submitted.get(chunk_task_id, ())
                if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                    raise VideoParseConflictError("indexed Chunk is not released")
                if current_versions[chunk_task_id] != version:
                    raise VideoParseConflictError("indexed Chunk content version changed")
                recorded[chunk_task_id] = version

            all_current = all(
                recorded.get(task_id) == version
                for task_id, version in current_versions.items()
            )
            result_detail = {
                **deepcopy(dict(result.detail or {})),
                "indexed_versions": recorded,
            }
            # Partial publication also finishes this execution. Preserve its
            # receipt so an observed timing sample can close after a later lease.
            finished_leases = result_detail.get("indexing_finished_lease_versions", [])
            if not isinstance(finished_leases, list):
                finished_leases = []
            if lease.lease_version not in finished_leases:
                finished_leases.append(lease.lease_version)
            result_detail["indexing_finished_lease_versions"] = finished_leases
            if all_current:
                result_detail["indexing_committed_lease_version"] = (
                    lease.lease_version
                )
            result.detail = result_detail
            result.update_time = datetime_to_unix_time_ms(completed_at)
            interval.status = (
                IntervalParseTaskStatus.COMPLETE.value
                if all_current
                else IntervalParseTaskStatus.INDEXING.value
            )
            interval.finish_time = completed_at if all_current else None
            interval.lease_owner = None
            interval.lease_expire_time = None
            interval.last_error = None
            interval.next_execute_time = (
                completed_at if all_current else completed_at + dependency_poll_delay
            )
            interval.update_time = datetime_to_unix_time_ms(completed_at)
            task = session.get(VideoParseTask, interval.video_task_id)
            if task is None:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {interval.video_task_id!r} does not exist"
                )
            session.flush()
            return _task_state(task)

    def save_index_embeddings(
        self,
        lease: IntervalParseLease,
        chunk_parse_task_id: str,
        content_version: int,
        representations: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
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
            _locked_parent_intervals(session, lease.video_parse_task_id)
            interval = _locked_interval_lease(
                session, lease, saved_at, IntervalParseTaskStatus.INDEXING.value
            )
            if chunk_parse_task_id not in interval.chunk_task_ids:
                raise VideoParseConflictError("embedding target is not an Interval member")
            covering = _submitted_intervals_by_member(
                session, interval.video_task_id, for_update=True
            ).get(chunk_parse_task_id, ())
            if not covering or any(item.status not in _REFINE_SUBMITTED for item in covering):
                raise VideoParseConflictError("embedding target is not released")
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
            cached_representations.update(deepcopy(dict(representations)))
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
        self,
        video_parse_task_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Reset submitted Intervals for an index-only rebuild.

        Refine results, Chunk completion, indexing checkpoints, and reusable
        embeddings remain intact. Incrementing each Interval lease version
        explicitly fences any index Worker that was active before the reset.
        """

        requested_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            intervals = _locked_parent_intervals(session, video_parse_task_id)
            task = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == video_parse_task_id)
                .with_for_update()
            )
            if task is None or task.is_deleted:
                raise VideoParseTaskNotFoundError(
                    f"VideoParseTask {video_parse_task_id!r} does not exist"
                )
            if _latest_video_task_id(session, task.video_id) != task.id:
                raise VideoParseConflictError(
                    "only the latest VideoParseTask can rebuild its index"
                )
            failure_status = _video_failure_status(task) or (
                task.status if task.status in {
                    VideoParseTaskStatus.PARSING_FAILED.value,
                    VideoParseTaskStatus.INDEXING_FAILED.value,
                } else None
            )
            if failure_status is not None and (
                failure_status != VideoParseTaskStatus.INDEXING_FAILED.value
                or task.status != VideoParseTaskStatus.INDEXING_FAILED.value
                or any(interval.lease_owner is not None for interval in intervals)
                or session.scalar(select(ChunkParseTask.id).where(
                    ChunkParseTask.video_task_id == task.id,
                    ChunkParseTask.lease_owner.is_not(None),
                ).limit(1)) is not None
            ):
                raise VideoParseConflictError(
                    "index rebuild requires a settled indexing failure; parsing failures require retry"
                )

            refined_interval_ids = set(session.scalars(
                select(IntervalParseContent.interval_task_id.label("interval_parse_task_id")).where(
                    IntervalParseContent.interval_task_id.in_(
                        tuple(interval.id for interval in intervals)
                    )
                )
            )) if intervals else set()
            reset: list[str] = []
            for interval in intervals:
                if (
                    interval.status not in _REFINE_SUBMITTED
                    or interval.id not in refined_interval_ids
                ):
                    continue
                interval.status = IntervalParseTaskStatus.INDEXING.value
                interval.next_execute_time = requested_at
                interval.retry_count = 0
                interval.lease_owner = None
                interval.lease_version += 1
                interval.lease_expire_time = None
                interval.finish_time = None
                interval.last_error = None
                interval.update_time = datetime_to_unix_time_ms(requested_at)
                reset.append(interval.id)

            if reset and failure_status is not None:
                detail = deepcopy(dict(task.detail or {}))
                runtime = detail.get(RUNTIME_DETAIL_KEY)
                if isinstance(runtime, dict):
                    runtime.pop("failure_status", None)
                task.detail = detail
                task.status = VideoParseTaskStatus.INDEXING.value
            task = _aggregate_video_task(session, task.id, requested_at)
            if reset:
                # A completed or exhausted coordinator must be claimable again
                # so it can aggregate the rebuilt Intervals back to COMPLETE.
                task.next_execute_time = requested_at
                task.attempt_count = 0
                task.last_error = None
            session.flush()
            return tuple(reset)

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
            phase, kind, task_id = "understand", "chunk", lease.chunk_parse_task_id
            model = ChunkParseTask
        elif isinstance(lease, IntervalParseLease) and lease.phase in {"REFINING", "INDEXING"}:
            phase = "refine" if lease.phase == "REFINING" else "index"
            kind, task_id = "interval", lease.interval_parse_task_id
            model = IntervalParseTask
        else:
            raise ValueError("timing supports only Chunk Understanding and Interval Refine/Index")

        sampled_at = now if now is not None else now_utc()
        identity = {
            "phase": phase, "execution_kind": kind, "execution_task_id": task_id,
            "lease_version": lease.lease_version, "worker_id": lease.worker_id,
            "started_at_ms": started_at_ms,
        }
        key = f"{phase}:{task_id}:{lease.lease_version}"
        with self._session_factory.begin() as session:
            # Match the model-call writer's child -> parent locking order.
            child = session.scalar(
                select(model).where(model.id == task_id).with_for_update()
            )
            if child is None or child.video_task_id != lease.video_parse_task_id:
                return False
            if isinstance(lease, ChunkParseLease):
                active = _matching_chunk_lease(child, lease, sampled_at)
                committed = (
                    child.chunk_id == lease.video_chunk_id
                    and child.status == ChunkParseTaskStatus.COMPLETE.value
                    and child.lease_version == lease.lease_version
                )
            else:
                active = _matching_interval_lease(child, lease, sampled_at)
                content = session.get(IntervalParseContent, task_id) if finished else None
                receipt = content.detail if content is not None and isinstance(content.detail, dict) else {}
                finished_leases = receipt.get("indexing_finished_lease_versions", [])
                committed_version = (
                    receipt.get("refinement_committed_lease_version") == lease.lease_version
                    if phase == "refine" else (
                        receipt.get("indexing_committed_lease_version") == lease.lease_version
                        or isinstance(finished_leases, list) and lease.lease_version in finished_leases
                    )
                )
                committed = bool(
                    committed_version
                    and tuple(child.chunk_task_ids) == lease.chunk_parse_task_ids
                    and child.start_sequence_no == lease.start_sequence_no
                    and child.chunk_count == lease.chunk_count
                )
            if not active and not (finished and committed):
                return False
            parent = session.scalar(
                select(VideoParseTask)
                .where(VideoParseTask.id == lease.video_parse_task_id)
                .with_for_update()
            )
            if parent is None or parent.is_deleted:
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
            max_calls = _positive_int(execution.get("max_model_calls"), "max_model_calls")
            max_concurrent = _positive_int(
                execution.get("max_concurrent_model_calls"),
                "max_concurrent_model_calls",
            )
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
                # Legacy snapshots retain max_model_calls; only concurrency
                # limits admission, while request history continues to grow.
                if len(active) >= max_concurrent:
                    raise ModelCallConcurrencyBusyError(
                        "VideoParseTask model-call concurrency is full"
                    )
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
                max_concurrent_calls=max_concurrent,
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
                    ChunkParseTask.status == ChunkParseTaskStatus.COMPLETE.value,
                )
            ).one_or_none()
            if chunk_row is None:
                return None
            chunk_task, chunk, content = chunk_row
            covering = _submitted_intervals_by_member(session, task.id).get(
                chunk_task.id, ()
            )
            if not covering or any(
                item.status not in _REFINE_SUBMITTED for item in covering
            ):
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
                reset = _reset_indexing_for_changed_members(
                    session,
                    task.id,
                    [chunk_task.id],
                    committed_at,
                    locked_intervals=locked_intervals,
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
        and task.status == ChunkParseTaskStatus.UNDERSTANDING.value
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


def _reset_indexing_for_changed_members(
    session: Session,
    video_parse_task_id: str,
    changed_member_ids: Sequence[str],
    changed_at: datetime,
    *,
    exclude_interval_id: str | None = None,
    locked_intervals: Sequence[IntervalParseTask] | None = None,
) -> tuple[str, ...]:
    changed = set(changed_member_ids)
    if not changed:
        return ()
    reset: list[str] = []
    intervals = (
        tuple(locked_intervals)
        if locked_intervals is not None
        else tuple(_locked_parent_intervals(session, video_parse_task_id))
    )
    for interval in intervals:
        if interval.id == exclude_interval_id or not changed.intersection(interval.chunk_task_ids):
            continue
        if interval.status not in _REFINE_SUBMITTED:
            continue
        interval.status = IntervalParseTaskStatus.INDEXING.value
        interval.finish_time = None
        interval.lease_owner = None
        interval.lease_expire_time = None
        interval.next_execute_time = changed_at
        interval.update_time = datetime_to_unix_time_ms(changed_at)
        reset.append(interval.id)
    return tuple(reset)


def _video_failure_status(task: VideoParseTask) -> str | None:
    runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY)
    status = runtime.get("failure_status") if isinstance(runtime, dict) else None
    return status if status in {
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    } else None


def _aggregate_video_task(
    session: Session,
    video_parse_task_id: str,
    now: datetime,
    *,
    release_terminal_lease: bool = False,
    failure_status: str | None = None,
) -> VideoParseTask:
    task = session.scalar(
        select(VideoParseTask)
        .where(VideoParseTask.id == video_parse_task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise VideoParseTaskNotFoundError(
            f"VideoParseTask {video_parse_task_id!r} does not exist"
        )
    runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY]
    if runtime.get("realtime_only") is True and runtime.get("accepted") is not True:
        # Base-only requests have independent target failures. They must never
        # enter the full Parse drain state and block unrelated missing Chunks.
        detail = _ensure_runtime(task.detail)
        detail[RUNTIME_DETAIL_KEY].pop("failure_status", None)
        task.detail = detail
        task.status = (VideoParseTaskStatus.PREPARING.value
                       if task.lease_owner is not None else VideoParseTaskStatus.PENDING.value)
        task.finish_time = None
        task.update_time = datetime_to_unix_time_ms(now)
        video = session.get(Video, task.video_id)
        if video is not None:
            video.parse_status = task.status
        return task
    previous_status = task.status
    chunks = list(session.scalars(
        select(ChunkParseTask).where(
            ChunkParseTask.video_task_id == video_parse_task_id
        )
    ))
    intervals = list(session.scalars(
        select(IntervalParseTask).where(
            IntervalParseTask.video_task_id == video_parse_task_id
        )
    ))
    if not chunks or len(chunks) != task.chunk_count or not intervals:
        status = VideoParseTaskStatus.PARSING.value
    elif any(item.status == ChunkParseTaskStatus.FAILED.value for item in chunks) or any(
        item.status == IntervalParseTaskStatus.PARSING_FAILED.value for item in intervals
    ):
        status = VideoParseTaskStatus.PARSING_FAILED.value
    elif any(item.status != ChunkParseTaskStatus.COMPLETE.value for item in chunks) or any(
        item.status in {
            IntervalParseTaskStatus.WAITING.value,
            IntervalParseTaskStatus.REFINING.value,
        }
        for item in intervals
    ):
        status = VideoParseTaskStatus.PARSING.value
    elif any(item.status == IntervalParseTaskStatus.INDEXING_FAILED.value for item in intervals):
        status = VideoParseTaskStatus.INDEXING_FAILED.value
    elif any(item.status == IntervalParseTaskStatus.INDEXING.value for item in intervals):
        status = VideoParseTaskStatus.INDEXING.value
    else:
        status = VideoParseTaskStatus.COMPLETE.value
    recorded_failure = _video_failure_status(task) or (
        task.status if task.status in {
            VideoParseTaskStatus.PARSING_FAILED.value,
            VideoParseTaskStatus.INDEXING_FAILED.value,
        } else None
    )
    failure_status = failure_status or recorded_failure
    if VideoParseTaskStatus.PARSING_FAILED.value in {status, recorded_failure, failure_status}:
        failure_status = VideoParseTaskStatus.PARSING_FAILED.value
    elif failure_status is None and status == VideoParseTaskStatus.INDEXING_FAILED.value:
        failure_status = status
    if failure_status is not None:
        detail = deepcopy(dict(task.detail or {}))
        detail.setdefault(RUNTIME_DETAIL_KEY, {})["failure_status"] = failure_status
        task.detail = detail
        status = failure_status
    persisted_status = status
    if failure_status is not None and task.status not in {
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    } and (
        any(child.lease_owner is not None for child in [*chunks, *intervals])
        or not release_terminal_lease
    ):
        # Only the coordinator or the queue's fresh drain transaction publishes
        # a new failure. An expired owner still needs row-locked reclamation.
        persisted_status = (
            VideoParseTaskStatus.PARSING.value
            if failure_status == VideoParseTaskStatus.PARSING_FAILED.value
            else VideoParseTaskStatus.INDEXING.value
        )
    elif not release_terminal_lease and task.lease_owner is not None:
        if status == VideoParseTaskStatus.PARSING_FAILED.value:
            persisted_status = VideoParseTaskStatus.PARSING.value
        elif status in {
            VideoParseTaskStatus.COMPLETE.value,
            VideoParseTaskStatus.INDEXING_FAILED.value,
        }:
            persisted_status = VideoParseTaskStatus.INDEXING.value
    task.status = persisted_status
    if persisted_status in {
        VideoParseTaskStatus.COMPLETE.value,
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    }:
        if previous_status not in {
            VideoParseTaskStatus.PARSING_FAILED.value,
            VideoParseTaskStatus.INDEXING_FAILED.value,
        } or task.finish_time is None:
            task.finish_time = now
    else:
        task.finish_time = None
    task.update_time = datetime_to_unix_time_ms(now)
    if release_terminal_lease and persisted_status in {
        VideoParseTaskStatus.COMPLETE.value,
        VideoParseTaskStatus.PARSING_FAILED.value,
        VideoParseTaskStatus.INDEXING_FAILED.value,
    }:
        task.lease_owner = None
        task.lease_expire_time = None
        task.heartbeat_time = None
    video = session.get(Video, task.video_id)
    if video is not None:
        video.parse_status = persisted_status
        video.parse_progress = (
            100 if persisted_status == VideoParseTaskStatus.COMPLETE.value else 0
        )
    return task


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


def _indexed_versions(detail: Mapping[str, Any] | None) -> dict[str, int]:
    raw = (detail or {}).get("indexed_versions", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


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
    if receipt.get("ordered_frames_manifest") is not None:
        normalized["ordered_frames_manifest"] = deepcopy(receipt["ordered_frames_manifest"])
    return normalized


def _require_matching_retrieval_identity(
    identity: VideoChunkRetrievalIdentity,
    *,
    video_chunk_id: str,
    receipt: Mapping[str, Any],
    series: Series,
    video: Video,
    content: ChunkParseContent,
) -> None:
    mismatches: list[str] = []
    for name, actual, expected in (
        ("tenantId", identity.tenant_id, series.tenant_id),
        ("seriesId", identity.series_id, series.id),
        ("videoId", identity.video_id, video.id),
        ("parseTaskId", identity.parse_task_id, receipt["parse_task_id"]),
        ("videoChunkId", video_chunk_id, receipt["video_chunk_id"]),
        ("contentVersion", identity.content_version, content.version),
        ("indexGeneration", identity.index_generation, receipt["index_generation"]),
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
    """Fence a child execution before locking its parent budget row.

    Chunk and Interval completion paths never aggregate or lock the parent.
    Prefix is the sole child-like path that also owns state on the parent row.
    """

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
    parent = session.scalar(
        select(VideoParseTask)
        .where(VideoParseTask.id == lease.video_parse_task_id)
        .with_for_update()
    )
    if parent is None or parent.is_deleted:
        raise VideoParseTaskNotFoundError(
            f"VideoParseTask {lease.video_parse_task_id!r} does not exist"
        )
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
