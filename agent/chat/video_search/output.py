from __future__ import annotations

from engine.rag.contracts import (
    AgentReply,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)

from ..common.errors import AgentModelInvocationError
from .state import (
    AgentFinalOutput, ChatRenderDraft, ChatRenderBlock, ChatVideoBlock, LoopTurn,
    MarkdownRenderBlock, QueryDraft,
    RetrievalCall, RetrievalTraceEntry, RunInputs, VideoRenderBlock,
)


def video_range_sources(
    evidence_by_id: dict[str, dict],
    video_chunk_id: str,
    start_seconds: object,
    end_seconds: object,
    *,
    source_video_chunk_ids: object = None,
    allow_adjacent: bool = False,
) -> list[str]:
    """Resolve a final range against unchanged canonical source snapshots."""
    if (
        not isinstance(start_seconds, int) or isinstance(start_seconds, bool)
        or not isinstance(end_seconds, int) or isinstance(end_seconds, bool)
        or not 0 <= start_seconds < end_seconds
        or video_chunk_id not in evidence_by_id
    ):
        raise ValueError("Invalid anchor or playback range")
    anchor = evidence_by_id[video_chunk_id]
    if allow_adjacent and source_video_chunk_ids is None:
        try:
            return video_range_sources(
                evidence_by_id, video_chunk_id, start_seconds, end_seconds,
                source_video_chunk_ids=[video_chunk_id],
            )
        except ValueError:
            for identifier in evidence_by_id:
                if identifier == video_chunk_id:
                    continue
                try:
                    return video_range_sources(
                        evidence_by_id, video_chunk_id, start_seconds, end_seconds,
                        source_video_chunk_ids=[video_chunk_id, identifier],
                    )
                except ValueError:
                    continue
            raise ValueError("No adjacent retrieved chunks continuously cover the playback range")
    sources = [video_chunk_id] if source_video_chunk_ids is None else source_video_chunk_ids
    if (
        not isinstance(sources, list) or not 1 <= len(sources) <= 2
        or any(not isinstance(identifier, str) or identifier not in evidence_by_id
               for identifier in sources)
        or len(set(sources)) != len(sources) or video_chunk_id not in sources
    ):
        raise ValueError("Invalid playback source IDs")
    source_ranges = []
    for identifier in sources:
        evidence = evidence_by_id[identifier]
        if not isinstance(evidence, dict) or (
            "videoChunkId" in evidence and evidence["videoChunkId"] != identifier
        ):
            raise ValueError("Canonical source ID does not match its evidence")
        start, end = evidence.get("startSeconds"), evidence.get("endSeconds")
        if (
            not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or not 0 <= start < end
        ):
            raise ValueError("Invalid canonical source range")
        source_ranges.append((start, end, identifier))
    source_ranges.sort()
    ordered = [identifier for _, _, identifier in source_ranges]
    intervals = [(start, end) for start, end, _ in source_ranges]
    if not anchor["startSeconds"] < end_seconds or not start_seconds < anchor["endSeconds"]:
        raise ValueError("Playback range does not intersect its anchor")
    if len(ordered) == 2:
        identity = tuple(anchor.get(key) for key in ("tenantId", "seriesId", "videoId"))
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("Playback sources lack a video identity")
        for (_, source_end), identifier in zip(intervals, ordered):
            evidence = evidence_by_id[identifier]
            if tuple(evidence.get(key) for key in ("tenantId", "seriesId", "videoId")) != identity:
                raise ValueError("Playback sources belong to different videos or scopes")
            duration = evidence.get("durationSeconds")
            if (not isinstance(duration, int) or isinstance(duration, bool)
                or not source_end <= duration
                or not end_seconds <= duration):
                raise ValueError("Playback range exceeds the source video")
        sequences = [evidence_by_id[identifier].get("sequenceNo") for identifier in ordered]
        if (any(not isinstance(value, int) or isinstance(value, bool) for value in sequences)
            or sequences[1] != sequences[0] + 1):
            raise ValueError("Playback sources are not adjacent chunks")
        if intervals[1][0] > intervals[0][1]:
            raise ValueError("Playback sources contain an uncovered gap")
    if not intervals[0][0] <= start_seconds < end_seconds <= max(end for _, end in intervals):
        raise ValueError("Playback range is not covered by its canonical sources")
    return ordered


def validate_result(result: VideoChunkSearchResult, inputs: RunInputs) -> None:
    scope = set(inputs["series_ids"])
    if set(result.series_ids) != scope:
        raise AgentModelInvocationError("Retrieval changed the fixed Series scope")
    if result.agent_reply.result_count != len(result.hits):
        raise AgentModelInvocationError("Retrieval result count does not match evidence")
    seen: set[str] = set()
    for hit in result.hits:
        evidence = hit.evidence
        if (hit.candidate.video_chunk_id != evidence.video_chunk_id
            or not evidence.video_chunk_id or not evidence.video_id):
            raise AgentModelInvocationError("Retrieved Candidate and Evidence IDs do not match")
        if evidence.video_chunk_id in seen:
            raise AgentModelInvocationError("Retrieval returned duplicate VideoChunk IDs")
        seen.add(evidence.video_chunk_id)
        if (evidence.tenant_id != inputs["tenant_id"]
            or evidence.series_id not in scope or evidence.series_id not in result.series_ids):
            raise AgentModelInvocationError("Retrieved Evidence is outside the fixed scope")
        if not 0 <= evidence.start_seconds < evidence.end_seconds <= evidence.duration_seconds:
            raise AgentModelInvocationError("Retrieved Evidence has an invalid video time range")
        if not (
            0 <= evidence.analysis_window_start_seconds <= evidence.start_seconds
            < evidence.end_seconds <= evidence.analysis_window_end_seconds <= evidence.duration_seconds
        ):
            raise AgentModelInvocationError("Retrieved Evidence has an invalid analysis window range")
        for matched in hit.selection_reason.matched_evidence:
            if (matched.start_seconds is None) != (matched.end_seconds is None):
                raise AgentModelInvocationError("Matched Evidence has an incomplete video time range")
            if matched.start_seconds is not None and not (
                matched.end_seconds is not None
                and 0 <= matched.start_seconds < matched.end_seconds <= evidence.duration_seconds
            ):
                raise AgentModelInvocationError("Matched Evidence has an invalid video time range")


def bind_draft(
    inputs: RunInputs,
    previous_turns: list[LoopTurn],
    calls: list[RetrievalCall],
    draft: QueryDraft,
) -> tuple[VideoChunkSearchResult, VideoChunkSearchResult, dict[str, int]]:
    # Later calls supersede the same chunk; every value still comes from a real call.
    by_id: dict[str, VideoChunkSearchHit] = {}
    sources: dict[str, int] = {}
    last_query = ""
    searched_video_ids: set[str] = set()
    scope_recorded = True
    warnings: list[str] = []
    partial = False
    for number, turn_calls in [
        *((turn.number, turn.retrieval_calls) for turn in previous_turns),
        (len(previous_turns) + 1, calls),
    ]:
        for call in turn_calls:
            if call.status == "error":
                continue
            validate_result(call.result, inputs)
            last_query = call.query
            scope_recorded = scope_recorded and call.result.searched_video_ids is not None
            searched_video_ids.update(call.result.searched_video_ids or [])
            partial = partial or call.result.partial
            warnings.extend(call.result.warnings)
            for hit in call.result.hits:
                video_chunk_id = hit.candidate.video_chunk_id
                by_id[video_chunk_id] = hit.model_copy(deep=True)
                sources[video_chunk_id] = number
    if not last_query:
        raise AgentModelInvocationError("First Query turn must retrieve real evidence")
    selected_ranges = []
    selected_ids: set[str] = set()
    for selected_range in draft.selected_video_ranges:
        if selected_range.video_chunk_id in selected_ids:
            raise AgentModelInvocationError(
                "Query Agent selected duplicate ranges for one VideoChunk"
            )
        selected_ids.add(selected_range.video_chunk_id)
        selected_ranges.append(selected_range)
    if any(item.video_chunk_id not in by_id for item in selected_ranges):
        raise AgentModelInvocationError("Query Agent selected a VideoChunk outside retrieved evidence")
    selected = [
        by_id[item.video_chunk_id].model_copy(deep=True)
        for item in selected_ranges
    ]
    # Per-chunk source turns and individual calls retain multi-query provenance.
    available = VideoChunkSearchResult(
        query=last_query, series_ids=list(inputs["series_ids"]),
        agent_reply=AgentReply(message="实际检索证据。", result_count=len(by_id)),
        hits=list(by_id.values()),
        searched_video_ids=sorted(searched_video_ids) if scope_recorded else None,
        partial=partial, warnings=list(dict.fromkeys(warnings)),
    )
    bound = VideoChunkSearchResult(
        query=last_query, series_ids=list(inputs["series_ids"]),
        agent_reply=AgentReply(message=draft.message, result_count=len(selected)), hits=selected,
        searched_video_ids=available.searched_video_ids,
        partial=available.partial, warnings=available.warnings,
    )
    return available, bound, {
        item.video_chunk_id: sources[item.video_chunk_id]
        for item in selected_ranges
    }


def build_chat_output(
    draft: VideoChunkSearchResult,
    query_draft: QueryDraft,
    rendered: ChatRenderDraft,
    retrieval_trace: list[RetrievalTraceEntry],
    *,
    video_sources: dict[str, list[str]] | None = None,
) -> AgentFinalOutput:
    return AgentFinalOutput.model_validate(build_chat_snapshot(
        draft, query_draft, rendered.blocks, retrieval_trace, video_sources=video_sources,
    ))


def build_chat_snapshot(
    draft: VideoChunkSearchResult,
    query_draft: QueryDraft,
    blocks: list[ChatRenderBlock],
    retrieval_trace: list[RetrievalTraceEntry],
    *,
    video_sources: dict[str, list[str]] | None = None,
) -> dict:
    """Project accepted render blocks for both prefixes and completed answers."""
    validate_approved_video_ranges(draft, query_draft, video_sources=video_sources)
    video_items = [block for block in blocks if isinstance(block, ChatVideoBlock)]
    approved_hits = {
        hit.candidate.video_chunk_id: hit
        for hit in draft.hits
    }
    approved_ranges = {
        item.video_chunk_id: item
        for item in query_draft.selected_video_ranges
    }
    if any(
        item.video_chunk_id not in approved_ranges
        for item in video_items
    ):
        raise AgentModelInvocationError(
            "Chat video items must come from approved evidence"
        )
    hits: list[VideoChunkSearchHit] = []
    for item in video_items:
        approved = approved_hits[item.video_chunk_id]
        hits.append(
            approved.model_copy(
                update={
                    "selection_reason": approved.selection_reason.model_copy(
                        update={"summary": item.claim},
                    ),
                },
                deep=True,
            )
        )
    if video_sources is not None:
        included = {hit.candidate.video_chunk_id for hit in hits}
        for item in video_items:
            for identifier in video_sources[item.video_chunk_id]:
                if identifier not in included:
                    hits.append(approved_hits[identifier].model_copy(deep=True))
                    included.add(identifier)
    answer_markdown = "\n\n".join(
        block.markdown for block in blocks if isinstance(block, MarkdownRenderBlock)
    )
    return {
        "seriesIds": list(draft.series_ids),
        # The legacy reply field is not exposed by the message API. A card-only
        # prefix retains the real retrieval reply there, without inventing text.
        "agentReply": AgentReply(
            message=answer_markdown or draft.agent_reply.message,
            result_count=len(hits),
        ).model_dump(mode="json", by_alias=True),
        "hits": [hit.model_dump(mode="json", by_alias=True) for hit in hits],
        "searchedVideoIds": draft.searched_video_ids,
        "partial": draft.partial, "warnings": list(draft.warnings),
        "renderBlocks": [
            block.model_dump(mode="json", by_alias=True)
            if not isinstance(block, ChatVideoBlock)
            else VideoRenderBlock(
                video_chunk_id=block.video_chunk_id,
                start_seconds=approved_ranges[block.video_chunk_id].start_seconds,
                end_seconds=approved_ranges[block.video_chunk_id].end_seconds,
                source_video_chunk_ids=(
                    video_sources[block.video_chunk_id]
                    if video_sources is not None and len(video_sources[block.video_chunk_id]) > 1
                    else None
                ),
            ).model_dump(mode="json", by_alias=True)
            for block in blocks
        ],
        "retrievalTrace": [entry.model_dump(mode="json", by_alias=True) for entry in retrieval_trace],
    }


def validate_approved_video_ranges(
    draft: VideoChunkSearchResult,
    query_draft: QueryDraft,
    *,
    video_sources: dict[str, list[str]] | None = None,
) -> None:
    approved_hits = {
        hit.candidate.video_chunk_id: hit
        for hit in draft.hits
    }
    approved_ranges = {
        item.video_chunk_id: item
        for item in query_draft.selected_video_ranges
    }
    if (
        len(approved_ranges) != len(query_draft.selected_video_ranges)
        or len(approved_hits) != len(draft.hits)
        or (video_sources is not None and set(video_sources) != set(approved_ranges))
        or set(approved_hits) != (
            set(approved_ranges) if video_sources is None
            else {identifier for sources in video_sources.values() for identifier in sources}
        )
    ):
        raise AgentModelInvocationError(
            "Approved video ranges do not match approved evidence"
        )
    evidence_by_id = {
        identifier: hit.evidence.model_dump(by_alias=True)
        for identifier, hit in approved_hits.items()
    }
    for video_chunk_id, approved in approved_ranges.items():
        evidence = approved_hits[video_chunk_id].evidence
        try:
            video_range_sources(
                evidence_by_id, video_chunk_id, approved.start_seconds, approved.end_seconds,
                source_video_chunk_ids=video_sources[video_chunk_id] if video_sources is not None else None,
            )
        except ValueError as exc:
            raise AgentModelInvocationError(
                "Approved range for VideoChunk "
                f"{video_chunk_id!r} [{approved.start_seconds}, {approved.end_seconds}) "
                "must be contained in canonical evidence range "
                f"[{evidence.start_seconds}, {evidence.end_seconds})"
                if video_sources is None else f"Invalid final video range: {exc}"
            ) from exc


def build_retrieval_trace(turns: list[LoopTurn]) -> list[RetrievalTraceEntry]:
    return [
        RetrievalTraceEntry(
            query=call.query,
            top_k=call.top_k,
            strategy_version=call.strategy_version,
            requested_weights=call.requested_weights.model_copy(deep=True),
            effective_weights=call.effective_weights.model_copy(deep=True),
            result_count=len(call.result.hits),
            video_chunk_ids=[
                hit.candidate.video_chunk_id for hit in call.result.hits
            ],
            granularity=call.granularity,
            effective_filters=call.effective_filters,
            event_trace=call.event_trace,
            role=call.role,
            status=call.status,
            error=call.error,
        )
        for turn in turns
        for call in turn.retrieval_calls
    ]


def finalize_output(turns: list[LoopTurn], inputs: RunInputs) -> AgentFinalOutput:
    if not turns:
        raise AgentModelInvocationError("Output requires at least one Query turn")
    turn = turns[-1]
    if turn.review.action != "complete" or turn.rendered_answer is None:
        raise AgentModelInvocationError("Output requires approved evidence and nonempty Chat text")
    validate_result(turn.draft_output, inputs)
    return build_chat_output(
        turn.draft_output,
        turn.query_draft,
        turn.rendered_answer,
        build_retrieval_trace(turns),
    )
