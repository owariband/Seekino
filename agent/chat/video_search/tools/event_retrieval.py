from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any, Literal

from pydantic import Field, model_validator

from engine.rag.contracts import AgentReply, VideoChunkSearchResult
from engine.rag.retrieval.contracts import (
    EventFilters, EventIndexNotReadyError, EventSearchResult, EventType,
)
from engine.rag.retrieval.fusion import (
    normalize_hybrid_retrieval_weights,
)
from engine.rag.retrieval.events import EVENT_RETRIEVAL_STRATEGY_VERSION

from ...common.errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolInvocationError, AgentToolOutputError,
)
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import cursor_position, page_items
from ...common.tool_result import TextToolResult, ToolResult
from ..output import validate_result
from ..state import LexicalRetrievalChannelWeights, RetrievalCall
from ..video_search_canvas import VideoSearchContextCanvas
from .retrieval import LexicalRetrieveArguments
from .shared import _ToolCallBudget, _bounded_text, _start_tool_call


_TOOL_NAME = "search_video_events"


class SearchVideoEventsArguments(LexicalRetrieveArguments):
    event_types: list[EventType] | None = None
    speaker: str | None = Field(default=None, min_length=1)
    video_chunk_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_filters(self) -> SearchVideoEventsArguments:
        if self.cursor is not None:
            if any(value is not None for value in (self.event_types, self.speaker, self.video_chunk_ids)):
                raise ValueError("cursor continuation must not repeat Event filters")
        else:
            EventFilters(event_types=tuple(self.event_types or ()), speaker=self.speaker,
                         video_chunk_ids=tuple(self.video_chunk_ids or ()))
        return self


def build_search_video_events_tool(
    search: Callable[..., EventSearchResult],
    *,
    canvas: VideoSearchContextCanvas,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    role: Literal["query", "supervisor"],
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    catalogs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    invocation_lock = Lock()

    def search_video_events(
        query: str | None = None,
        top_k: int | None = None,
        channel_weights: LexicalRetrievalChannelWeights | None = None,
        event_types: list[EventType] | None = None,
        speaker: str | None = None,
        video_chunk_ids: list[str] | None = None,
        cursor: str | None = None,
    ) -> ToolResult:
        with invocation_lock:
            _start_tool_call(control, budget, _TOOL_NAME)
            if cursor is not None:
                try:
                    position = cursor_position(cursor, namespace=_TOOL_NAME)
                    catalog = catalogs.get(position.resource_id)
                    if catalog is None:
                        raise AgentToolInvocationError("Unknown or expired Event cursor")
                    items, metadata = catalog
                    return TextToolResult(success=True, content=page_items(
                        items, namespace=_TOOL_NAME, resource_id=position.resource_id,
                        cursor=cursor, maximum_items=maximum_catalog_items,
                        maximum_bytes=maximum_result_bytes,
                        metadata={**metadata, "draft_item_count": canvas.draft_item_count()},
                    ))
                except AgentToolOutputError:
                    raise
                except AgentToolInvocationError as exc:
                    raise AgentToolFeedbackError(
                        "INVALID_CATALOG_CURSOR", str(exc),
                        "Call search_video_events again with query, top_k and channel_weights.",
                    ) from exc
            if query is None or top_k is None or channel_weights is None:
                raise AgentToolInvocationError("Event search requires query, top_k and channel_weights")
            filters = EventFilters(event_types=tuple(event_types or ()), speaker=speaker,
                                   video_chunk_ids=tuple(video_chunk_ids or ()))
            requested = LexicalRetrievalChannelWeights.model_validate(channel_weights)
            lexical, semantic, visual = normalize_hybrid_retrieval_weights(
                requested.lexical, requested.semantic, requested.visual,
            )
            effective = LexicalRetrievalChannelWeights(lexical=lexical, semantic=semantic, visual=visual)
            try:
                found = search(
                    query, tenant_id=inputs["tenant_id"], series_ids=list(inputs["series_ids"]),
                    top_k=top_k, lexical_weight=lexical, text_weight=semantic, vision_weight=visual,
                    **filters.model_dump(mode="json"),
                )
            except AgentModelInvocationError:
                raise
            except Exception as exc:
                control.check()
                code = "EVENT_INDEX_NOT_READY" if isinstance(exc, EventIndexNotReadyError) else "EVENT_RETRIEVAL_FAILED"
                calls.append(RetrievalCall(
                    query=query, top_k=top_k, requested_weights=requested.model_copy(deep=True),
                    effective_weights=effective, granularity="event", role=role, status="error",
                    strategy_version=EVENT_RETRIEVAL_STRATEGY_VERSION,
                    effective_filters=filters.model_dump(mode="json"),
                    error={"code": code, "type": type(exc).__name__, "message": str(exc)},
                    result=VideoChunkSearchResult(
                        query=query, series_ids=list(inputs["series_ids"]), hits=[], partial=True,
                        agent_reply=AgentReply(message="Event retrieval did not complete", result_count=0),
                    ),
                ))
                raise AgentToolFeedbackError(
                    code, "Event retrieval did not complete for the requested scope.",
                    "Continue with retrieve or retry Event search. Missing coverage is not evidence of no matching scenes.",
                ) from exc
            control.check()
            found = EventSearchResult.model_validate(found).model_copy(deep=True)
            strategy_version = found.trace.get("strategy_version", EVENT_RETRIEVAL_STRATEGY_VERSION)
            if strategy_version != EVENT_RETRIEVAL_STRATEGY_VERSION:
                raise AgentModelInvocationError("Event retrieval returned an incompatible strategy version")
            result = found.chunk_result
            validate_result(result, inputs)
            if result.query != query or len(result.hits) > top_k:
                raise AgentModelInvocationError("Event retrieval result does not match query or top_k")
            hits = {hit.candidate.video_chunk_id: hit for hit in result.hits}
            if filters.video_chunk_ids and not set(hits).issubset(filters.video_chunk_ids):
                raise AgentModelInvocationError("Event retrieval widened the requested Chunk scope")
            if {match.video_chunk_id for match in found.matches} != set(hits):
                raise AgentModelInvocationError("Event matches do not match hydrated Chunk evidence")
            items = []
            for match in found.matches:
                evidence = hits[match.video_chunk_id].evidence
                if ((filters.event_types and match.event_type not in filters.event_types)
                    or (filters.speaker is not None and match.speaker != filters.speaker)
                    or match.start_ms is None or match.end_ms is None
                    or not 0 <= match.start_ms < match.end_ms <= evidence.duration_seconds
                    or not match.start_ms < evidence.end_seconds
                    or not evidence.start_seconds < match.end_ms):
                    raise AgentModelInvocationError("Event match has invalid filters or canonical times")
                items.append({
                    "video_chunk_id": match.video_chunk_id,
                    "episode_order": evidence.episode_order,
                    "start_seconds": evidence.start_seconds,
                    "end_seconds": evidence.end_seconds,
                    "matched_event": {
                        **match.model_dump(mode="json", exclude={"parse_task_id", "version", "video_chunk_id", "lexical_fields", "matched_fields"}),
                        **{name: [
                            {"field": field.field, "language": field.language, "role": field.role,
                             "text_excerpt": _bounded_text(field.raw_text, 160)}
                            for field in getattr(match, name)
                        ] for name in ("lexical_fields", "matched_fields")},
                    },
                })
            event_trace = {
                **found.trace,
                "matched_events": [match.model_dump(mode="json") for match in found.matches],
            }
            canvas.record_retrieval(result)
            calls.append(RetrievalCall(
                query=query, top_k=top_k, requested_weights=requested.model_copy(deep=True),
                effective_weights=effective, result=result, granularity="event",
                effective_filters=filters.model_dump(mode="json"), event_trace=event_trace, role=role,
                status="success",
                strategy_version=strategy_version,
            ))
            resource_id = f"event-retrieval-{len(catalogs) + 1}"
            metadata = {
                "query": query, "granularity": "event", "requested_top_k": top_k,
                "result_count": len(result.hits), "event_count": len(found.matches),
                "draft_item_count": canvas.draft_item_count(), "partial": result.partial,
                "warnings": [_bounded_text(value, 500) for value in result.warnings[:8]],
                "effective_filters": filters.model_dump(mode="json"),
                "retrieval_strategy": {
                    "version": strategy_version,
                    "requested_weights": requested.model_dump(mode="json"),
                    "effective_weights": effective.model_dump(mode="json"),
                },
            }
            catalogs[resource_id] = (items, metadata)
            return TextToolResult(success=True, content=page_items(
                items, namespace=_TOOL_NAME, resource_id=resource_id, cursor=None,
                maximum_items=maximum_catalog_items, maximum_bytes=maximum_result_bytes, metadata=metadata,
            ))

    return build_agent_tool(
        func=search_video_events, name=_TOOL_NAME,
        description=(
            "Search individual Timeline Events directly or within a smaller Chunk scope. "
            "top_k counts distinct Chunk groups; each paginated card describes one matched Event. "
            "speaker selects only reliable DIALOGUE speakers, not visible people. "
            "Use lexical/semantic/visual weights. Continue with cursor only. "
            "Read canonical Evidence through get_context_canvas(collection='retrieval') before making claims. "
            "Visual similarity still requires inspect_video_frames verification."
        ),
        args_schema=SearchVideoEventsArguments, maximum_result_bytes=maximum_result_bytes,
        draft_item_count=canvas.draft_item_count,
    )
