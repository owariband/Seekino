"""Chunk preparation, content submission and index publication rules.

The caller owns the transaction. Every database operation uses repositories
with the same Session; model, encoder and search-engine calls remain outside it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session

from agent.video_parse_v2.scheduler import chunk_index_ready
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    RefinementMergeConflictError, merge_refinement_content,
    parse_content_fingerprint, validate_result_time_range,
)

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.identifiers import derive_video_chunk_id
from ..model.dto.jobDTO import (
    ChunkParseContentState, ChunkExecutionState, ChunkParseLease,
    IndexableChunkState, ChunkIndexingSnapshot, VideoParseTaskState,
    VideoParseLease, VideoChunkState, IntervalMemberSnapshot, IntervalParseLease, RealtimeChunkState,
    ChunkParseTaskData,
    ChunkParseContentData,
    IntervalParseTaskData,
    IntervalParseContentData,
    VideoParseTaskData,
    VideoChunkData,
    VideoData,
)
from ..model.job import (
    ChunkTaskPhase, ChunkParseTaskStatus, IntervalParseTaskStatus,
    VideoParseTaskStatus, interval_full_ready_mask, interval_ready_mask_bit,
    validate_interval_parse_task_members,
)
from ..repository import video_parse_chunk as chunk_store
from ..repository import video_parse_interval as interval_store
from ..repository import video_parse_task as task_store
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_errors import (
    VideoParseConflictError, VideoParseSnapshotConflictError, VideoParseLeaseLostError,
)
from .video_parse_task import (
    _task_input_state, _task_state, _locked_video_lease, _locked_parent_task,
    _locked_parent_intervals, _submitted_intervals_by_member, _advance_ready_chunks,
    _video_failure_status, _aggregate_video_task, _ensure_runtime,
    _latest_video_task_id, _positive_int, _interval_size, _interval_specs,
    _set_capacity_acquired,
)


EVENT_INDEX_TARGET_DETAIL_KEY = "_event_index_target_v1"
INDEX_EMBEDDINGS_DETAIL_KEY = "_index_embeddings_v2"


def _video_chunk_state(chunk: VideoChunkData) -> VideoChunkState:
    return VideoChunkState(
        video_chunk_id=chunk.id,
        sequence_no=chunk.sequence_no,
        start_seconds=chunk.start_ms,
        end_seconds=chunk.end_ms,
        chunk_media_ref=chunk.chunk_media_ref,
        analysis_window_media_ref=chunk.analysis_window_media_ref,
    )


def _locked_chunk_lease(
    session: Session, lease: ChunkParseLease, now: datetime
) -> ChunkParseTaskData:
    parent = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if parent is not None and (parent.last_error or {}).get("code") == "PARSE_NODE_FAILED":
        raise VideoParseLeaseLostError("Video parsing stopped after a final node failure")
    task = chunk_store.get_task(session, lease.chunk_parse_task_id, for_update=True)
    if not _matching_chunk_lease(task, lease, now):
        raise VideoParseLeaseLostError("ChunkParseTask lease is no longer current")
    return task


def _matching_chunk_lease(
    task: ChunkParseTaskData | None,
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


def _content_state(
    task: ChunkParseTaskData,
    chunk: VideoChunkData,
    content: ChunkParseContentData,
) -> ChunkParseContentState:
    return ChunkParseContentState(
        chunk_parse_task_id=task.id,
        video_chunk_id=chunk.id,
        sequence_no=chunk.sequence_no,
        content=deepcopy(dict(content.content or {})),
        version=content.version,
        detail=deepcopy(dict(content.detail or {})),
    )


def _validate_chunk_boundaries(
    video: VideoData,
    parse_config: Mapping[str, Any],
    chunks: Sequence[VideoChunkData],
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


def _index_receipt(
    content: ChunkParseContentData,
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


def _require_event_index_target(parent: VideoParseTaskData, expected: Mapping[str, Any] | None) -> None:
    current = (parent.detail or {}).get(EVENT_INDEX_TARGET_DETAIL_KEY)
    if current is None and expected is None:
        return
    if expected is None or current != _normalize_event_index_target(expected):
        raise VideoParseConflictError("Event index target changed during execution")


def _require_event_publication(
    parent: VideoParseTaskData, child: ChunkParseTaskData,
    content: ChunkParseContentData, receipt: Mapping[str, Any], target: Mapping[str, Any],
    *, chunk: VideoChunkData | None,
) -> None:
    from engine.rag.indexing.events import (
        EventIndexingContract, build_event_index_receipt, plan_event_index,
    )

    _require_event_index_target(parent, target)
    contract = EventIndexingContract.model_validate(target["contract"])
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


def load_realtime_chunks(
    session: Session,
    *,
    video_parse_task_id: str,
    tenant_id: str,
    video_id: str,
    task_version: int,
) -> tuple[RealtimeChunkState, ...]:
    task = task_store.get_task(session, video_parse_task_id)
    video = task_store.get_video(session, task.video_id) if task is not None else None
    series = task_store.get_series(session, video.series_id) if video is not None else None
    if (task is None or task.video_id != video_id or task.version != task_version
            or task.is_deleted or series is None or series.tenant_id != tenant_id
            or _latest_video_task_id(session, video_id) != task.id):
        raise VideoParseConflictError("Realtime Parse task changed; retry with the latest task")
    parent_error = _video_failure_status(task)
    if task.status in {VideoParseTaskStatus.PARSING_FAILED.value, VideoParseTaskStatus.INDEXING_FAILED.value}:
        parent_error = task.status
    rows = chunk_store.list_task_chunk_content_rows(
        session, task.id, include_missing_content=True,
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


def ensure_video_chunks(
    session: Session,
    *,
    lease: VideoParseLease,
    normalized: tuple[tuple[int, int, int], ...],
    prepared_at: datetime,
) -> tuple[VideoChunkState, ...]:
    task = _locked_video_lease(session, lease, prepared_at)
    if task.status != VideoParseTaskStatus.PREPARING.value:
        raise VideoParseConflictError("Video Chunk preparation is already complete")
    video = task_store.get_video(session, task.video_id)
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
        for item in chunk_store.list_video_chunks(session, video_id=video.id, for_update=True)
    }
    if set(existing) - set(range(task.chunk_count)):
        raise VideoParseConflictError("Video has an incompatible fixed topology")
    chunks: list[VideoChunkData] = []
    for sequence_no, start_seconds, end_seconds in normalized:
        chunk = existing.get(sequence_no)
        if chunk is None:
            source_ref = video.proxy_uri or video.source_uri
            chunk = VideoChunkData(
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
            chunk_store.insert_video_chunk(session, chunk)
        elif (chunk.start_ms, chunk.end_ms) != (
            start_seconds, end_seconds
        ):
            raise VideoParseConflictError(
                "existing Video Chunk topology differs from the plan"
            )
        chunks.append(chunk)
    session.flush()
    return tuple(_video_chunk_state(item) for item in chunks)


def commit_chunk_result(
    session: Session,
    *,
    lease: ChunkParseLease,
    detail: Mapping[str, Any] | None,
    committed_at: datetime,
    payload: dict[str, Any],
) -> tuple[str, ...]:
    intervals = _locked_parent_intervals(
        session, lease.video_parse_task_id
    )
    task = _locked_chunk_lease(session, lease, committed_at)
    if lease.phase != ChunkTaskPhase.PARSE:
        raise VideoParseLeaseLostError("only Parse leases can commit understanding")
    chunk = chunk_store.get_video_chunk(session, task.chunk_id)
    if chunk is None:
        raise VideoParseConflictError("ChunkParseTask VideoChunk no longer exists")
    payload = validate_result_time_range(
        VideoParseResult.model_validate(payload),
        start_seconds=chunk.window_start_ms,
        end_seconds=chunk.window_end_ms,
    ).model_dump(mode="json")
    result = chunk_store.get_content(session, task.id, for_update=True)
    result_detail = deepcopy(dict(detail or {}))
    result_detail["scope"] = [chunk.sequence_no]
    result_detail["parse_committed_lease_version"] = lease.lease_version
    if result is None:
        result = ChunkParseContentData(
            id=str(uuid4()),
            chunk_task_id=task.id,
            content=payload,
            version=1,
            detail=result_detail,
            create_time=datetime_to_unix_time_ms(committed_at),
            update_time=datetime_to_unix_time_ms(committed_at),
        )
        chunk_store.insert_content(session, result)
    else:
        # A valid lease can only reach this branch after an explicitly
        # recovered incomplete commit. Never replace a COMPLETE result.
        result.content = payload
        result.detail = result_detail
        result.update_time = datetime_to_unix_time_ms(committed_at)
        chunk_store.save_content(session, result)

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
        interval_store.save_task(session, interval)
        touched.append(interval.id)

    task.phase = ChunkTaskPhase.REFINE
    task.status = ChunkParseTaskStatus.WAITING.value
    task.retry_count = 0
    task.finish_time = None
    task.lease_owner = None
    task.lease_expire_time = None
    task.last_error = None
    task.update_time = datetime_to_unix_time_ms(committed_at)
    chunk_store.save_task(session, task)
    session.flush()
    parent = task_store.get_task(session, task.video_task_id)
    runtime = _ensure_runtime(parent.detail)[RUNTIME_DETAIL_KEY]
    if runtime.get("realtime_only") and not runtime.get("accepted"):
        if not chunk_store.has_leased_task(session, parent.id) and parent.lease_owner is None:
            _set_capacity_acquired(parent, False)
            task_store.save_task(session, parent)
    return tuple(touched)


def load_chunk_execution(
    session: Session,
    *,
    lease: ChunkParseLease,
    loaded_at: datetime,
) -> ChunkExecutionState:
    task = chunk_store.get_task(session, lease.chunk_parse_task_id)
    if not _matching_chunk_lease(task, lease, loaded_at):
        raise VideoParseLeaseLostError(
            "ChunkParseTask lease is no longer current"
        )
    chunk = chunk_store.get_video_chunk(session, task.chunk_id)
    parent = task_store.get_task(session, task.video_task_id)
    video = task_store.get_video(session, parent.video_id) if parent is not None else None
    series = task_store.get_series(session, video.series_id) if video is not None else None
    if chunk is None or parent is None or video is None or series is None:
        raise VideoParseConflictError("Chunk execution identity is incomplete")
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


def bind_event_index_target(
    session: Session,
    *,
    lease: ChunkParseLease,
    bound_at: datetime,
    target: dict[str, Any],
) -> dict[str, Any]:
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
    task_store.save_task(session, parent)
    return deepcopy(target)


def load_chunk_indexing_snapshot(
    session: Session,
    *,
    lease: ChunkParseLease,
    event_target: Mapping[str, Any] | None,
    loaded_at: datetime,
) -> ChunkIndexingSnapshot:
    child = chunk_store.get_task(session, lease.chunk_parse_task_id)
    if lease.phase != ChunkTaskPhase.INDEX or not _matching_chunk_lease(child, lease, loaded_at):
        raise VideoParseLeaseLostError("Chunk Index lease is no longer current")
    parent = task_store.get_task(session, lease.video_parse_task_id)
    video = task_store.get_video(session, parent.video_id) if parent is not None else None
    series = task_store.get_series(session, video.series_id) if video is not None else None
    chunk = chunk_store.get_video_chunk(session, child.chunk_id)
    content = chunk_store.get_content(session, child.id)
    if parent is None or parent.is_deleted or video is None or series is None or chunk is None or content is None:
        raise VideoParseConflictError("Chunk Index identity or content is incomplete")
    if event_target is not None:
        _require_event_index_target(parent, event_target)
    covering = _submitted_intervals_by_member(session, parent.id).get(child.id, ())
    refined = interval_store.list_content_ids(session, [item.id for item in covering])
    if not chunk_index_ready(bool(content.content), [
        item.status == IntervalParseTaskStatus.COMPLETE.value and item.id in refined
        for item in covering
    ]):
        raise VideoParseConflictError("Chunk Index dependencies are incomplete")
    return ChunkIndexingSnapshot(
        video_parse_task_id=parent.id,
        target_index_generation=parent.parse_config["indexing"]["index_generation"],
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
    session: Session,
    *,
    lease: ChunkParseLease,
    content_version: int,
    index_generation: int,
    event_target: Mapping[str, Any] | None,
    completed_at: datetime,
) -> VideoParseTaskState:
    parent = _locked_parent_task(session, lease.video_parse_task_id)
    child = _locked_chunk_lease(session, lease, completed_at)
    content = chunk_store.get_content(session, child.id, for_update=True)
    if lease.phase != ChunkTaskPhase.INDEX or content is None or content.version != content_version:
        raise VideoParseConflictError("indexed Chunk content or phase changed")
    if parent.parse_config["indexing"]["index_generation"] != index_generation:
        raise VideoParseConflictError("indexed Chunk target generation changed")
    _require_event_index_target(parent, event_target)
    receipt = _index_receipt(content, parent.id, child.chunk_id)
    if receipt["index_generation"] != index_generation:
        raise VideoParseConflictError("indexed Chunk checkpoint generation changed")
    if event_target is not None:
        _require_event_publication(
            parent, child, content, receipt, event_target,
            chunk=chunk_store.get_video_chunk(session, child.chunk_id),
        )
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
    chunk_store.save_content(session, content)
    chunk_store.save_task(session, child)
    session.flush()
    # The queue can also finish this aggregation if the coordinator exits.
    parent = _aggregate_video_task(session, parent.id, completed_at)
    session.flush()
    return _task_state(task_store.get_task(session, parent.id))


def save_index_embeddings(
    session: Session,
    *,
    lease: ChunkParseLease,
    chunk_parse_task_id: str,
    content_version: int,
    representations: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    event_target: Mapping[str, Any] | None,
    saved_at: datetime,
) -> None:
    parent = _locked_parent_task(session, lease.video_parse_task_id)
    child = _locked_chunk_lease(session, lease, saved_at)
    _require_event_index_target(parent, event_target)
    if lease.phase != ChunkTaskPhase.INDEX or child.id != chunk_parse_task_id:
        raise VideoParseConflictError("embedding target is not the leased Index Chunk")
    content = chunk_store.get_content(session, chunk_parse_task_id, for_update=True)
    if content is None or content.version != content_version:
        raise VideoParseConflictError("embedding target content version changed")
    detail = deepcopy(dict(content.detail or {}))
    cached = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
    cached_representations: dict[str, Any] = {}
    selected_receipt: dict[str, Any] | None = None
    if receipt is not None:
        child = chunk_store.get_task(session, chunk_parse_task_id)
        if child is None or child.video_task_id != lease.video_parse_task_id:
            raise VideoParseConflictError("embedding target belongs to a different task")
        selected_receipt = _validate_index_receipt(
            receipt, lease.video_parse_task_id, child.chunk_id, content_version
        )
        if selected_receipt["index_generation"] != parent.parse_config["indexing"]["index_generation"]:
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
    chunk_store.save_content(session, content)


def request_index_rebuild(
    session: Session,
    *,
    video_parse_task_id: str,
    event_target: Mapping[str, Any] | None,
    requested_at: datetime,
) -> tuple[str, ...]:
    task = _locked_parent_task(session, video_parse_task_id)
    intervals = _locked_parent_intervals(session, task.id)
    chunks = chunk_store.list_tasks(session, task.id, for_update=True)
    if _latest_video_task_id(session, task.video_id) != task.id:
        raise VideoParseConflictError("only the latest VideoParseTask can rebuild its index")
    if event_target is not None:
        detail = deepcopy(dict(task.detail or {}))
        detail[EVENT_INDEX_TARGET_DETAIL_KEY] = _normalize_event_index_target(event_target)
        task.detail = detail
        task_store.save_task(session, task)
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
        task_store.save_task(session, task)
    task = _aggregate_video_task(session, task.id, requested_at)
    if reset:
        task.next_execute_time = requested_at
        task.last_error = None
        task_store.save_task(session, task)
    return reset


def ensure_chunk_tasks(
    session: Session, task: VideoParseTaskData, video: VideoData,
    ordered_chunk_ids: tuple[str, ...], prepared_at: datetime,
) -> tuple[list[VideoChunkData], list[ChunkParseTaskData]]:
    """Freeze ordered Chunk children after the caller locks the parent lease."""
    chunk_rows = chunk_store.list_video_chunks(session, chunk_ids=ordered_chunk_ids, for_update=True)
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

    existing_chunks = chunk_store.list_tasks(session, task.id, for_update=True)
    by_video_chunk = {item.chunk_id: item for item in existing_chunks}
    if len(by_video_chunk) != len(existing_chunks):
        raise VideoParseConflictError("duplicate ChunkParseTask topology")
    if set(by_video_chunk) - set(ordered_chunk_ids):
        raise VideoParseConflictError("existing ChunkParseTask topology is incompatible")

    chunk_tasks: list[ChunkParseTaskData] = []
    for chunk in chunk_rows:
        child = by_video_chunk.get(chunk.id)
        if child is None:
            child = ChunkParseTaskData(
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
            chunk_store.insert_task(session, child)
            session.flush()
        chunk_tasks.append(child)
    return chunk_rows, chunk_tasks


def _interval_for_lease(
    session: Session, lease: IntervalParseLease, now: datetime
) -> IntervalParseTaskData:
    interval = interval_store.get_task(session, lease.interval_parse_task_id)
    if not _matching_interval_lease(interval, lease, now):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def ensure_interval_tasks(
    session: Session, task: VideoParseTaskData, chunk_tasks: Sequence[ChunkParseTaskData],
    chunk_rows: Sequence[VideoChunkData], prepared_at: datetime,
) -> list[IntervalParseTaskData]:
    """Freeze Refine windows after Chunk topology in the same transaction."""
    interval_specs = _interval_specs(
        len(chunk_tasks), _interval_size(task.parse_config)
    )
    existing_intervals = interval_store.list_tasks(session, task.id, for_update=True)
    by_start = {item.start_sequence_no: item for item in existing_intervals}
    if len(by_start) != len(existing_intervals):
        raise VideoParseConflictError("duplicate IntervalParseTask topology")
    if set(by_start) - {start for start, _count in interval_specs}:
        raise VideoParseConflictError("existing IntervalParseTask topology is incompatible")

    intervals: list[IntervalParseTaskData] = []
    committed_understanding = {
        result.chunk_task_id for result in chunk_store.list_contents(session, [child.id for child in chunk_tasks])
        if result.content
    }
    for start, count in interval_specs:
        members = chunk_tasks[start:start + count]
        member_ids = [member.id for member in members]
        ready_mask = sum(
            interval_ready_mask_bit(index, count)
            for index, member in enumerate(members) if member.id in committed_understanding
        )
        interval = by_start.get(start)
        if interval is None:
            interval = IntervalParseTaskData(
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
            interval_store.insert_task(session, interval)
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
            interval_store.save_task(session, interval)
        intervals.append(interval)
    return intervals


def _locked_interval_lease(
    session: Session,
    lease: IntervalParseLease,
    now: datetime,
    expected_status: str,
) -> IntervalParseTaskData:
    parent = task_store.get_task(session, lease.video_parse_task_id, for_update=True)
    if parent is not None and (parent.last_error or {}).get("code") == "PARSE_NODE_FAILED":
        raise VideoParseLeaseLostError("Video parsing stopped after a final node failure")
    interval = interval_store.get_task(session, lease.interval_parse_task_id, for_update=True)
    if (
        not _matching_interval_lease(interval, lease, now)
        or interval.status != expected_status
    ):
        raise VideoParseLeaseLostError("IntervalParseTask lease is no longer current")
    return interval


def _matching_interval_lease(
    interval: IntervalParseTaskData | None,
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
    interval: IntervalParseTaskData,
    *,
    for_update: bool,
) -> list[tuple[ChunkParseTaskData, VideoChunkData, ChunkParseContentData]]:
    rows = chunk_store.list_task_chunk_content_rows(
        session, chunk_task_ids=interval.chunk_task_ids, for_update=for_update,
    )
    if (
        len(rows) != interval.chunk_count
        or tuple(row[0].id for row in rows) != tuple(interval.chunk_task_ids)
    ):
        raise VideoParseConflictError("Interval member content is incomplete")
    return rows


def load_interval_members(
    session: Session,
    *,
    lease: IntervalParseLease,
    loaded_at: datetime,
) -> IntervalMemberSnapshot:
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
    session: Session,
    *,
    lease: IntervalParseLease,
    interval_content: Mapping[str, Any],
    member_updates: Mapping[str, Mapping[str, Any]],
    input_versions: Mapping[str, int],
    input_fingerprints: Mapping[str, str],
    input_contents: Mapping[str, Mapping[str, Any]],
    detail: Mapping[str, Any] | None,
    committed_at: datetime,
) -> IntervalParseTaskData:
    """Persist members and result under the caller's parent/Interval locks.

    The caller advances ready Chunks before committing this same transaction.
    """
    interval = _locked_interval_lease(
        session, lease, committed_at, IntervalParseTaskStatus.REFINING.value
    )
    if interval_store.get_content(session, interval.id) is not None:
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
    parent = task_store.get_task(session, interval.video_task_id)
    video = task_store.get_video(session, parent.video_id) if parent is not None else None
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
        chunk_store.save_content(session, result)

    interval_store.insert_content(session, IntervalParseContentData(
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
    interval_store.save_task(session, interval)

    session.flush()

    return interval
