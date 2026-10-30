from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from agent.video_parse_v2.input import ParseConfig
from agent.video_summarize.state import VideoSummarizeInput, VideoSummaryResult
from engine.rag.contracts import VideoChunkEvidence, VideoChunkRetrievalIdentity

from ..constants.parse import MAX_INDEX_GENERATION, RUNTIME_DETAIL_KEY
from ..model.dto.jobDTO import (
    ChunkParseContentState,
    ChunkExecutionState,
    ChunkParseLease,
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
    VideoParseRetrievalScope,
    HydratedRetrievalCandidate,
    RealtimeChunkState,
    ParseExportChunkSnapshot,
    VideoParseTaskData,
    VideoSummarizeLease,
)
from ..model.job import (
    VideoParseTaskStatus,
)
from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store
from ..repository import video_parse_retrieval as read_store
from ..utils.time import datetime_to_unix_time_ms, now_utc
from ..utils.validation import canonical_uuid, require_nonempty_text
from .video_parse_errors import (
    VideoParseRepositoryError,
    VideoParseTaskNotFoundError,
    VideoParseConflictError,
    VideoParseSnapshotConflictError,
    VoiceprintRegistrationRejectedError,
    VideoParseLeaseLostError,
    ModelCallBudgetExhaustedError,
    ModelCallLeaseConflictError,
)
from .video_parse_task import (
    _task_input_state,
    _task_state,
    _locked_video_lease,
    _locked_parent_intervals,
    _advance_ready_chunks,
    _aggregate_video_task,
    _interval_specs,
    _interval_size,
    _ensure_runtime,
    _latest_video_task_id,
    _set_capacity_acquired,
    _requires_video_asr,
    _video_failure_status,
)
from .video_parse_content import (
    _normalize_event_index_target,
    INDEX_EMBEDDINGS_DETAIL_KEY,
    EVENT_INDEX_TARGET_DETAIL_KEY,
)
from .video_parse_prefix import invalidate_series_prefixes_from, _series_prefix_frontier
from . import video_parse_content as content_ops
from . import video_parse_prefix as prefix_ops
from . import video_parse_voiceprint as voiceprint_ops
from . import video_parse_model_calls as execution_ops
from . import video_parse_read as retrieval_ops
from . import video_parse_summarize as summary_ops
from ..repository.library import LibraryRepository


class VideoParsePersistenceService:
    """Transactional persistence for the V2 parse pipeline.

    Model and Elasticsearch calls must happen outside these methods. Every
    mutating method either fences a Worker lease or locks the parent task row.
    This service owns Session lifetime. Workflow operations consume DTOs and
    explicitly persist their changes through SQL repositories in that Session.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load_export_snapshot(
        self,
        *,
        tenant_id: str,
        video_ids: Sequence[str] = (),
        video_parse_task_ids: Sequence[str] = (),
    ) -> tuple[ParseExportChunkSnapshot, ...]:
        """Freeze complete formal content in one read, independently of indexing."""

        workspace = require_nonempty_text(tenant_id, "tenant_id")
        if bool(video_ids) == bool(video_parse_task_ids):
            raise ValueError("provide either video_ids or video_parse_task_ids")
        by_video = bool(video_ids)
        selected_ids = tuple(dict.fromkeys(
            canonical_uuid(value, "video_id" if by_video else "video_parse_task_id")
            for value in (video_ids if by_video else video_parse_task_ids)
        ))
        with self._session_factory() as session:
            rows = read_store.load_export_rows(
                session,
                selected_ids=selected_ids,
                by_video=by_video,
                workspace=workspace,
            )

        return retrieval_ops.export_snapshots(rows, selected_ids, by_video=by_video)

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
                    task = task_store.get_task(session, candidate_id, for_update=True, include_deleted=False)
                    if task is None:
                        continue
                video = task_store.get_video(session, video_id, for_update=True)
                series = task_store.get_series(session, video.series_id) if video is not None else None
                if video is None or series is None or series.tenant_id != tenant_id or video.upload_status != "COMPLETED":
                    raise VideoParseTaskNotFoundError("Video does not exist in this workspace")
                if _latest_video_task_id(session, video_id) != candidate_id:
                    # Release the Video lock before locking a newly observed
                    # parent so parent -> Video remains consistent everywhere.
                    continue
                runtime = _ensure_runtime(task.detail)[RUNTIME_DETAIL_KEY] if task else {}
                is_v2 = task is not None and (
                    runtime.get("execution_mode") == "v2"
                    or (runtime.get("execution_mode") is None and task.pipeline_version == "v2")
                )
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
                    # On-demand playback has no full-video coordinator/cache.
                    # Freeze its own Chunk scope without changing full-parse defaults.
                    config = default_parse_config.model_copy(update={
                        "models": default_parse_config.models.model_copy(update={
                            "asr": default_parse_config.models.asr.model_copy(update={"scope": "chunk"}),
                        }),
                    })
                    task = VideoParseTaskData(
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
                    task_store.insert_task(session, task)
                if is_v2:
                    task_store.save_task(session, task)
                session.flush()
                task = task_store.get_task(session, task.id)
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
            return content_ops.load_realtime_chunks(
                session,
                video_parse_task_id=video_parse_task_id,
                tenant_id=tenant_id,
                video_id=video_id,
                task_version=task_version,
            )

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
                _set_capacity_acquired(task, False)
            task.next_execute_time = finished_at
            task.update_time = datetime_to_unix_time_ms(finished_at)
            task_store.save_task(session, task)
            video = task_store.get_video(session, task.video_id)
            if video is not None:
                video.parse_status = task.status
                task_store.update_video(session, video.id, parse_status=video.parse_status)

    def get_chunk_evidence(
        self, video_chunk_id: str, *, tenant_id: str,
    ) -> VideoChunkEvidence:
        """Read media evidence through the latest complete V2 receipt."""

        chunk_id = canonical_uuid(video_chunk_id, "video_chunk_id")
        workspace = require_nonempty_text(tenant_id, "tenant_id")
        with self._session_factory() as session:
            identity = retrieval_ops.load_chunk_evidence_identity(
                session,
                chunk_id=chunk_id,
                workspace=workspace,
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
            return retrieval_ops.resolve_retrieval_scope(
                session,
                require_sparse=require_sparse,
                ordered_series_ids=ordered_series_ids,
                workspace=workspace,
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
        with self._session_factory() as session:
            sources = retrieval_ops.validate_event_coverage(
                session,
                scope=scope,
                index_name=index_name,
                contract_id=contract_id,
                requested=requested,
            )
        return tuple(sorted(sources))

    def event_index_status(self, video_parse_task_id: str) -> dict[str, Any]:
        """Report persisted Event coverage without changing task or index state."""
        task_id = canonical_uuid(video_parse_task_id, "video_parse_task_id")
        with self._session_factory() as session:
            return retrieval_ops.event_index_status(
                session,
                task_id=task_id,
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
            return retrieval_ops.hydrate_retrieval_candidates(
                session,
                include_context=include_context,
                chunk_ids=chunk_ids,
                ordered=ordered,
                task_ids=task_ids,
            )

    def load_task_input(self, video_parse_task_id: str) -> VideoParseTaskInputState:
        with self._session_factory() as session:
            row = task_store.get_task_input_context(session, video_parse_task_id)
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
            video = task_store.get_video(session, task.video_id)
            if video is None:
                raise VideoParseConflictError("VideoParseTask Video no longer exists")
            chunk_rows, chunk_tasks = content_ops.ensure_chunk_tasks(
                session, task, video, ordered_chunk_ids, prepared_at,
            )
            intervals = content_ops.ensure_interval_tasks(
                session, task, chunk_tasks, chunk_rows, prepared_at,
            )

            if _activate:
                task.status = VideoParseTaskStatus.PARSING.value
                task.last_error = None
            task.update_time = datetime_to_unix_time_ms(prepared_at)
            task.detail = _ensure_runtime(task.detail)
            video.parse_status = (
                VideoParseTaskStatus.PARSING.value if _activate else task.status
            )
            video.parse_progress = 0
            task_store.save_task(session, task)
            task_store.update_video(
                session, video.id, parse_status=video.parse_status, parse_progress=video.parse_progress,
            )
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
            return content_ops.ensure_video_chunks(
                session,
                lease=lease,
                normalized=normalized,
                prepared_at=prepared_at,
            )

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
            chunk_count = len(chunk_store.list_tasks(session, task.id))
            interval_count = len(interval_store.list_tasks(session, task.id))
            if chunk_count != task.chunk_count or interval_count != len(
                _interval_specs(task.chunk_count, _interval_size(task.parse_config))
            ):
                raise VideoParseConflictError("VideoParseTask preparation is incomplete")
            task.status = VideoParseTaskStatus.PARSING.value
            task.last_error = None
            task.update_time = datetime_to_unix_time_ms(activated_at)
            video = task_store.get_video(session, task.video_id)
            task_store.save_task(session, task)
            if video is not None:
                video.parse_status = task.status
                video.parse_progress = 0
                task_store.update_video(
                    session, video.id, parse_status=video.parse_status, parse_progress=video.parse_progress,
                )
            session.flush()
            return _task_state(task_store.get_task(session, task.id))

    def finish_video_asr_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> None:
        prepared_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            task = _locked_video_lease(session, lease, prepared_at)
            if (task.is_deleted or _latest_video_task_id(session, task.video_id) != task.id
                    or _video_failure_status(task) is not None):
                raise VideoParseLeaseLostError("Video ASR preparation is no longer current")
            if not _requires_video_asr(task) or task.status not in {"PREPARING", "PARSING"}:
                raise VideoParseConflictError("Video ASR preparation is not pending")
            detail = _ensure_runtime(task.detail)
            detail[RUNTIME_DETAIL_KEY]["asr_prepared_lease_version"] = lease.lease_version
            task.detail = detail
            task.update_time = datetime_to_unix_time_ms(prepared_at)
            task_store.save_task(session, task)

    def begin_voiceprint_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Freeze the task's eligible Understanding sources under the Video lease."""
        started_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.begin_voiceprint_preparation(
                session,
                lease=lease,
                started_at=started_at,
            )

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
            return voiceprint_ops.bind_voiceprint_encoder(
                session,
                lease=lease,
                bound_at=bound_at,
                identity=identity,
            )

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
            return voiceprint_ops.finish_voiceprint_scoring(
                session,
                lease=lease,
                check_active=check_active,
                descriptor=descriptor,
                finished_at=finished_at,
                payload=payload,
            )

    def load_voiceprint_scoring(
        self, lease: VideoParseLease | IntervalParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read the committed manifest; artifact I/O belongs to the service."""
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return voiceprint_ops.load_voiceprint_scoring(
                session,
                lease=lease,
                loaded_at=loaded_at,
            )

    def precheck_voiceprint_registration(
        self, lease: VideoParseLease, *, source_ref: str, speaker: str,
        start_ms: int, end_ms: int, model: str, encoder_revision: str,
        replace_source_ref: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        checked_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.precheck_voiceprint_registration(
                session,
                lease=lease,
                source_ref=source_ref,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
                model=model,
                encoder_revision=encoder_revision,
                replace_source_ref=replace_source_ref,
                checked_at=checked_at,
            )

    def register_voiceprint(
        self, lease: VideoParseLease, *, source_ref: str, speaker: str,
        start_ms: int, end_ms: int, selection_basis: str, confidence: float | None,
        model: str, encoder_revision: str, vector: Sequence[float], pcm_sha256: str,
        duration_ms: int, replace_source_ref: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        registered_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.register_voiceprint(
                session,
                lease=lease,
                source_ref=source_ref,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
                selection_basis=selection_basis,
                confidence=confidence,
                model=model,
                encoder_revision=encoder_revision,
                vector=vector,
                pcm_sha256=pcm_sha256,
                duration_ms=duration_ms,
                replace_source_ref=replace_source_ref,
                registered_at=registered_at,
            )

    def unregister_voiceprint(
        self, lease: VideoParseLease, *, source_ref: str, now: datetime | None = None,
    ) -> dict[str, Any]:
        removed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.unregister_voiceprint(
                session,
                lease=lease,
                source_ref=source_ref,
                removed_at=removed_at,
            )

    def get_voiceprint_speaker_list(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.get_voiceprint_speaker_list(
                session,
                lease=lease,
                loaded_at=loaded_at,
            )

    def finish_voiceprint_preparation(
        self, lease: VideoParseLease, *, skip_reasons: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        finished_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.finish_voiceprint_preparation(
                session,
                lease=lease,
                skip_reasons=skip_reasons,
                finished_at=finished_at,
            )

    def stop_voiceprint_preparation(
        self, lease: VideoParseLease, *, now: datetime | None = None,
    ) -> bool:
        """Normal cancellation returns the phase without charging a failure."""
        stopped_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return voiceprint_ops.stop_voiceprint_preparation(
                session,
                lease=lease,
                stopped_at=stopped_at,
            )

    def load_voiceprint_references(
        self, lease: IntervalParseLease, *, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return voiceprint_ops.load_voiceprint_references(
                session,
                lease=lease,
                loaded_at=loaded_at,
            )

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
            return content_ops.commit_chunk_result(
                session,
                lease=lease,
                detail=detail,
                committed_at=committed_at,
                payload=payload,
            )

    def load_chunk_execution(
        self,
        lease: ChunkParseLease,
        *,
        now: datetime | None = None,
    ) -> ChunkExecutionState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return content_ops.load_chunk_execution(
                session,
                lease=lease,
                loaded_at=loaded_at,
            )

    def load_interval_members(
        self,
        lease: IntervalParseLease,
        *,
        now: datetime | None = None,
    ) -> IntervalMemberSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return content_ops.load_interval_members(
                session,
                lease=lease,
                loaded_at=loaded_at,
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
            interval = content_ops.commit_interval_refinement(
                session,
                lease=lease,
                interval_content=interval_content,
                member_updates=member_updates,
                input_versions=input_versions,
                input_fingerprints=input_fingerprints,
                input_contents=input_contents,
                detail=detail,
                committed_at=committed_at,
            )
            locked_intervals = [interval if item.id == interval.id else item for item in locked_intervals]
            queued = _advance_ready_chunks(
                session, interval.video_task_id, interval.chunk_task_ids,
                committed_at, locked_intervals=locked_intervals,
            )
            summary_ops.mark_summary_ready(session, interval.video_task_id, now=committed_at)
            return tuple(queued)

    def bind_event_index_target(
        self, lease: ChunkParseLease, configured_target: Mapping[str, Any],
        *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Freeze one Event build target at the first leased INDEX execution."""

        target = _normalize_event_index_target(configured_target)
        bound_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return content_ops.bind_event_index_target(
                session,
                lease=lease,
                bound_at=bound_at,
                target=target,
            )

    def load_chunk_indexing_snapshot(
        self, lease: ChunkParseLease, *, event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ChunkIndexingSnapshot:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return content_ops.load_chunk_indexing_snapshot(
                session,
                lease=lease,
                event_target=event_target,
                loaded_at=loaded_at,
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
            return content_ops.complete_chunk_indexing(
                session,
                lease=lease,
                content_version=content_version,
                index_generation=index_generation,
                event_target=event_target,
                completed_at=completed_at,
            )

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
            return content_ops.save_index_embeddings(
                session,
                lease=lease,
                chunk_parse_task_id=chunk_parse_task_id,
                content_version=content_version,
                representations=representations,
                receipt=receipt,
                event_target=event_target,
                saved_at=saved_at,
            )

    def request_index_rebuild(
        self, video_parse_task_id: str, *, event_target: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Requeue released Chunks while preserving Parse and Refine results."""
        requested_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return content_ops.request_index_rebuild(
                session,
                video_parse_task_id=video_parse_task_id,
                event_target=event_target,
                requested_at=requested_at,
            )

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
            return _task_state(task_store.get_task(session, task.id))

    def record_execution_timing(
        self,
        lease: VideoParseLease | ChunkParseLease | IntervalParseLease,
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
        if not (isinstance(lease, (VideoParseLease, ChunkParseLease))
                or isinstance(lease, IntervalParseLease) and lease.phase == "REFINING"):
            raise ValueError("timing supports only Video ASR, Chunk Parse/Index and Interval Refine")

        sampled_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return execution_ops.record_execution_timing(
                session,
                lease=lease,
                started_at_ms=started_at_ms,
                elapsed_ms=elapsed_ms,
                finished=finished,
                outcome=outcome,
                sampled_at=sampled_at,
            )

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
        with self._session_factory.begin() as session:
            return execution_ops.reserve_model_call(
                session,
                lease=lease,
                call_id=call_id,
                expires_at=expires_at,
                now_ms=now_ms,
                reserved_at=reserved_at,
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
        with self._session_factory.begin() as session:
            return execution_ops.renew_model_call(
                session,
                lease=lease,
                call_id=call_id,
                lease_duration=lease_duration,
                renewed_at=renewed_at,
                renewed_at_ms=renewed_at_ms,
            )

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
            return execution_ops.release_model_call(
                session,
                video_parse_task_id=video_parse_task_id,
                call_id=call_id,
                worker_id=worker_id,
                released_at=released_at,
            )

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
            return prefix_ops.claim_next_prefix(
                session,
                series_id=series_id,
                worker_id=worker_id,
                claimed_at=claimed_at,
                expires_at=expires_at,
                frontier_task_id=frontier_task_id,
                now_ms=now_ms,
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
            series_ids = task_store.list_series_ids(session)
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
            return prefix_ops.heartbeat_prefix(
                session,
                lease=lease,
                lease_duration=lease_duration,
                heartbeat_at=heartbeat_at,
            )

    def load_prefix_content(
        self, lease: PrefixParseLease, *, now: datetime | None = None,
    ) -> ChunkParseContentState:
        loaded_at = now if now is not None else now_utc()
        with self._session_factory() as session:
            return prefix_ops.load_prefix_content(
                session,
                lease=lease,
                loaded_at=loaded_at,
            )

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
            return prefix_ops.commit_prefix(
                session,
                lease=lease,
                content_updates=content_updates,
                video_prefix_compact=video_prefix_compact,
                series_prefix_compact=series_prefix_compact,
                input_version=input_version,
                input_fingerprint=input_fingerprint,
                committed_at=committed_at,
            )

    def retry_prefix(
        self,
        lease: PrefixParseLease,
        *,
        error: str,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return prefix_ops.retry_prefix(
                session,
                lease=lease,
                error=error,
                failed_at=failed_at,
            )

    def release_prefix(
        self,
        lease: PrefixParseLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Release a cancelled Prefix execution without recording a failure."""

        released_at = now if now is not None else now_utc()
        with self._session_factory.begin() as session:
            return prefix_ops.release_prefix(
                session,
                lease=lease,
                released_at=released_at,
            )

    def mark_summary_ready(self, task_id: str, *, now: datetime | None = None) -> bool:
        with self._summary_transaction() as session:
            return summary_ops.mark_summary_ready(session, task_id, now=now or now_utc())

    @contextmanager
    def _summary_transaction(self) -> Iterator[Session]:
        with self._session_factory.begin() as session:
            # Set before acquiring a connection/starting the database transaction.
            # Summary scans precede parent locks; RR would otherwise retain an
            # older source snapshot even after the current parent is locked.
            if session.get_bind().dialect.name == "mysql":
                session.connection(execution_options={"isolation_level": "READ COMMITTED"})
            yield session

    def claim_next_summary(
        self, worker_id: str, *, lease_duration: timedelta,
        config_snapshot: dict | None = None, now: datetime | None = None,
    ) -> VideoSummarizeLease | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        worker_id = require_nonempty_text(worker_id, "worker_id")
        with self._summary_transaction() as session:
            return summary_ops.claim_next_summary(
                session, worker_id, lease_duration=lease_duration,
                config_snapshot=config_snapshot, now=now or now_utc(),
            )

    def load_summary_input(
        self, lease: VideoSummarizeLease, *, now: datetime | None = None,
    ) -> VideoSummarizeInput:
        with self._summary_transaction() as session:
            return summary_ops.load_summary_input(session, lease, now=now or now_utc())

    def heartbeat_summary(
        self, lease: VideoSummarizeLease, *, lease_duration: timedelta, now: datetime | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        with self._session_factory.begin() as session:
            return summary_ops.heartbeat_summary(session, lease, lease_duration=lease_duration, now=now or now_utc())

    def complete_summary(
        self, lease: VideoSummarizeLease, result: VideoSummaryResult, *,
        cover_uri: str | None, now: datetime | None = None,
    ) -> bool:
        with self._summary_transaction() as session:
            return summary_ops.complete_summary(
                session, LibraryRepository(self._session_factory, execution_mode="v2"),
                lease, result, cover_uri=cover_uri, now=now or now_utc(),
            )

    def retry_summary(
        self, lease: VideoSummarizeLease, *, error: str, retry_at: datetime,
        max_retries: int, now: datetime | None = None,
    ) -> bool:
        with self._session_factory.begin() as session:
            return summary_ops.retry_summary(
                session, lease, error=error, retry_at=retry_at, max_retries=max_retries, now=now or now_utc(),
            )

    def release_summary(self, lease: VideoSummarizeLease, *, now: datetime | None = None) -> bool:
        with self._session_factory.begin() as session:
            return summary_ops.release_summary(session, lease, now=now or now_utc())
