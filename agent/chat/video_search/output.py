from __future__ import annotations

from engine.retrieval.contracts import (
    AgentReply,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)

from ..common.errors import AgentModelInvocationError
from .state import (
    AgentFinalOutput, ChatRenderDraft, LoopTurn, MarkdownRenderBlock, QueryDraft,
    RetrievalCall, RetrievalTraceEntry, RunInputs, VideoRenderBlock,
)


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
        if (evidence.workspace_id != inputs["workspace_id"]
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
    query_by_id: dict[str, str] = {}
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
                query_by_id[video_chunk_id] = call.query
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
    # For a union of searches, per-chunk source turns and full calls retain provenance.
    # The public query is the actual last source query of the ordered selection.
    selected_query = (
        query_by_id[selected_ranges[-1].video_chunk_id]
        if selected_ranges else last_query
    )
    available = VideoChunkSearchResult(
        query=last_query, series_ids=list(inputs["series_ids"]),
        agent_reply=AgentReply(message="实际检索证据。", result_count=len(by_id)),
        hits=list(by_id.values()),
        searched_video_ids=sorted(searched_video_ids) if scope_recorded else None,
        partial=partial, warnings=list(dict.fromkeys(warnings)),
    )
    bound = VideoChunkSearchResult(
        query=selected_query, series_ids=list(inputs["series_ids"]),
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
) -> AgentFinalOutput:
    validate_approved_video_ranges(draft, query_draft)
    approved_hits = {
        hit.candidate.video_chunk_id: hit
        for hit in draft.hits
    }
    approved_ranges = {
        item.video_chunk_id: item
        for item in query_draft.selected_video_ranges
    }
    if any(
        item.video_chunk_id not in approved_hits
        for item in rendered.video_items
    ):
        raise AgentModelInvocationError(
            "Chat video items must come from approved evidence"
        )
    hits: list[VideoChunkSearchHit] = []
    for item in rendered.video_items:
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
    return AgentFinalOutput(
        query=draft.query,
        series_ids=list(draft.series_ids),
        agent_reply=AgentReply(
            message=rendered.answer_markdown,
            result_count=len(hits),
        ),
        hits=hits,
        searched_video_ids=draft.searched_video_ids,
        partial=draft.partial, warnings=list(draft.warnings),
        render_blocks=[
            block.model_copy(deep=True)
            if isinstance(block, MarkdownRenderBlock)
            else VideoRenderBlock(
                video_chunk_id=block.video_chunk_id,
                start_seconds=approved_ranges[block.video_chunk_id].start_seconds,
                end_seconds=approved_ranges[block.video_chunk_id].end_seconds,
            )
            for block in rendered.blocks
        ],
        retrieval_trace=[entry.model_copy(deep=True) for entry in retrieval_trace],
    )


def validate_approved_video_ranges(
    draft: VideoChunkSearchResult,
    query_draft: QueryDraft,
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
        or set(approved_ranges) != set(approved_hits)
    ):
        raise AgentModelInvocationError(
            "Approved video ranges do not match approved evidence"
        )
    for video_chunk_id, approved in approved_ranges.items():
        evidence = approved_hits[video_chunk_id].evidence
        if not (
            evidence.start_seconds
            <= approved.start_seconds
            < approved.end_seconds
            <= evidence.end_seconds
        ):
            raise AgentModelInvocationError(
                "Approved range for VideoChunk "
                f"{video_chunk_id!r} [{approved.start_seconds}, {approved.end_seconds}) "
                "must be contained in canonical evidence range "
                f"[{evidence.start_seconds}, {evidence.end_seconds})"
            )


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
