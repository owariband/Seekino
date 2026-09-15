from __future__ import annotations

from ..schema import AgentReply, VideoChunkSearchHit, VideoChunkSearchResult
from .state import AgentModelInvocationError, LoopTurn, QueryDraft, RetrievalCall, RunInputs


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
        if not 0 <= evidence.start_ms < evidence.end_ms <= evidence.duration_ms:
            raise AgentModelInvocationError("Retrieved Evidence has an invalid video time range")
        if not (
            0 <= evidence.analysis_window_start_ms <= evidence.start_ms
            < evidence.end_ms <= evidence.analysis_window_end_ms <= evidence.duration_ms
        ):
            raise AgentModelInvocationError("Retrieved Evidence has an invalid analysis window range")
        for matched in hit.selection_reason.matched_evidence:
            if (matched.start_ms is None) != (matched.end_ms is None):
                raise AgentModelInvocationError("Matched Evidence has an incomplete video time range")
            if matched.start_ms is not None and not (
                matched.end_ms is not None
                and 0 <= matched.start_ms < matched.end_ms <= evidence.duration_ms
            ):
                raise AgentModelInvocationError("Matched Evidence has an invalid video time range")


def bind_draft(
    inputs: RunInputs,
    previous_turns: list[LoopTurn],
    calls: list[RetrievalCall],
    draft: QueryDraft,
    *,
    max_selected_hits: int,
) -> tuple[VideoChunkSearchResult, VideoChunkSearchResult, dict[str, int]]:
    # Later calls supersede the same chunk; every value still comes from a real call.
    by_id: dict[str, VideoChunkSearchHit] = {}
    sources: dict[str, int] = {}
    query_by_id: dict[str, str] = {}
    last_query = ""
    for number, turn_calls in [
        *((turn.number, turn.retrieval_calls) for turn in previous_turns),
        (len(previous_turns) + 1, calls),
    ]:
        for call in turn_calls:
            validate_result(call.result, inputs)
            last_query = call.query
            for hit in call.result.hits:
                video_chunk_id = hit.candidate.video_chunk_id
                by_id[video_chunk_id] = hit.model_copy(deep=True)
                sources[video_chunk_id] = number
                query_by_id[video_chunk_id] = call.query
    if not last_query:
        raise AgentModelInvocationError("First Query turn must retrieve real evidence")
    selected_ids = list(dict.fromkeys(draft.selected_video_chunk_ids))
    if len(selected_ids) > max_selected_hits:
        raise AgentModelInvocationError("Query draft exceeds the final selected-hit budget")
    if any(video_chunk_id not in by_id for video_chunk_id in selected_ids):
        raise AgentModelInvocationError("Query Agent selected a VideoChunk outside retrieved evidence")
    selected = [by_id[video_chunk_id].model_copy(deep=True) for video_chunk_id in selected_ids]
    # For a union of searches, per-chunk source turns and full calls retain provenance.
    # The public query is the actual last source query of the ordered selection.
    selected_query = query_by_id[selected_ids[-1]] if selected_ids else last_query
    available = VideoChunkSearchResult(
        query=last_query, series_ids=list(inputs["series_ids"]),
        agent_reply=AgentReply(message="实际检索证据。", result_count=len(by_id)),
        hits=list(by_id.values()),
    )
    bound = VideoChunkSearchResult(
        query=selected_query, series_ids=list(inputs["series_ids"]),
        agent_reply=AgentReply(message=draft.message, result_count=len(selected)), hits=selected,
    )
    return available, bound, {key: sources[key] for key in selected_ids}


def finalize_output(turn: LoopTurn, inputs: RunInputs) -> VideoChunkSearchResult:
    if turn.review.action != "complete" or not turn.rendered_text.strip():
        raise AgentModelInvocationError("Output requires approved evidence and nonempty Chat text")
    validate_result(turn.draft_output, inputs)
    return VideoChunkSearchResult(
        query=turn.draft_output.query,
        series_ids=list(turn.draft_output.series_ids),
        agent_reply=AgentReply(message=turn.rendered_text, result_count=len(turn.draft_output.hits)),
        hits=[hit.model_copy(deep=True) for hit in turn.draft_output.hits],
    )
