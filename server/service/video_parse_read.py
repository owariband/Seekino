"""Interpret persisted parse facts for retrieval, media evidence and exports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from agent.video_parse_v2.input import ParseConfig
from engine.parsev2.schemas import DialogueEvent, VideoParseResult
from engine.parsev2.validation import validate_result_time_range
from engine.rag.contracts import TranscriptCueView, VideoChunkEvidence, VideoChunkRetrievalIdentity
from engine.rag.indexing.projection import project_chunk_index
from engine.rag.retrieval.contracts import ChunkRetrievalFacts

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.job import ChunkTaskPhase, ChunkParseTaskStatus, VideoParseTaskStatus
from ..error.retrieval import SeriesNotReadyError, SeriesScopeNotFoundError, VideoChunkNotFoundError
from ..repository import video_parse_retrieval as read_store
from ..repository import video_parse_task as task_store
from ..model.dto.jobDTO import (
    ChunkParseContentData,
    ChunkParseTaskData,
    SeriesData,
    VideoChunkData,
    VideoData,
    VideoParseTaskData,
    VideoParseRetrievalChunkData,
    VideoParseRetrievalTaskData,
    VideoParseRetrievalScope,
    HydratedRetrievalCandidate,
    ParseExportChunkSnapshot,
)
from .video_parse_errors import VideoParseTaskNotFoundError, VideoParseConflictError
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_task import _video_failure_status, _latest_video_task_id
from .video_parse_content import _index_receipt, _require_event_publication, EVENT_INDEX_TARGET_DETAIL_KEY


def _require_matching_retrieval_identity(
    identity: VideoChunkRetrievalIdentity,
    *,
    video_chunk_id: str,
    child: ChunkParseTaskData,
    task: VideoParseTaskData,
    series: SeriesData,
    video: VideoData,
    content: ChunkParseContentData,
) -> None:
    mismatches: list[str] = []
    for name, actual, expected in (
        ("tenantId", identity.tenant_id, series.tenant_id),
        ("seriesId", identity.series_id, series.id),
        ("videoId", identity.video_id, video.id),
        ("parseTaskId", identity.parse_task_id, child.video_task_id),
        ("videoChunkId", video_chunk_id, child.chunk_id),
        ("contentVersion", identity.content_version, content.version),
        ("indexGeneration", identity.index_generation, task.parse_config["indexing"]["index_generation"]),
    ):
        if actual != expected:
            mismatches.append(name)
    if mismatches:
        raise VideoParseConflictError(
            "retrieval candidate disagrees with current MySQL fields: "
            + ", ".join(mismatches)
        )


def _chunk_retrieval_facts(
    chunk: VideoChunkData,
    content: ChunkParseContentData,
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
    series: SeriesData,
    video: VideoData,
    chunk: VideoChunkData,
    task: VideoParseTaskData,
    content: ChunkParseContentData,
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


def resolve_retrieval_scope(
    session,
    *,
    require_sparse: bool,
    ordered_series_ids: tuple[str, ...],
    workspace: str,
) -> VideoParseRetrievalScope:
    existing_series_ids = set(read_store.list_series_ids(
        session, series_ids=ordered_series_ids, workspace=workspace,
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

    videos = read_store.list_series_videos(session, series_ids=ordered_series_ids)
    if not videos:
        raise SeriesNotReadyError(
            "selected Series have no COMPLETE Video Parse V2 task"
        )

    video_ids = [video.id for video in videos]
    latest_by_video: dict[str, VideoParseRetrievalTaskData] = {}
    for task in read_store.list_video_tasks(session, video_ids=video_ids):
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
    rows_by_task: dict[str, list[VideoParseRetrievalChunkData]] = {}
    for row in read_store.list_indexed_chunk_rows(
        session, task_ids=task_ids, phase=ChunkTaskPhase.INDEX,
        status=ChunkParseTaskStatus.COMPLETE.value,
    ):
        rows_by_task.setdefault(row.video_task_id, []).append(row)

    shared_contract: tuple[int, str, str, int, float, int] | None = None
    selected_config: ParseConfig | None = None
    for video, task in selected:
        rows = rows_by_task.get(task.id, [])
        if (
            len(rows) != task.chunk_count
            or [chunk.sequence_no for chunk in rows]
            != list(range(task.chunk_count))
            or any(chunk.video_id != video.id for chunk in rows)
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
        if rows:
            if require_sparse and not config.indexing.sparse_enabled:
                raise VideoParseConflictError("selected V2 task has no published Sparse representation")
            contract = (
                task.parse_config["indexing"]["index_generation"],
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


def event_index_status(
    session,
    *,
    task_id: str,
) -> dict[str, Any]:
    parent = task_store.get_task(session, task_id)
    if parent is None or parent.is_deleted:
        raise VideoParseTaskNotFoundError(task_id)
    target = (parent.detail or {}).get(EVENT_INDEX_TARGET_DETAIL_KEY)
    rows = read_store.load_task_chunk_rows(session, task_id=task_id)
    chunks = []
    for child, content, chunk in rows:
        reason = None
        try:
            if (target is None or content is None or child.phase != ChunkTaskPhase.INDEX
                    or child.status != ChunkParseTaskStatus.COMPLETE.value
                    or child.indexed_content_version != content.version):
                raise VideoParseConflictError("Event source is not published")
            receipt = _index_receipt(content, parent.id, child.chunk_id)
            _require_event_publication(parent, child, content, receipt, target, chunk=chunk)
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


def hydrate_retrieval_candidates(
    session,
    *,
    include_context: bool,
    chunk_ids: set[str],
    ordered: tuple[tuple[str, VideoChunkRetrievalIdentity], ...],
    task_ids: set[str],
) -> tuple[HydratedRetrievalCandidate, ...]:
    rows = read_store.load_candidate_rows(
        session, task_ids=task_ids, chunk_ids=chunk_ids,
        task_status=VideoParseTaskStatus.COMPLETE.value,
        phase=ChunkTaskPhase.INDEX, status=ChunkParseTaskStatus.COMPLETE.value,
    )
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
        neighbors_by_sequence = {
            (row[3].id, row[2].sequence_no): row
            for row in read_store.load_neighbor_rows(
                session,
                task_ids=task_ids, task_status=VideoParseTaskStatus.COMPLETE.value,
                phase=ChunkTaskPhase.INDEX, status=ChunkParseTaskStatus.COMPLETE.value,
                neighbors=[(task.id, (chunk.sequence_no - 1, chunk.sequence_no + 1))
                           for _series, _video, chunk, task, _child, _content in rows],
            )
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
                    index_generation=task.parse_config["indexing"]["index_generation"],
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


def load_chunk_evidence_identity(
    session,
    *,
    chunk_id: str,
    workspace: str,
) -> VideoChunkRetrievalIdentity:
    row = read_store.load_chunk_evidence_video(session, chunk_id=chunk_id, workspace=workspace)
    if row is None:
        raise VideoChunkNotFoundError("videoChunkId does not exist in this workspace")
    series, video = row
    task_id = _latest_video_task_id(session, video.id)
    if task_id is None:
        raise VideoChunkNotFoundError("videoChunkId has no current V2 task")
    task = task_store.get_task(session, task_id)
    if task is None or task.is_deleted:
        raise VideoChunkNotFoundError("videoChunkId has no current V2 task")
    if task.status != VideoParseTaskStatus.COMPLETE.value:
        raise VideoParseConflictError("videoChunkId latest V2 task is not COMPLETE")
    content = read_store.load_indexed_chunk_content(
        session, task_id=task_id, chunk_id=chunk_id,
        phase=ChunkTaskPhase.INDEX, status=ChunkParseTaskStatus.COMPLETE.value,
    )
    if content is None:
        raise VideoParseConflictError("videoChunkId has no COMPLETE V2 content")
    identity = VideoChunkRetrievalIdentity(
        tenant_id=workspace,
        series_id=series.id,
        video_id=video.id,
        parse_task_id=task_id,
        content_version=content.version,
        index_generation=task.parse_config["indexing"]["index_generation"],
    )

    return identity


def validate_event_coverage(
    session,
    *,
    scope: VideoParseRetrievalScope,
    index_name: str,
    contract_id: str,
    requested: set[str] | None,
) -> list[tuple[str, str, int]]:
    sources = []
    tasks = {task.id: task for task in read_store.list_tasks(
        session, task_ids=tuple(scope.parse_tasks_by_video_id.values()),
    )}
    if len(tasks) != len(scope.parse_tasks_by_video_id):
        raise VideoParseConflictError("Event retrieval scope changed")
    for parent in tasks.values():
        if (parent.status != VideoParseTaskStatus.COMPLETE.value
                or _latest_video_task_id(session, parent.video_id) != parent.id):
            raise VideoParseConflictError("Event source is no longer a COMPLETE publication")
    rows = read_store.load_event_source_rows(session, task_ids=tuple(tasks))
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
        _require_event_publication(parent, child, content, receipt, target, chunk=chunk)
        sources.append((parent.id, child.chunk_id, content.version))
    if requested is not None and seen != requested:
        raise VideoParseConflictError("Event Chunk sub-scope is outside the authorized publication")
    if requested is None and any(counts[task_id] != task.chunk_count for task_id, task in tasks.items()):
        raise VideoParseConflictError("Event publication scope has missing Chunk content")
    if not sources:
        raise VideoParseConflictError("Event index has no covered Chunk sources")

    return sources


def export_snapshots(
    rows: Sequence[Any], selected_ids: Sequence[str], *, by_video: bool,
) -> tuple[ParseExportChunkSnapshot, ...]:
    grouped: dict[str, list] = {}
    for row in rows:
        task = row[0]
        grouped.setdefault(task.video_id if by_video else task.id, []).append(row)
    if missing := set(selected_ids) - grouped.keys():
        raise VideoParseTaskNotFoundError(
            "export source does not exist in this workspace: " + ", ".join(sorted(missing))
        )

    snapshots = []
    for source_id in selected_ids:
        source_rows = grouped[source_id]
        task, video, *_ = source_rows[0]
        runtime = (task.detail or {}).get(RUNTIME_DETAIL_KEY, {})
        if task.is_test or not isinstance(runtime, Mapping) or runtime.get("accepted") is not True:
            raise VideoParseConflictError(f"export task {task.id} is not a formal accepted parse")
        if (task.status not in {
            VideoParseTaskStatus.INDEXING.value,
            VideoParseTaskStatus.INDEXING_FAILED.value,
            VideoParseTaskStatus.COMPLETE.value,
        } or _video_failure_status(task) == VideoParseTaskStatus.PARSING_FAILED.value):
            raise VideoParseConflictError(f"export task {task.id} parsing is not complete")
        if task.parse_config.get("compact_enabled", False):
            prefix = runtime.get("prefix", {})
            cursor = prefix.get("next_chunk_sequence_no", 0) if isinstance(prefix, Mapping) else None
            if type(cursor) is not int or cursor < task.chunk_count:
                raise VideoParseConflictError(f"export task {task.id} prefix refinement is not complete")
        if (len(source_rows) != task.chunk_count
                or any(row[3] is None for row in source_rows)
                or [row[3].sequence_no for row in source_rows] != list(range(task.chunk_count))
                or any(row[3].video_id != video.id for row in source_rows)):
            raise VideoParseConflictError(f"export task {task.id} Chunk topology is incomplete")
        for _task, _video, child, chunk, stored in source_rows:
            if child.phase != ChunkTaskPhase.INDEX:
                raise VideoParseConflictError(f"export Chunk {chunk.id} refinement is not complete")
            if stored is None or not stored.content or type(stored.version) is not int or stored.version < 1:
                raise VideoParseConflictError(f"export Chunk {chunk.id} formal content is missing")
            try:
                content = validate_result_time_range(
                    VideoParseResult.model_validate(stored.content),
                    start_seconds=0, end_seconds=video.duration_ms,
                )
            except ValueError as exc:
                raise VideoParseConflictError(
                    f"export Chunk {chunk.id} content version {stored.version} is invalid: {exc}"
                ) from exc
            snapshots.append(ParseExportChunkSnapshot(
                video_id=video.id, parse_task_id=task.id, chunk_id=chunk.id,
                chunk_sequence_no=chunk.sequence_no, content_version=stored.version, content=content,
            ))
    return tuple(snapshots)
