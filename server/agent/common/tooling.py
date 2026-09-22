from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from hashlib import sha256
import json
import logging
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel

from .errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolInvocationError,
    UnknownToolItemError,
)
from .state import RunInputs
from .tool_output import bounded_json

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


def build_agent_tool(
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
        "section", "start", "end", "total_chars", "omitted_count",
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
