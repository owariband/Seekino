"""Collect Chat Completions SSE before exposing a result or executing tools."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from time import monotonic
from typing import Any

import httpx
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.model_diagnostics import record_model_event
from gateway.usage import ModelCallTracker


def request_chat_completion(
    completions: Any, *, stream: bool, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None, **request: Any,
) -> ChatCompletion:
    """Execute the model's configured transport and return a complete turn."""
    if not isinstance(stream, bool):
        raise TypeError("stream must be a boolean")
    request["stream"] = stream
    usage_call.raise_if_cancelled()
    if check_control is not None:
        check_control()
    if stream:
        request["stream_options"] = {**request.get("stream_options", {}), "include_usage": True}
        return request_chat_stream(completions, usage_call=usage_call, check_control=check_control, **request)
    request.pop("stream_options", None)
    response = completions.create(**request)
    usage_call.capture_response(response)
    usage_call.raise_if_cancelled()
    if check_control is not None:
        check_control()
    return response


def request_chat_stream(
    completions: Any, *, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None, **request: Any,
) -> ChatCompletion:
    """Collect a verified turn with SDK delta assembly before executing tools."""
    state = ChatCompletionStreamState()
    usage = None
    with closing(_iter_chat_chunks(completions, usage_call=usage_call, check_control=check_control, **request)) as chunks:
        for chunk in chunks:
            if chunk.usage is not None:
                usage = chunk.usage
            try:
                state.handle_chunk(chunk)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
                raise ModelFailureError("Chat completion stream returned an invalid delta") from error
    response = state.current_completion_snapshot
    response.usage = usage
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
    check_control: Callable[[], None] | None = None, **request: Any,
) -> Iterator[Any]:
    # The SDK decodes SSE. We additionally reject EOF without a terminal choice.
    deadline = monotonic() + request["timeout"]

    def check() -> None:
        usage_call.raise_if_cancelled()
        if check_control is not None:
            check_control()
        if monotonic() >= deadline:
            raise ModelFailureError(
                "Chat completion stream exceeded its time budget", reason=ModelFailureReason.MODEL_TIMEOUT,
            )

    check()
    stream = completions.create(**request)
    usage_call.capture_response(stream.response, include_usage=False)
    unregister = usage_call.add_cancel_callback(stream.close)
    received_choice = False
    terminal = False
    try:
        with stream:
            for chunk in stream:
                check()
                if not isinstance(chunk, ChatCompletionChunk):
                    try:
                        chunk = ChatCompletionChunk.model_validate(chunk.model_dump())
                    except (AttributeError, TypeError, ValueError) as error:
                        raise ModelFailureError("Chat completion stream returned an invalid delta") from error
                usage_call.capture_response(chunk, include_usage=chunk.usage is not None)
                record_model_event("sse", chunk)
                for choice in chunk.choices:
                    # Our requests ask for one completion. Reject ambiguous or
                    # late deltas instead of silently consuming a partial turn.
                    if choice.index != 0 or terminal:
                        raise ModelFailureError("Chat completion stream returned an unexpected choice")
                    received_choice = True
                    terminal = choice.finish_reason is not None
                yield chunk
            check()
    except (httpx.RequestError, httpx.StreamError) as error:
        check()
        raise ModelFailureError(
            "Chat completion stream read failed",
            reason=(ModelFailureReason.MODEL_TIMEOUT if isinstance(error, httpx.TimeoutException)
                    else ModelFailureReason.MODEL_CONNECTION_ERROR),
        ) from error
    finally:
        unregister()
    if not received_choice or not terminal:
        raise ModelFailureError(
            "Chat completion stream ended without a terminal choice",
            reason=ModelFailureReason.MODEL_CONNECTION_ERROR,
        )
