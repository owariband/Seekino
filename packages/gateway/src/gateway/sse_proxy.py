from __future__ import annotations

import json
from typing import Any

_OUTPUT_CONTENT_FILTER_CODES = frozenset(
    {
        "OutputTextSensitiveContentDetected",
        "content_filter",
    }
)


class SSEProxyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        response_id: str | None = None,
        partial_output_chars: int = 0,
        content_filter_type: str | None = None,
        content_filter_details: str | None = None,
    ) -> None:
        self.code = code
        self.response_id = response_id
        self.partial_output_chars = partial_output_chars
        self.content_filter_type = content_filter_type
        self.content_filter_details = content_filter_details

        details = [message]
        if code:
            details.append(f"code={code}")
        if response_id:
            details.append(f"response_id={response_id}")
        details.append(f"partial_output_chars={partial_output_chars}")
        super().__init__("; ".join(details))


class ContentFilterError(SSEProxyError):
    """The provider explicitly rejected generated output via content policy."""


def request_sse_json(responses: Any, **request: Any) -> dict[str, Any]:
    try:
        stream = responses.create(stream=True, **request)
    except Exception as error:
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
    text_parts: dict[tuple[int, str, int], list[str]] = {}
    done_texts: dict[tuple[int, str, int], str] = {}
    completed_response: object | None = None
    response_id: str | None = None
    last_sequence = -1

    try:
        for event in stream:
            event_type = getattr(event, "type", None)
            sequence = getattr(event, "sequence_number", None)
            if isinstance(sequence, int):
                if sequence <= last_sequence:
                    raise SSEProxyError(
                        "Ark Responses SSE sequence is not strictly increasing",
                        response_id=response_id,
                        partial_output_chars=_output_length(text_parts),
                    )
                last_sequence = sequence

            response = getattr(event, "response", None)
            response_id = getattr(response, "id", None) or response_id

            if event_type == "response.output_text.delta":
                key = (
                    getattr(event, "output_index", 0),
                    getattr(event, "item_id", ""),
                    getattr(event, "content_index", 0),
                )
                delta = getattr(event, "delta", None)
                if delta:
                    text_parts.setdefault(key, []).append(delta)
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
                    or "Ark Responses SSE failed",
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
                message = reason or "Ark Responses SSE was incomplete"
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
                    or "Ark Responses SSE returned an error event",
                    code=getattr(event, "code", None),
                    response_id=response_id,
                    partial_output_chars=_output_length(text_parts),
                )
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    if completed_response is None:
        raise SSEProxyError(
            "Ark Responses SSE ended without a terminal event",
            response_id=response_id,
            partial_output_chars=_output_length(text_parts),
        )

    streamed_text = _join_streamed_text(text_parts, done_texts, response_id)
    response_text = _extract_response_text(completed_response)
    if response_text and streamed_text and response_text != streamed_text:
        raise SSEProxyError(
            "Ark Responses SSE output differs from the completed response",
            response_id=response_id,
            partial_output_chars=len(streamed_text),
        )

    output_text = response_text or streamed_text
    if not output_text:
        raise SSEProxyError(
            "Ark Responses SSE returned no output text",
            response_id=response_id,
        )

    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise SSEProxyError(
            f"Ark Responses SSE returned invalid JSON at char {error.pos}",
            response_id=response_id,
            partial_output_chars=len(output_text),
        ) from error
    if not isinstance(payload, dict):
        raise SSEProxyError(
            "Ark Responses SSE JSON root must be an object",
            response_id=response_id,
            partial_output_chars=len(output_text),
        )
    return payload


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
                "Ark Responses SSE delta text differs from output_text.done",
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
    return "".join(texts)
