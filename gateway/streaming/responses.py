from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from gateway.errors import ContentFilterError, SSEProxyError
from gateway.model_diagnostics import capture_model_diagnostics, record_model_event
from gateway.usage import ModelCallTracker, track_model_call

_logger = logging.getLogger(__name__)
_DIAGNOSTIC_EVENT_TYPES = frozenset({
    "response.created", "response.in_progress", "response.completed",
    "response.failed", "response.incomplete", "error",
    "response.output_text.delta", "response.output_text.done",
    "response.output_item.added", "response.output_item.done",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.content_part.added", "response.content_part.done",
    "response.reasoning_text.delta", "response.reasoning_text.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
})
_DIAGNOSTIC_ITEM_TYPES = frozenset({"message", "function_call", "reasoning", "refusal"})
_DIAGNOSTIC_CONTENT_TYPES = frozenset({"output_text", "reasoning_text", "refusal"})

_OUTPUT_CONTENT_FILTER_CODES = frozenset(
    {
        "OutputTextSensitiveContentDetected",
        "content_filter",
    }
)


def request_response(
    responses: Any, *, stream: bool, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None, **request: Any,
) -> tuple[Any, str]:
    """Return one verified Responses turn using the configured transport."""
    if not isinstance(stream, bool):
        raise TypeError("stream must be a boolean")
    if stream:
        return request_sse_response(responses, usage_call=usage_call, check_control=check_control, **request)
    usage_call.raise_if_cancelled()
    if check_control is not None:
        check_control()
    response = responses.create(stream=False, **request)
    usage_call.capture_response(response)
    record_model_event("response", response)
    usage_call.raise_if_cancelled()
    if check_control is not None:
        check_control()
    status = getattr(response, "status", None)
    if status not in {None, "completed"}:
        code = (getattr(getattr(response, "error", None), "code", None)
                or getattr(getattr(response, "incomplete_details", None), "reason", None))
        error_type = ContentFilterError if code in _OUTPUT_CONTENT_FILTER_CODES else SSEProxyError
        raise error_type(f"Responses request did not complete: {status}", code=code, response_id=getattr(response, "id", None))
    return response, _extract_response_text(response)


def request_sse_response(
    responses: Any, *, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None, **request: Any,
) -> tuple[Any, str]:
    """Read a verified terminal response, including function-call-only turns."""
    metadata: dict[str, Any] = {}
    for _ in _request_sse_text(
        responses, allow_terminal_text=True, allow_function_calls=True,
        metadata=metadata, usage_call=usage_call, check_control=check_control,
        **request,
    ):
        pass
    return metadata["response"], metadata.get("output_text", "")


def request_response_json(
    responses: Any, *, usage_call: ModelCallTracker | None = None,
    stream: bool = True, check_control: Callable[[], None] | None = None, **request: Any,
) -> dict[str, Any]:
    with _usage_scope(usage_call, request) as call, capture_model_diagnostics(call, {**request, "stream": stream}):
        response, output_text = request_response(
            responses, stream=stream, usage_call=call, check_control=check_control, **request,
        )
        if not output_text:
            raise SSEProxyError("Responses returned no output text", response_id=getattr(response, "id", None))
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise SSEProxyError(
                f"Responses returned invalid JSON at char {error.pos}",
                response_id=getattr(response, "id", None), partial_output_chars=len(output_text),
            ) from error
        if not isinstance(payload, dict):
            raise SSEProxyError("Responses JSON root must be an object", response_id=getattr(response, "id", None))
        return payload


def request_sse_text(
    responses: Any, *, usage_call: ModelCallTracker | None = None, **request: Any
) -> Iterator[str]:
    """Yield original provider deltas; exhaustion means completion was verified."""
    with _usage_scope(usage_call, request) as call, capture_model_diagnostics(call, {**request, "stream": True}):
        yield from _request_sse_text(responses, allow_terminal_text=False, usage_call=call, **request)


def _usage_scope(
    usage_call: ModelCallTracker | None, request: dict[str, Any]
) -> AbstractContextManager[ModelCallTracker]:
    if usage_call is not None:
        return nullcontext(usage_call)
    operation = request.get("text", {}).get("format", {}).get("name", "text_generation")
    return track_model_call(provider="ark", model=request.get("model", "unknown"), operation=operation)


def _request_sse_text(
    responses: Any,
    *,
    allow_terminal_text: bool,
    usage_call: ModelCallTracker,
    metadata: dict[str, Any] | None = None,
    allow_function_calls: bool = False,
    check_control: Callable[[], None] | None = None,
    **request: Any,
) -> Iterator[str]:
    usage_call.raise_if_cancelled()
    if check_control is not None:
        check_control()
    try:
        stream = responses.create(stream=True, **request)
    except Exception as error:
        usage_call.capture_response(error)
        code = getattr(error, "code", None)
        if (
            getattr(error, "status_code", None) == 400
            and code == "OutputTextSensitiveContentDetected"
        ):
            raise ContentFilterError(
                getattr(error, "message", None) or str(error),
                code=code,
                response_id=getattr(error, "request_id", None),
            ) from error
        raise
    usage_call.capture_response(getattr(stream, "response", None))
    close = getattr(stream, "close", None)
    unregister_cancel = (
        usage_call.add_cancel_callback(close) if callable(close) else lambda: None
    )
    text_parts: dict[tuple[int, str, int], list[str]] = {}
    emitted_parts: list[str] = []
    done_texts: dict[tuple[int, str, int], str] = {}
    completed_response: object | None = None
    response_id: str | None = None
    last_sequence = -1
    event_counts: Counter[str] = Counter()
    stream_item_counts: Counter[str] = Counter()

    try:
        for event in stream:
            usage_call.raise_if_cancelled()
            if check_control is not None:
                check_control()
            response = getattr(event, "response", None)
            event_type = getattr(event, "type", None)
            event_kind = event_type if isinstance(event_type, str) and event_type in _DIAGNOSTIC_EVENT_TYPES else "other"
            event_counts[event_kind] += 1
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                kind = item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)
                item_kind = kind if isinstance(kind, str) and kind in _DIAGNOSTIC_ITEM_TYPES else "other"
                stream_item_counts[item_kind] += 1
            usage_call.capture_response(response, include_usage=event_type in {"response.completed", "response.failed", "response.incomplete"})
            record_model_event("sse", event)
            sequence = getattr(event, "sequence_number", None)
            if isinstance(sequence, int):
                if sequence <= last_sequence:
                    raise SSEProxyError(
                        "Responses SSE sequence is not strictly increasing",
                        response_id=response_id,
                        partial_output_chars=_output_length(text_parts),
                    )
                last_sequence = sequence

            response_id = getattr(response, "id", None) or response_id
            if metadata is not None:
                metadata["response_id"] = response_id

            if event_type == "response.output_text.delta":
                key = (
                    getattr(event, "output_index", 0),
                    getattr(event, "item_id", ""),
                    getattr(event, "content_index", 0),
                )
                delta = getattr(event, "delta", None)
                if delta:
                    if not isinstance(delta, str):
                        raise SSEProxyError("Responses SSE delta must be text")
                    text_parts.setdefault(key, []).append(delta)
                    emitted_parts.append(delta)
                    yield delta
                continue

            if event_type == "response.output_text.done":
                key = (
                    getattr(event, "output_index", 0),
                    getattr(event, "item_id", ""),
                    getattr(event, "content_index", 0),
                )
                text = getattr(event, "text", None)
                if text:
                    done_texts[key] = text
                continue

            if event_type == "response.completed":
                status = getattr(response, "status", None)
                if status not in {None, "completed"}:
                    raise SSEProxyError(
                        f"Responses SSE completed event has status {status}",
                        response_id=response_id,
                        partial_output_chars=_output_length(text_parts),
                    )
                completed_response = response
                break

            if event_type == "response.failed":
                error = getattr(response, "error", None)
                code = getattr(error, "code", None)
                error_type = (
                    ContentFilterError
                    if code in _OUTPUT_CONTENT_FILTER_CODES
                    else SSEProxyError
                )
                raise error_type(
                    getattr(error, "message", None)
                    or "Responses SSE failed",
                    code=code,
                    response_id=response_id,
                    partial_output_chars=_output_length(text_parts),
                )

            if event_type == "response.incomplete":
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                content_filter = getattr(details, "content_filter", None)
                filter_type = getattr(content_filter, "type", None)
                filter_details = getattr(content_filter, "details", None)
                error_type = (
                    ContentFilterError
                    if reason == "content_filter" or content_filter is not None
                    else SSEProxyError
                )
                message = reason or "Responses SSE was incomplete"
                if filter_type:
                    message += f" ({filter_type})"
                raise error_type(
                    message,
                    code=reason,
                    response_id=response_id,
                    partial_output_chars=_output_length(text_parts),
                    content_filter_type=filter_type,
                    content_filter_details=filter_details,
                )

            if event_type == "error":
                raise SSEProxyError(
                    getattr(event, "message", None)
                    or "Responses SSE returned an error event",
                    code=getattr(event, "code", None),
                    response_id=response_id,
                    partial_output_chars=_output_length(text_parts),
                )
    finally:
        unregister_cancel()
        if callable(close):
            close()

    if completed_response is None:
        raise SSEProxyError(
            "Responses SSE ended without a terminal event",
            response_id=response_id,
            partial_output_chars=_output_length(text_parts),
        )

    streamed_text = _join_streamed_text(text_parts, done_texts, response_id)
    response_text = _extract_response_text(completed_response)
    if response_text and streamed_text and response_text != streamed_text:
        raise SSEProxyError(
            "Responses SSE output differs from the completed response",
            response_id=response_id,
            partial_output_chars=len(streamed_text),
        )

    output_text = response_text or streamed_text
    if metadata is not None:
        metadata["response"] = completed_response
        metadata["output_text"] = output_text
    if not output_text and allow_function_calls and any(
        getattr(item, "type", None) == "function_call"
        for item in getattr(completed_response, "output", None) or []
    ):
        return
    if not output_text:
        output = completed_response.get("output") if isinstance(completed_response, Mapping) else getattr(completed_response, "output", None)
        items = output if isinstance(output, (list, tuple)) else []
        item_types: Counter[str] = Counter()
        item_classes: Counter[str] = Counter()
        content_types: Counter[str] = Counter()
        content_classes: Counter[str] = Counter()
        dict_count = int(isinstance(completed_response, Mapping)) + int(isinstance(output, Mapping))
        text_fields = empty_text_fields = function_count = 0
        refusal_present = False
        truncated = len(items) > 32
        for item in items[:32]:
            dict_count += int(isinstance(item, Mapping))
            item_classes[type(item).__name__[:64]] += 1
            kind = item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)
            item_kind = kind if isinstance(kind, str) and kind in _DIAGNOSTIC_ITEM_TYPES else "other"
            item_types[item_kind] += 1
            function_count += int(getattr(item, "type", None) == "function_call")
            refusal_present |= kind == "refusal"
            content = item.get("content") if isinstance(item, Mapping) else getattr(item, "content", None)
            dict_count += int(isinstance(content, Mapping))
            parts = content if isinstance(content, (list, tuple)) else []
            truncated |= len(parts) > 32
            for part in parts[:32]:
                dict_count += int(isinstance(part, Mapping))
                class_name = type(part).__name__[:64]
                content_classes[class_name if len(content_classes) < 32 or class_name in content_classes else "other"] += 1
                kind = part.get("type") if isinstance(part, Mapping) else getattr(part, "type", None)
                content_kind = kind if isinstance(kind, str) and kind in _DIAGNOSTIC_CONTENT_TYPES else "other"
                content_types[content_kind] += 1
                refusal_present |= kind == "refusal"
                if kind == "output_text":
                    present = "text" in part if isinstance(part, Mapping) else hasattr(part, "text")
                    text = part.get("text") if isinstance(part, Mapping) else getattr(part, "text", None)
                    text_fields += int(present)
                    empty_text_fields += int(present and text == "")
        tools = completed_response.get("tools") if isinstance(completed_response, Mapping) else getattr(completed_response, "tools", None)
        choice = completed_response.get("tool_choice") if isinstance(completed_response, Mapping) else getattr(completed_response, "tool_choice", None)
        text_config = completed_response.get("text") if isinstance(completed_response, Mapping) else getattr(completed_response, "text", None)
        status = completed_response.get("status") if isinstance(completed_response, Mapping) else getattr(completed_response, "status", None)
        format_config = text_config.get("format") if isinstance(text_config, Mapping) else getattr(text_config, "format", None)
        format_type = format_config.get("type") if isinstance(format_config, Mapping) else getattr(format_config, "type", None)
        strict = format_config.get("strict") if isinstance(format_config, Mapping) else getattr(format_config, "strict", None)
        summary = {
            "call_id": usage_call.call_id, "provider": usage_call.provider[:160],
            "model": usage_call.model[:160], "operation": usage_call.operation[:160],
            "provider_request_id": usage_call.request_id[:160] if isinstance(usage_call.request_id, str) else None,
            "response_id": response_id[:160] if isinstance(response_id, str) else None,
            "terminal_status": status if status is None or status == "completed" else "other",
            "event_types": dict(event_counts), "stream_item_types": dict(stream_item_counts),
            "output_item_done_count": event_counts["response.output_item.done"],
            "function_args_delta_count": event_counts["response.function_call_arguments.delta"],
            "function_args_done_count": event_counts["response.function_call_arguments.done"],
            "output_count": len(items), "output_container_type": type(output).__name__[:64],
            "item_classes": dict(item_classes), "item_types": dict(item_types),
            "content_classes": dict(content_classes), "content_types": dict(content_types),
            "text_chars": len(output_text), "terminal_text_chars": len(response_text),
            "streamed_text_chars": len(streamed_text), "function_count": function_count,
            "text_field_count": text_fields, "empty_text_field_count": empty_text_fields,
            "unknown_dict_structures": dict_count, "refusal_present": refusal_present,
            "allow_function_calls": allow_function_calls, "output_rejected": True,
            "summary_truncated": truncated,
            "terminal_tool_count": len(tools) if isinstance(tools, (list, tuple)) else None,
            "terminal_tool_choice": (
                choice if isinstance(choice, str) and choice in {"auto", "none", "required"}
                else "other" if choice is not None else None
            ),
            "terminal_format_type": (
                format_type if isinstance(format_type, str) and format_type in {"text", "json_object", "json_schema"}
                else "other" if format_type is not None else None
            ),
            "terminal_format_strict": strict if isinstance(strict, bool) else None,
        }
        for key in (
            "video_parse_task_id", "run_id", "chunk_parse_task_id", "interval_parse_task_id",
            "video_chunk_id", "sequence_no", "lease_version",
        ):
            value = usage_call.context.get(key)
            if isinstance(value, (str, int)) or value is None:
                summary[key] = value[:160] if isinstance(value, str) else value
        _logger.warning(
            "event=ark_sse_empty_output summary=%s",
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        )
        raise SSEProxyError(
            "Responses SSE returned no output text",
            response_id=response_id,
        )

    if metadata is not None:
        metadata["output_text"] = output_text
    if not text_parts:
        if allow_terminal_text:
            yield output_text
            return
        raise SSEProxyError(
            "Responses SSE returned no text deltas",
            response_id=response_id,
        )
    if not allow_terminal_text and output_text != "".join(emitted_parts):
        raise SSEProxyError(
            "Responses SSE output differs from emitted text deltas",
            response_id=response_id,
            partial_output_chars=_output_length(text_parts),
        )


def _output_length(
    text_parts: dict[tuple[int, str, int], list[str]],
) -> int:
    return sum(len(chunk) for chunks in text_parts.values() for chunk in chunks)


def _join_streamed_text(
    text_parts: dict[tuple[int, str, int], list[str]],
    done_texts: dict[tuple[int, str, int], str],
    response_id: str | None,
) -> str:
    texts: list[str] = []
    for key in sorted(text_parts.keys() | done_texts.keys()):
        delta_text = "".join(text_parts.get(key, []))
        done_text = done_texts.get(key, "")
        if delta_text and done_text and delta_text != done_text:
            raise SSEProxyError(
                "Responses SSE delta text differs from output_text.done",
                response_id=response_id,
                partial_output_chars=_output_length(text_parts),
            )
        texts.append(done_text or delta_text)
    return "".join(texts)


def _extract_response_text(response: object) -> str:
    texts: list[str] = []
    for output in getattr(response, "output", None) or []:
        if getattr(output, "type", None) != "message":
            continue
        for content in getattr(output, "content", None) or []:
            if getattr(content, "type", None) != "output_text":
                continue
            text = getattr(content, "text", None)
            if text:
                texts.append(text)
    if texts:
        return "".join(texts)
    output_text = getattr(response, "output_text", None)
    return output_text if isinstance(output_text, str) else ""
