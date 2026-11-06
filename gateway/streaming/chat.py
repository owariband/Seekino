"""Forward optional display text while collecting verified results and tools."""
from __future__ import annotations

import socket

from collections.abc import Callable, Iterator
from contextlib import closing
from time import monotonic
from typing import Any

import httpx
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.model_diagnostics import capture_model_diagnostics, record_model_event
from gateway.model_events import ModelReasoningDelta, ModelTextDelta, emit_model_reasoning, emit_model_text
from gateway.usage import ModelCallTracker
from gateway.streaming.first_sse import FirstSSEDeadline


def request_chat_completion(
    completions: Any, *, stream: bool, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None,
    first_sse_deadline: FirstSSEDeadline | None = None, **request: Any,
) -> ChatCompletion:
    """Execute the model's configured transport and return a complete turn."""
    if not isinstance(stream, bool):
        raise TypeError("stream must be a boolean")
    request["stream"] = stream
    if stream:
        request["stream_options"] = {**request.get("stream_options", {}), "include_usage": True}
    else:
        request.pop("stream_options", None)
    with capture_model_diagnostics(usage_call, request):
        usage_call.raise_if_cancelled()
        if check_control is not None:
            check_control()
        if stream:
            return request_chat_stream(
                completions, usage_call=usage_call, check_control=check_control,
                first_sse_deadline=first_sse_deadline, **request,
            )
        record_model_event("chat_transport", {"stream": False, "started_monotonic": monotonic()})
        response = completions.create(**request)
        usage_call.capture_response(response)
        record_model_event("response", response)
        usage_call.raise_if_cancelled()
        if check_control is not None:
            check_control()
        return response


def request_chat_stream(
    completions: Any, *, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None,
    first_sse_deadline: FirstSSEDeadline | None = None, **request: Any,
) -> ChatCompletion:
    """Collect a verified turn with SDK delta assembly before executing tools."""
    state = ChatCompletionStreamState()
    usage = None
    with closing(_iter_chat_chunks(
        completions, usage_call=usage_call, check_control=check_control,
        first_sse_deadline=first_sse_deadline, **request,
    )) as chunks:
        for chunk in chunks:
            if chunk.usage is not None:
                usage = chunk.usage
            try:
                state.handle_chunk(chunk)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
                raise ModelFailureError("Chat completion stream returned an invalid delta") from error
    response = state.current_completion_snapshot
    response.usage = usage
    record_model_event("response", response)
    return response


def stream_chat_text(completions: Any, *, usage_call: ModelCallTracker, **request: Any) -> Iterator[str]:
    """Expose visible text deltas; successful exhaustion requires a stop choice."""
    request.update(stream=True, stream_options={"include_usage": True})
    output_chars = 0
    with closing(_iter_chat_chunks(completions, usage_call=usage_call, **request)) as chunks:
        for chunk in chunks:
            for choice in chunk.choices:
                if choice.finish_reason not in {None, "stop"}:
                    raise ModelFailureError(f"chat stream did not complete: {choice.finish_reason}")
                delta = choice.delta.content
                if delta:
                    output_chars += len(delta)
                    yield delta
    if not output_chars:
        raise ModelFailureError("chat stream ended without completed text")


def _iter_chat_chunks(
    completions: Any, *, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None,
    first_sse_deadline: FirstSSEDeadline | None = None, **request: Any,
) -> Iterator[Any]:
    # The SDK decodes SSE. We additionally reject EOF without a terminal choice.
    started_at = monotonic()
    deadline = started_at + request["timeout"]
    text_sequence = 0
    reasoning_sequence = 0
    first_text_ms = None

    def check() -> None:
        usage_call.raise_if_cancelled()
        if check_control is not None:
            check_control()
        if first_sse_deadline is not None:
            first_sse_deadline.raise_if_expired()
        if monotonic() >= deadline:
            raise ModelFailureError(
                "Chat completion stream exceeded its time budget", reason=ModelFailureReason.MODEL_TIMEOUT,
            )

    received_choice = False
    terminal = False
    unregister = lambda: None
    unregister_startup = lambda: None
    if first_sse_deadline is not None:
        first_sse_deadline.start()
        unregister_startup = usage_call.add_cancel_callback(first_sse_deadline.interrupt)
    try:
        check()
        record_model_event("chat_transport", {"stream": True, "started_monotonic": started_at})
        stream = completions.create(**request)
        with stream:
            if first_sse_deadline is not None:
                first_sse_deadline.attach_response(stream.response)
            usage_call.capture_response(stream.response, include_usage=False)
            unregister = usage_call.add_cancel_callback(lambda: _cancel_chat_stream(stream))
            for chunk in stream:
                received_at = monotonic()
                check()
                if first_sse_deadline is not None:
                    first_sse_deadline.received()
                if not isinstance(chunk, ChatCompletionChunk):
                    try:
                        chunk = ChatCompletionChunk.model_validate(chunk.model_dump())
                    except (AttributeError, TypeError, ValueError) as error:
                        raise ModelFailureError("Chat completion stream returned an invalid delta") from error
                usage_call.capture_response(chunk, include_usage=chunk.usage is not None)
                record_model_event("sse", chunk)
                for choice in chunk.choices:
                    # Reject ambiguous or late deltas instead of accepting a partial turn.
                    if choice.index != 0 or terminal:
                        raise ModelFailureError("Chat completion stream returned an unexpected choice")
                    received_choice = True
                    terminal = choice.finish_reason is not None
                    text = choice.delta.content
                    reasoning = getattr(choice.delta, "reasoning_content", None)
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_sequence += 1
                        record_model_event("chat_reasoning_delta", {
                            "sequence": reasoning_sequence, "chars": len(reasoning),
                            "received_monotonic": received_at,
                        })
                        emit_model_reasoning(ModelReasoningDelta(
                            reasoning, usage_call.call_id, reasoning_sequence, received_at,
                        ))
                    if isinstance(text, str) and text:
                        elapsed_ms = (received_at - started_at) * 1000
                        if first_text_ms is None and text.strip():
                            first_text_ms = elapsed_ms
                        text_sequence += 1
                        delta = ModelTextDelta(
                            text, usage_call.call_id, text_sequence, received_at,
                            elapsed_ms, first_text_ms, not terminal,
                        )
                        record_model_event("chat_text_delta", {
                            "sequence": text_sequence, "chars": len(text),
                            "received_monotonic": received_at, "elapsed_ms": elapsed_ms,
                            "first_text_ms": first_text_ms, "before_completion": not terminal,
                        })
                        emit_model_text(delta)
                yield chunk
            check()
    except Exception as error:
        # A watchdog-induced socket error is a retryable timeout, not task
        # cancellation. Real caller cancellation keeps precedence in check().
        check()
        if isinstance(error, (httpx.RequestError, httpx.StreamError)):
            raise ModelFailureError(
                "Chat completion stream read failed",
                reason=(ModelFailureReason.MODEL_TIMEOUT if isinstance(error, httpx.TimeoutException)
                        else ModelFailureReason.MODEL_CONNECTION_ERROR),
            ) from error
        raise
    finally:
        if first_sse_deadline is not None:
            first_sse_deadline.close()
        unregister_startup()
        unregister()
        record_model_event("chat_transport_end", {
            "finished_monotonic": monotonic(), "terminal": terminal,
            "text_deltas": text_sequence, "reasoning_deltas": reasoning_sequence,
            "first_text_ms": first_text_ms,
        })
    if not received_choice or not terminal:
        raise ModelFailureError(
            "Chat completion stream ended without a terminal choice",
            reason=ModelFailureReason.MODEL_CONNECTION_ERROR,
        )


def _cancel_chat_stream(stream: Any) -> None:
    """Wake a blocked HTTP/1 read before closing the response and its client.

    Closing a socket from another thread alone may leave recv blocked. HTTP/2
    connections can be multiplexed, so never shut down their shared socket.
    """
    try:
        response = getattr(stream, "response", None)
        extensions = getattr(response, "extensions", {})
        if (not getattr(response, "is_closed", False)
                and extensions.get("http_version") in {b"HTTP/1.0", b"HTTP/1.1"}):
            network = extensions.get("network_stream")
            sock = network.get_extra_info("socket") if network is not None else None
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        # SDK/test transports may not expose a socket, or it may already be closed.
        pass
    finally:
        stream.close()
