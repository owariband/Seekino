from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..schema import VideoChunkEvidence, VideoChunkSearchHit, VideoChunkSearchResult

if TYPE_CHECKING:
    from .state import RunInputs


_EVIDENCE_FIELDS = (
    "series_id",
    "video_id",
    "episode_order",
    "original_filename",
    "duration_ms",
    "video_chunk_id",
    "sequence_no",
    "start_ms",
    "end_ms",
    "analysis_window_start_ms",
    "analysis_window_end_ms",
    "transcript",
    "baseline_visual_caption",
    "all_time_events",
    "video_compact",
    "context_before_compact",
    "context_after_compact",
    "window_summary",
    "is_fallback",
)
_EVIDENCE_ALIASES = tuple(
    field.alias or name
    for name, field in VideoChunkEvidence.model_fields.items()
    if name in _EVIDENCE_FIELDS
)


def project_inputs(
    inputs: RunInputs,
    *,
    include_reference_evidence: bool = True,
) -> dict[str, Any]:
    """Expose model-relevant input once, without replaying full stored search results."""
    return {
        "conversation_id": inputs["conversation_id"],
        "assistant_message_id": inputs["assistant_message_id"],
        "workspace_id": inputs["workspace_id"],
        "series_ids": list(inputs["series_ids"]),
        "series_context": list(inputs["series_context"]),
        "user_message": inputs["user_message"],
        "history": [_project_history_message(item) for item in inputs["history"]],
        "requested_top_k": inputs["requested_top_k"],
        "reference_context": _project_reference(
            inputs.get("reference_context"), include_evidence=include_reference_evidence,
        ),
    }


def project_search_result(result: VideoChunkSearchResult) -> dict[str, Any]:
    """Project the canonical Server result into evidence needed by model roles."""
    return {
        "query": result.query,
        "seriesIds": list(result.series_ids),
        "resultCount": len(result.hits),
        "hits": [_project_hit(hit) for hit in result.hits],
        "searchedVideoCount": (
            len(result.searched_video_ids)
            if result.searched_video_ids is not None else None
        ),
        "partial": result.partial,
        "warnings": list(result.warnings),
    }


def _project_hit(hit: VideoChunkSearchHit) -> dict[str, Any]:
    return {
        "candidate": hit.candidate.model_dump(mode="json", by_alias=True),
        "selectionReason": hit.selection_reason.model_dump(mode="json", by_alias=True),
        "evidence": hit.evidence.model_dump(
            mode="json", by_alias=True, include=_EVIDENCE_FIELDS,
        ),
    }


def _project_history_message(message: dict) -> dict[str, Any]:
    projected = {
        key: message[key]
        for key in ("role", "content", "seriesIds", "messageId", "status", "reference")
        if key in message
    }
    result = message.get("searchResult")
    projected["searchResult"] = (
        _project_stored_search_result(result) if isinstance(result, dict) else None
    )
    return projected


def _project_stored_search_result(result: dict) -> dict[str, Any]:
    hits = []
    for hit in result.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        candidate = hit.get("candidate")
        reason = hit.get("selectionReason")
        evidence = hit.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            continue
        hits.append({
            "candidate": {
                "videoChunkId": candidate.get("videoChunkId"),
                "fusedRank": candidate.get("fusedRank"),
            },
            "selectionReason": _project_selection_reason(reason, include_matched=True),
            "evidence": {
                key: (
                    _truncate(evidence.get(key), 2_000)
                    if key in {"windowSummary", "videoCompact"}
                    else evidence.get(key)
                )
                for key in (
                    "seriesId", "videoId", "videoChunkId", "episodeOrder", "sequenceNo",
                    "startMs", "endMs", "windowSummary", "videoCompact",
                )
                if key in evidence
            },
        })
    reply = result.get("agentReply")
    return {
        "query": result.get("query"),
        "seriesIds": list(result.get("seriesIds") or []),
        "resultCount": reply.get("resultCount") if isinstance(reply, dict) else len(hits),
        "hits": hits,
        "partial": bool(result.get("partial", False)),
        "warnings": list(result.get("warnings") or []),
    }


def _project_reference(
    reference: dict | None,
    *,
    include_evidence: bool,
) -> dict[str, Any] | None:
    if reference is None:
        return None
    projected = {
        key: reference[key]
        for key in ("reference", "sourceStatus")
        if key in reference
    }
    reason = reference.get("selectionReason")
    if isinstance(reason, dict):
        projected["selectionReason"] = _project_selection_reason(
            reason, include_matched=include_evidence,
        )
    evidence = reference.get("evidence")
    if include_evidence and isinstance(evidence, dict):
        projected["evidence"] = {
            key: evidence[key] for key in _EVIDENCE_ALIASES if key in evidence
        }
    return projected


def _project_selection_reason(
    reason: Any,
    *,
    include_matched: bool,
) -> dict[str, Any]:
    if not isinstance(reason, dict):
        return {"summary": None}
    projected: dict[str, Any] = {"summary": _truncate(reason.get("summary"), 1_000)}
    if not include_matched:
        return projected
    projected["matchedEvidence"] = [
        {
            **{
                key: item.get(key)
                for key in ("kind", "startMs", "endMs")
                if key in item
            },
            "text": _truncate(item.get("text"), 1_000),
        }
        for item in (reason.get("matchedEvidence") or [])[:4]
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    projected["limitations"] = [
        _truncate(item, 500)
        for item in (reason.get("limitations") or [])[:4]
        if isinstance(item, str)
    ]
    return projected


def _truncate(value: Any, maximum: int) -> Any:
    if not isinstance(value, str) or len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"
