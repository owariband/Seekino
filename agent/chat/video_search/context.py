from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, Literal

from engine.rag.contracts import (
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)

if TYPE_CHECKING:
    from .state import RunInputs
    from .workspace import AgentWorkspace


_MAX_CHECKPOINT_RETRIEVAL_IDS = 200


_EVIDENCE_FIELDS = (
    "series_id",
    "video_id",
    "episode_order",
    "original_filename",
    "duration_seconds",
    "video_chunk_id",
    "sequence_no",
    "start_seconds",
    "end_seconds",
    "analysis_window_start_seconds",
    "analysis_window_end_seconds",
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


def build_workspace_checkpoint(
    workspace: AgentWorkspace,
    *,
    role: Literal["query", "evaluator", "chat"],
) -> dict[str, Any]:
    """Project bounded canonical state when the Harness installs a new window."""

    retrieval_ids = workspace.retrieval_item_ids()
    visible_retrieval_ids = retrieval_ids[-_MAX_CHECKPOINT_RETRIEVAL_IDS:]
    approved = role == "chat"
    draft_items = workspace.draft_items(approved=approved)
    checkpoint: dict[str, Any] = {
        "schema": "anivag.agent-workspace-checkpoint.v1",
        "role": role,
        "workspace_id": workspace.workspace_id,
        "series_ids": list(workspace.series_ids),
        "retrieval_catalog": {
            "item_count": len(retrieval_ids),
            "visible_video_chunk_ids": list(visible_retrieval_ids),
            "omitted_item_count": len(retrieval_ids) - len(visible_retrieval_ids),
        },
        "draft_catalog": [
            {
                "video_chunk_id": item.video_chunk_id,
                "start_seconds": item.start_seconds,
                "end_seconds": item.end_seconds,
            }
            for item in draft_items
        ],
        "recovery": (
            "Treat IDs and counts as navigation metadata only. Use the registered "
            "Catalog/Read Tools for exact Evidence or Draft content."
        ),
    }
    if role == "chat":
        blocks = workspace.render_blocks()
        checkpoint["render_draft"] = {
            "block_count": len(blocks),
            "blocks": [
                (
                    {
                        "type": "video",
                        "video_chunk_id": getattr(block, "video_chunk_id"),
                    }
                    if getattr(block, "type", None) == "video"
                    else {
                        "type": "markdown",
                        "character_count": len(getattr(block, "markdown", "")),
                    }
                )
                for block in blocks
            ],
        }
    return checkpoint


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


def project_search_result(
    result: VideoChunkSearchResult,
    *,
    previously_returned_video_chunk_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Project canonical evidence without replaying an earlier Tool payload."""
    previous_ids = set(previously_returned_video_chunk_ids)
    new_hits = []
    reused_hits = []
    for hit in result.hits:
        if hit.candidate.video_chunk_id in previous_ids:
            reused_hits.append({
                "candidate": hit.candidate.model_dump(mode="json", by_alias=True),
                "selectionReason": _project_selection_reason(
                    hit.selection_reason.model_dump(mode="json", by_alias=True),
                    include_matched=False,
                ),
            })
        else:
            new_hits.append(_project_hit(hit))

    projected = {
        "query": result.query,
        "seriesIds": list(result.series_ids),
        "resultCount": len(result.hits),
        "hits": new_hits,
        "searchedVideoCount": (
            len(result.searched_video_ids)
            if result.searched_video_ids is not None else None
        ),
        "partial": result.partial,
        "warnings": list(result.warnings),
    }
    if reused_hits:
        projected.update({
            "newResultCount": len(new_hits),
            "reusedResultCount": len(reused_hits),
            "reusedHits": reused_hits,
        })
    return projected


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
    evidence_ranges: dict[str, tuple[int, int]] = {}
    for hit in result.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        candidate = hit.get("candidate")
        reason = hit.get("selectionReason")
        evidence = hit.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            continue
        video_chunk_id = candidate.get("videoChunkId")
        start_seconds = evidence.get("startSeconds")
        end_seconds = evidence.get("endSeconds")
        if (
            isinstance(video_chunk_id, str)
            and isinstance(start_seconds, int)
            and not isinstance(start_seconds, bool)
            and isinstance(end_seconds, int)
            and not isinstance(end_seconds, bool)
            and 0 <= start_seconds < end_seconds
        ):
            evidence_ranges[video_chunk_id] = (start_seconds, end_seconds)
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
                    "startSeconds", "endSeconds", "windowSummary", "videoCompact",
                )
                if key in evidence
            },
        })
    reply = result.get("agentReply")
    return {
        "seriesIds": list(result.get("seriesIds") or []),
        "resultCount": reply.get("resultCount") if isinstance(reply, dict) else len(hits),
        "hits": hits,
        "videoRanges": _project_stored_video_ranges(result, evidence_ranges),
        "partial": bool(result.get("partial", False)),
        "warnings": list(result.get("warnings") or []),
    }


def _project_stored_video_ranges(
    result: dict,
    evidence_ranges: dict[str, tuple[int, int]],
) -> list[dict[str, int | str]]:
    raw_blocks = result.get("renderBlocks")
    if not isinstance(raw_blocks, list):
        return []
    ranges: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for block in raw_blocks:
        if not isinstance(block, dict) or block.get("type") != "video":
            continue
        video_chunk_id = block.get("videoChunkId")
        if (
            not isinstance(video_chunk_id, str)
            or video_chunk_id in seen
            or video_chunk_id not in evidence_ranges
        ):
            continue
        evidence_start_seconds, evidence_end_seconds = evidence_ranges[video_chunk_id]
        start_seconds = block.get("startSeconds")
        end_seconds = block.get("endSeconds")
        if not (
            isinstance(start_seconds, int)
            and not isinstance(start_seconds, bool)
            and isinstance(end_seconds, int)
            and not isinstance(end_seconds, bool)
            and evidence_start_seconds <= start_seconds < end_seconds <= evidence_end_seconds
        ):
            continue
        seen.add(video_chunk_id)
        ranges.append({
            "videoChunkId": video_chunk_id,
            "startSeconds": start_seconds,
            "endSeconds": end_seconds,
        })
    return ranges


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
    selected_range = reference.get("selectedRange")
    if isinstance(selected_range, dict):
        start_seconds = selected_range.get("startSeconds")
        end_seconds = selected_range.get("endSeconds")
        if (
            isinstance(start_seconds, int)
            and not isinstance(start_seconds, bool)
            and isinstance(end_seconds, int)
            and not isinstance(end_seconds, bool)
            and 0 <= start_seconds < end_seconds
        ):
            projected["selectedRange"] = {
                "startSeconds": start_seconds,
                "endSeconds": end_seconds,
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
                for key in ("kind", "startSeconds", "endSeconds")
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
