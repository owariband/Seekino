from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from functools import wraps
from hashlib import sha256
import json
import logging
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Annotated, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.retrieval.video_retrieval import normalize_hybrid_retrieval_weights

from ..constants.retrieval import AGENT_RETRIEVAL_STRATEGY_VERSION
from ..schema import VideoChunkSearchHit, VideoChunkSearchResult
from .errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolInvocationError,
    AgentToolOutputError, InvalidDraftRangeError, UnknownToolItemError,
)
from .output import validate_result
from .state import (
    ChatVideoBlock, DraftItem, ExecutionControl, RetrievalCall,
    RetrievalChannelWeights, RunInputs,
)
from .tool_output import bounded_json, cursor_position, page_items, range_text
from .workspace import AgentWorkspace


_logger = logging.getLogger(__name__)


class AgentToolTraceHandler(BaseCallbackHandler):
    """Trace actual Tool calls without logging user text or full Evidence."""

    def __init__(self, role: str, inputs: RunInputs) -> None:
        self.role = role
        self.conversation_id = inputs["conversation_id"]
        self.message_id = inputs["assistant_message_id"]
        self._active: dict[UUID, tuple[str, float]] = {}
        self._lock = Lock()

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = serialized.get("name", "unknown") if serialized else "unknown"
        with self._lock:
            self._active[run_id] = (name, monotonic())
        _logger.info(
            "event=agent_tool_started role=%s conversation_id=%s message_id=%s "
            "tool=%s tool_run_id=%s arguments_summary=%s",
            self.role, self.conversation_id, self.message_id, name, run_id,
            _trace_json(_trace_tool_arguments(inputs)),
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        name, duration_ms = self._finish(run_id)
        status, result = _trace_tool_result(output)
        _logger.info(
            "event=agent_tool_finished role=%s conversation_id=%s message_id=%s "
            "tool=%s tool_run_id=%s status=%s duration_ms=%s result_summary=%s",
            self.role, self.conversation_id, self.message_id, name, run_id,
            status, duration_ms, _trace_json(result),
        )

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any,
    ) -> None:
        name, duration_ms = self._finish(run_id)
        error_message = (
            str(error).replace("\n", " ")[:256]
            if isinstance(error, (AgentModelInvocationError, AgentToolInvocationError))
            else "-"
        )
        _logger.warning(
            "event=agent_tool_failed role=%s conversation_id=%s message_id=%s "
            "tool=%s tool_run_id=%s duration_ms=%s error_type=%s error_message=%s",
            self.role, self.conversation_id, self.message_id, name, run_id,
            duration_ms, type(error).__name__, error_message,
        )

    def _finish(self, run_id: UUID) -> tuple[str, int | None]:
        with self._lock:
            active = self._active.pop(run_id, None)
        if active is None:
            return "unknown", None
        name, started = active
        return name, round((monotonic() - started) * 1_000)


def attach_agent_tool_logging(
    tools: list[StructuredTool], *, role: str, inputs: RunInputs,
) -> None:
    handler = AgentToolTraceHandler(role, inputs)
    for tool in tools:
        tool.callbacks = [*(tool.callbacks or []), handler]


def _build_agent_tool(
    *,
    func: Callable[..., str],
    name: str,
    description: str,
    args_schema: type[BaseModel],
    maximum_result_bytes: int,
    draft_item_count: Callable[[], int] | None = None,
) -> StructuredTool:
    """Keep correctable Tool failures model-visible across both Agent runners."""

    def with_draft_count(payload: dict[str, Any]) -> dict[str, Any]:
        if draft_item_count is not None:
            payload["draft_item_count"] = draft_item_count()
        return payload

    @wraps(func)
    def invoke(*args: Any, **kwargs: Any) -> str:
        try:
            return func(*args, **kwargs)
        except AgentToolFeedbackError as exc:
            raise ToolException(bounded_json(with_draft_count({
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retry_hint": exc.retry_hint,
                    **(
                        {"suggested_end": exc.suggested_end}
                        if exc.suggested_end is not None else {}
                    ),
                },
            }), maximum_result_bytes)) from exc
        except UnknownToolItemError as exc:
            raise ToolException(bounded_json(with_draft_count({
                "ok": False,
                "error": {
                    "code": "UNKNOWN_ITEM",
                    "message": str(exc),
                    "retry_hint": exc.retry_hint,
                },
            }), maximum_result_bytes)) from exc

    def validation_feedback(error: Any) -> str:
        fields = [
            ".".join(map(str, item.get("loc", ()))) or "arguments"
            for item in error.errors(include_input=False, include_url=False)[:4]
        ]
        return bounded_json(with_draft_count({
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENTS",
                "message": f"Invalid arguments for {name}: {', '.join(fields)}.",
                "retry_hint": "Check the Tool schema and call it again with valid arguments.",
            },
        }), maximum_result_bytes)

    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=description,
        args_schema=args_schema,
        handle_tool_error=True,
        handle_validation_error=validation_feedback,
    )


def _trace_tool_arguments(inputs: dict[str, Any] | None) -> dict[str, Any]:
    if inputs is None:
        return {"input_type": "text"}
    summary: dict[str, Any] = {}
    video_chunk_id = inputs.get("video_chunk_id")
    if isinstance(video_chunk_id, str):
        if len(video_chunk_id) <= 64:
            summary["video_chunk_id"] = video_chunk_id
        else:
            summary["video_chunk_id_chars"] = len(video_chunk_id)
            summary["video_chunk_id_sha256"] = sha256(
                video_chunk_id.encode("utf-8", "replace")
            ).hexdigest()[:16]
    for key in ("start_ms", "end_ms", "top_k", "start", "end"):
        value = inputs.get(key)
        if type(value) is int:
            summary[key] = value
    section = inputs.get("section")
    if isinstance(section, str):
        summary["section"] = section[:64]
    for key in ("query", "claim"):
        value = inputs.get(key)
        if isinstance(value, str):
            summary[f"{key}_chars"] = len(value)
            summary[f"{key}_sha256"] = sha256(
                value.encode("utf-8", "replace")
            ).hexdigest()[:16]
    weights = inputs.get("channel_weights")
    if isinstance(weights, dict):
        summary["channel_weights"] = {
            key: weights[key]
            for key in ("transcript", "semantic", "visual")
            if key in weights
            and isinstance(weights[key], (int, float))
            and not isinstance(weights[key], bool)
            and isfinite(weights[key])
        }
    cursor = inputs.get("cursor")
    summary["cursor_present"] = isinstance(cursor, str) and bool(cursor)
    return summary


def _trace_tool_result(output: Any) -> tuple[str, dict[str, Any]]:
    status = output.status if isinstance(output, ToolMessage) else "success"
    content = output.content if isinstance(output, ToolMessage) else output
    if not isinstance(content, str):
        return status, {"result_type": type(content).__name__}
    summary: dict[str, Any] = {
        "result_bytes": len(content.encode("utf-8", "replace"))
    }
    try:
        payload = json.loads(content)
    except ValueError:
        return ("feedback" if status == "success" else status), summary
    if not isinstance(payload, dict):
        return status, summary
    if payload.get("ok") is False:
        status = "error"
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            summary["error_code"] = error["code"]
    for key in (
        "accepted", "operation", "removed", "video_chunk_id", "id",
        "draft_item_count", "video_item_count", "result_count", "total", "partial",
        "section", "start", "end", "total_chars",
    ):
        if key in payload:
            summary[key] = payload[key]
    items = payload.get("items")
    if isinstance(items, list):
        summary["item_count"] = len(items)
        summary["item_video_chunk_ids"] = [
            item["video_chunk_id"] for item in items[:20]
            if isinstance(item, dict) and isinstance(item.get("video_chunk_id"), str)
        ]
    if isinstance(payload.get("content"), str):
        summary["content_chars"] = len(payload["content"])
    if isinstance(payload.get("warnings"), list):
        summary["warning_count"] = len(payload["warnings"])
    if "next_cursor" in payload:
        summary["next_cursor_present"] = payload["next_cursor"] is not None
    return status, summary


def _trace_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


_OptionalCursor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
] | None


class RetrieveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
    ] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    channel_weights: RetrievalChannelWeights | None = None
    cursor: _OptionalCursor = None

    @model_validator(mode="after")
    def validate_initial_or_continuation_call(self) -> RetrieveArguments:
        search_values = (self.query, self.top_k, self.channel_weights)
        if self.cursor is None and any(value is None for value in search_values):
            raise ValueError(
                "query, top_k and channel_weights are required without cursor"
            )
        if self.cursor is not None and any(value is not None for value in search_values):
            raise ValueError("cursor continuation must not repeat search arguments")
        return self


class ReadItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    section: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ] = "overview"
    start: int = Field(default=0, ge=0, strict=True)
    end: int | None = Field(default=None, ge=0, strict=True)


class DraftCatalogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: _OptionalCursor = None


class UpdateDraftTemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    start_ms: int = Field(strict=True)
    end_ms: int = Field(strict=True)
    claim: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
    ]


class RemoveItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]


class UpdateVideoItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]
    claim: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
    ]


@dataclass(slots=True)
class _ToolCallBudget:
    maximum: int
    role: str
    used: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.maximum <= 0
        ):
            raise ValueError("maximum Tool calls must be a positive integer")

    def consume(self, tool_name: str) -> None:
        with self._lock:
            self.used += 1
            if self.used > self.maximum:
                raise AgentModelInvocationError(
                    f"{self.role} Agent Tool-call budget exceeded before {tool_name}"
                )


@dataclass(slots=True)
class DraftReadAudit:
    catalog_complete: bool = False
    expected: dict[str, dict[str, int]] = field(default_factory=dict)
    ranges: dict[str, dict[str, list[tuple[int, int]]]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def mark_catalog_complete(self) -> None:
        with self._lock:
            self.catalog_complete = True

    def mark_overview(self, video_chunk_id: str, sections: dict[str, str]) -> None:
        with self._lock:
            lengths = {name: len(content) for name, content in sections.items()}
            if self.expected.get(video_chunk_id) != lengths:
                self.expected[video_chunk_id] = lengths
                self.ranges[video_chunk_id] = {name: [] for name in lengths}

    def mark_range(
        self, video_chunk_id: str, section: str, start: int, end: int,
    ) -> None:
        with self._lock:
            ranges = self.ranges.get(video_chunk_id)
            if ranges is not None and section in ranges:
                ranges[section].append((start, end))

    def covers(self, video_chunk_ids: Collection[str]) -> bool:
        with self._lock:
            return self.catalog_complete and all(
                video_chunk_id in self.expected
                and all(
                    _ranges_cover(
                        self.ranges[video_chunk_id][section], total_chars,
                    )
                    for section, total_chars in self.expected[video_chunk_id].items()
                )
                for video_chunk_id in video_chunk_ids
            )


def _ranges_cover(ranges: list[tuple[int, int]], total_chars: int) -> bool:
    covered = 0
    for start, end in sorted(ranges):
        if start > covered:
            return False
        covered = max(covered, end)
        if covered >= total_chars:
            return True
    return covered >= total_chars


_EVIDENCE_SECTION_FIELDS = {
    "evidence.transcript": ("transcript",),
    "evidence.events": ("allTimeEvents", "allTimeEventText"),
    "evidence.text": (
        "baselineVisualCaption", "retrievalText", "videoCompact",
        "contextBeforeCompact", "contextAfterCompact", "windowSummary",
    ),
    "evidence.series_context": (
        "seriesDescription", "importantExtraData", "characterBible",
    ),
}


def _detail_sections(
    hit: VideoChunkSearchHit, draft: DraftItem | None = None,
) -> dict[str, str]:
    evidence = hit.evidence.model_dump(mode="json", by_alias=True)
    grouped = {
        name: {field_name: evidence[field_name] for field_name in field_names}
        for name, field_names in _EVIDENCE_SECTION_FIELDS.items()
    }
    grouped_fields = {
        field_name
        for field_names in _EVIDENCE_SECTION_FIELDS.values()
        for field_name in field_names
    }
    grouped["evidence.metadata"] = {
        name: value for name, value in evidence.items() if name not in grouped_fields
    }
    sections = {
        "candidate": hit.candidate.model_dump(mode="json", by_alias=True),
        "selection_reason": hit.selection_reason.model_dump(mode="json", by_alias=True),
        **grouped,
    }
    if draft is not None:
        sections = {"draft": draft.model_dump(mode="json"), **sections}
    return {
        name: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for name, value in sections.items()
    }


def _read_detail_section(
    video_chunk_id: str,
    sections: dict[str, str],
    *,
    section: str,
    start: int,
    end: int | None,
    maximum_result_bytes: int,
    maximum_read_chars: int,
) -> str:
    if section == "overview":
        if start != 0 or end is not None:
            raise AgentToolFeedbackError(
                "INVALID_RANGE", "overview does not accept a character range.",
                "Call with video_chunk_id and section='overview' only.",
            )
        return bounded_json({
            "id": video_chunk_id,
            "section": "overview",
            "sections": [
                {"name": name, "total_chars": len(content)}
                for name, content in sections.items()
            ],
        }, maximum_result_bytes)
    content = sections.get(section)
    if content is None:
        raise AgentToolFeedbackError(
            "UNKNOWN_SECTION", f"Unknown section {section!r}.",
            "Read section='overview' to see the available section names.",
        )
    return range_text(
        content,
        resource_id=video_chunk_id,
        section=section,
        start=start,
        end=end,
        maximum_bytes=maximum_result_bytes,
        maximum_chars=maximum_read_chars,
    )


def build_retrieve_tool(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    """Return bounded discovery cards while retaining canonical hits in Workspace."""

    catalogs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    invocation_lock = Lock()

    def retrieve(
        query: str | None = None,
        top_k: int | None = None,
        channel_weights: RetrievalChannelWeights | None = None,
        cursor: str | None = None,
    ) -> str:
        with invocation_lock:
            _start_tool_call(control, budget, "retrieve")
            if cursor is not None:
                try:
                    position = cursor_position(cursor, namespace="retrieve")
                    catalog = catalogs.get(position.resource_id)
                    if catalog is None:
                        raise AgentToolInvocationError("Unknown or expired retrieve cursor")
                    items, metadata = catalog
                    return page_items(
                        items,
                        namespace="retrieve",
                        resource_id=position.resource_id,
                        cursor=cursor,
                        maximum_items=maximum_catalog_items,
                        maximum_bytes=maximum_result_bytes,
                        metadata={
                            **metadata,
                            "draft_item_count": workspace.draft_item_count(),
                        },
                    )
                except AgentToolOutputError:
                    raise
                except AgentToolInvocationError as exc:
                    raise AgentToolFeedbackError(
                        "INVALID_CATALOG_CURSOR", str(exc),
                        "Call retrieve again with query, top_k and channel_weights.",
                    ) from exc
            if query is None or top_k is None or channel_weights is None:
                raise AgentToolInvocationError(
                    "retrieve requires query, top_k and channel_weights"
                )
            channel_weights = RetrievalChannelWeights.model_validate(channel_weights)
            bounded_top_k = min(top_k, inputs["requested_top_k"], maximum_top_k)
            transcript_weight, semantic_weight, visual_weight = (
                normalize_hybrid_retrieval_weights(
                    channel_weights.transcript,
                    channel_weights.semantic,
                    channel_weights.visual,
                )
            )
            effective_weights = RetrievalChannelWeights(
                transcript=transcript_weight,
                semantic=semantic_weight,
                visual=visual_weight,
            )
            try:
                result = search(
                    query,
                    workspace_id=inputs["workspace_id"],
                    series_ids=list(inputs["series_ids"]),
                    top_k=bounded_top_k,
                    transcript_weight=transcript_weight,
                    text_weight=semantic_weight,
                    vision_weight=visual_weight,
                )
            except AgentModelInvocationError:
                raise
            except Exception as exc:
                raise AgentToolInvocationError("Video retrieval tool failed") from exc
            control.check()
            result = VideoChunkSearchResult.model_validate(result).model_copy(deep=True)
            validate_result(result, inputs)
            if result.query != query:
                raise AgentModelInvocationError(
                    "Retrieval result query does not match the actual call"
                )
            if len(result.hits) > bounded_top_k:
                raise AgentModelInvocationError("Retrieval result exceeds its top_k budget")

            workspace.record_retrieval(result)
            calls.append(RetrievalCall(
                query=query,
                top_k=bounded_top_k,
                requested_weights=channel_weights.model_copy(deep=True),
                effective_weights=effective_weights,
                result=result,
            ))
            resource_id = f"retrieval-{len(catalogs) + 1}"
            items = [_retrieval_card(hit) for hit in result.hits]
            metadata = {
                "query": query,
                "result_count": len(items),
                "draft_item_count": workspace.draft_item_count(),
                "partial": result.partial,
                "warnings": [_bounded_text(value, 500) for value in result.warnings[:8]],
                "retrieval_strategy": {
                    "version": AGENT_RETRIEVAL_STRATEGY_VERSION,
                    "requested_weights": channel_weights.model_dump(mode="json"),
                    "effective_weights": effective_weights.model_dump(mode="json"),
                },
            }
            catalogs[resource_id] = (items, metadata)
            return page_items(
                items,
                namespace="retrieve",
                resource_id=resource_id,
                cursor=None,
                maximum_items=maximum_catalog_items,
                maximum_bytes=maximum_result_bytes,
                metadata=metadata,
            )

    return _build_agent_tool(
        func=retrieve,
        name="retrieve",
        description=(
            "Search the fixed Series Scope and return a bounded Evidence catalog. "
            "Catalog entries contain descriptions and video_chunk_id handles, never "
            "the full Evidence. Use read_trieve_item to inspect a selected handle. "
            "When next_cursor is present, call retrieve with cursor only."
        ),
        args_schema=RetrieveArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
    )


def build_read_trieve_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_read_chars: int = 16_384,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def read_trieve_item(
        video_chunk_id: str,
        section: str = "overview",
        start: int = 0,
        end: int | None = None,
    ) -> str:
        _start_tool_call(control, budget, "read_trieve_item")
        sections = _detail_sections(workspace.retrieval_snapshot(video_chunk_id))
        return _read_detail_section(
            video_chunk_id, sections,
            section=section,
            start=start,
            end=end,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
        )

    return _build_agent_tool(
        func=read_trieve_item,
        name="read_trieve_item",
        description=(
            "Read a stable Evidence snapshot by video_chunk_id. First request "
            "section='overview' to get section names and character lengths. Then "
            "read a named section with start/end character offsets [start,end); "
            "omit end to read to this section's end, and never reuse another ID's end. "
            "If a range is too large, retry with the suggested smaller end."
        ),
        args_schema=ReadItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_update_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def update_draft_item(
        video_chunk_id: str,
        start_ms: int,
        end_ms: int,
        claim: str,
    ) -> str:
        _start_tool_call(control, budget, "update_draft_item")
        try:
            operation, draft_item_count = workspace.upsert_draft_item(
                video_chunk_id=video_chunk_id,
                start_ms=start_ms,
                end_ms=end_ms,
                claim=claim,
            )
        except InvalidDraftRangeError as exc:
            raise AgentToolFeedbackError(
                "INVALID_DRAFT_RANGE",
                "时间区间不合法："
                f"当前提交 start_ms={exc.start_ms}, end_ms={exc.end_ms}；"
                "必须满足 "
                f"{exc.evidence_start_ms} <= start_ms < end_ms "
                f"<= {exc.evidence_end_ms}。",
                "请按 canonical Evidence 的时间范围修正后重新调用 update_draft_item。",
            ) from exc
        return bounded_json({
            "accepted": True,
            "operation": operation,
            "video_chunk_id": video_chunk_id,
            "draft_item_count": draft_item_count,
            **(
                {"message": "该片段已在草稿中，提交内容未变。"}
                if operation == "already_present" else {}
            ),
        }, maximum_result_bytes)

    return _build_agent_tool(
        func=update_draft_item,
        name="update_draft_item",
        description=(
            "Insert or update one Draft Item by video_chunk_id. The Evidence must have "
            "been retrieved, and the range must satisfy canonical_start_ms <= "
            "start_ms < end_ms <= canonical_end_ms."
        ),
        args_schema=UpdateDraftTemArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
    )


def build_remove_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def remove_draft_item(video_chunk_id: str) -> str:
        _start_tool_call(control, budget, "remove_draft_item")
        removed, draft_item_count = workspace.remove_draft_item(video_chunk_id)
        return bounded_json({
            "removed": removed,
            "video_chunk_id": video_chunk_id,
            "draft_item_count": draft_item_count,
        }, maximum_result_bytes)

    return _build_agent_tool(
        func=remove_draft_item,
        name="remove_draft_item",
        description="Remove one Draft Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
        maximum_result_bytes=maximum_result_bytes,
        draft_item_count=workspace.draft_item_count,
    )


def build_get_draft_catelog_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    approved: bool = False,
    budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> StructuredTool:
    resource_id = "approved-draft" if approved else "draft"

    def get_draft_catelog(cursor: str | None = None) -> str:
        _start_tool_call(control, budget, "get_draft_catelog")
        items = [_draft_card(workspace, item) for item in workspace.draft_items(
            approved=approved,
        )]
        try:
            result = page_items(
                items,
                namespace="get_draft_catelog",
                resource_id=resource_id,
                cursor=cursor,
                maximum_items=maximum_catalog_items,
                maximum_bytes=maximum_result_bytes,
            )
        except AgentToolOutputError:
            raise
        except AgentToolInvocationError as exc:
            raise AgentToolFeedbackError(
                "INVALID_CATALOG_CURSOR", str(exc),
                "Call get_draft_catelog without cursor to read the current catalog.",
            ) from exc
        if audit is not None and json.loads(result)["next_cursor"] is None:
            audit.mark_catalog_complete()
        return result

    return _build_agent_tool(
        func=get_draft_catelog,
        name="get_draft_catelog",
        description=(
            "List bounded Draft descriptions and video_chunk_id handles. Use "
            "read_draft_item for the full Draft Item and its canonical Evidence."
        ),
        args_schema=DraftCatalogArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_read_draft_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    maximum_read_chars: int = 16_384,
    approved: bool = False,
    budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> StructuredTool:
    def read_draft_item(
        video_chunk_id: str,
        section: str = "overview",
        start: int = 0,
        end: int | None = None,
    ) -> str:
        _start_tool_call(control, budget, "read_draft_item")
        item = workspace.draft_item(video_chunk_id, approved=approved)
        sections = _detail_sections(
            workspace.retrieval_snapshot(video_chunk_id), item,
        )
        result = _read_detail_section(
            video_chunk_id, sections,
            section=section,
            start=start,
            end=end,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
        )
        if audit is not None:
            if section == "overview":
                audit.mark_overview(video_chunk_id, sections)
            else:
                response = json.loads(result)
                audit.mark_range(
                    video_chunk_id, section, response["start"], response["end"],
                )
        return result

    return _build_agent_tool(
        func=read_draft_item,
        name="read_draft_item",
        description=(
            "Read a Draft Item and its stable Evidence snapshot by video_chunk_id. "
            "First request section='overview'; then read every required section "
            "with start/end character offsets [start,end). Omit end to read to this "
            "section's end; never reuse another ID's end. Retry oversized ranges "
            "with the suggested smaller end."
        ),
        args_schema=ReadItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_update_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def update_video_item(video_chunk_id: str, claim: str) -> str:
        _start_tool_call(control, budget, "update_video_item")
        inserted = workspace.upsert_video_item(ChatVideoBlock(
            video_chunk_id=video_chunk_id,
            claim=claim,
        ))
        return bounded_json({
            "accepted": True,
            "operation": "inserted" if inserted else "updated",
            "video_chunk_id": video_chunk_id,
            "video_item_count": workspace.video_item_count(),
        }, maximum_result_bytes)

    return _build_agent_tool(
        func=update_video_item,
        name="update_video_item",
        description=(
            "Insert or update one final Video Item from the approved Draft. Do not "
            "provide playback times; the Server binds the approved Draft range."
        ),
        args_schema=UpdateVideoItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_remove_video_item_tool(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> StructuredTool:
    def remove_video_item(video_chunk_id: str) -> str:
        _start_tool_call(control, budget, "remove_video_item")
        removed = workspace.remove_video_item(video_chunk_id)
        return bounded_json({
            "removed": removed,
            "video_chunk_id": video_chunk_id,
            "video_item_count": workspace.video_item_count(),
        }, maximum_result_bytes)

    return _build_agent_tool(
        func=remove_video_item,
        name="remove_video_item",
        description="Remove one final Video Item by video_chunk_id.",
        args_schema=RemoveItemArguments,
        maximum_result_bytes=maximum_result_bytes,
    )


def build_query_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Query")
        if maximum_tool_calls is not None else None
    )
    return [
        build_retrieve_tool(
            search,
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
        build_update_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_remove_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
    ]


def build_evaluator_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
    audit: DraftReadAudit | None = None,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Evaluator")
        if maximum_tool_calls is not None else None
    )
    return [
        build_retrieve_tool(
            search,
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
            audit=audit,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
            audit=audit,
        ),
    ]


def build_chat_tools(
    *,
    workspace: AgentWorkspace,
    control: ExecutionControl,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
) -> list[StructuredTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Chat")
        if maximum_tool_calls is not None else None
    )
    return [
        build_remove_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_update_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            approved=True,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            approved=True,
            budget=budget,
        ),
    ]


def _start_tool_call(
    control: ExecutionControl,
    budget: _ToolCallBudget | None,
    tool_name: str,
) -> None:
    control.check()
    if budget is not None:
        budget.consume(tool_name)


def _retrieval_card(hit: VideoChunkSearchHit) -> dict[str, Any]:
    return {
        "video_chunk_id": hit.candidate.video_chunk_id,
        "episode_order": hit.evidence.episode_order,
        "start_ms": hit.evidence.start_ms,
        "end_ms": hit.evidence.end_ms,
        "description": _bounded_text(hit.selection_reason.summary, 500),
        "fused_rank": hit.candidate.fused_rank,
        "fused_score": hit.candidate.fused_score,
        "transcript_rank": hit.candidate.transcript_rank,
        "semantic_rank": hit.candidate.text_rank,
        "visual_rank": hit.candidate.vision_rank,
    }


def _draft_card(workspace: AgentWorkspace, item: DraftItem) -> dict[str, Any]:
    evidence = workspace.retrieval_item(item.video_chunk_id).evidence
    return {
        "video_chunk_id": item.video_chunk_id,
        "episode_order": evidence.episode_order,
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "description": _bounded_text(item.claim, 500),
    }


def _bounded_text(value: str, maximum_chars: int) -> str:
    if len(value) <= maximum_chars:
        return value
    return value[: maximum_chars - 1].rstrip() + "…"
