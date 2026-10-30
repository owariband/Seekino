"""Collect Chat Completions SSE before exposing a result or executing tools."""
from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

import httpx
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletion

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.model_diagnostics import record_model_event
from gateway.usage import ModelCallTracker


def request_chat_stream(
    completions: Any, *, usage_call: ModelCallTracker,
    check_control: Callable[[], None] | None = None, **request: Any,
) -> ChatCompletion:
    """Keep SDK delta assembly, but require a terminal choice and check controls.

    The SDK accumulator alone accepts EOF without a finish_reason. Never expose
    that snapshot to the business layer, even if its partial JSON happens to parse.
    """
    deadline = monotonic() + request["timeout"]

    def check() -> None:
        usage_call.raise_if_cancelled()
        if check_control is not None:
            check_control()
        if monotonic() >= deadline:
            raise ModelFailureError(
                "Vision stream exceeded its time budget", reason=ModelFailureReason.MODEL_TIMEOUT,
            )

    check()
    stream = completions.create(**request)
    usage_call.capture_response(stream.response, include_usage=False)
    unregister = usage_call.add_cancel_callback(stream.close)
    # Do not pass tools/schema to the accumulator: the existing caller validates
    # them after collection, including length/content_filter and tool contracts.
    state = ChatCompletionStreamState()
    received_choice = False
    terminal = False
    usage = None
    try:
        with stream:
            for chunk in stream:
                check()
                usage_call.capture_response(chunk, include_usage=chunk.usage is not None)
                record_model_event("sse", chunk)
                if chunk.usage is not None:
                    usage = chunk.usage
                for choice in chunk.choices:
                    # Our requests ask for one completion. Reject ambiguous or
                    # late deltas instead of silently consuming a partial turn.
                    if choice.index != 0 or terminal:
                        raise ModelFailureError("Vision stream returned an unexpected choice")
                    received_choice = True
                    terminal = choice.finish_reason is not None
                try:
                    state.handle_chunk(chunk)
                except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
                    raise ModelFailureError("Vision stream returned an invalid delta") from error
            check()
    except (httpx.RequestError, httpx.StreamError) as error:
        check()
        raise ModelFailureError(
            "Vision stream read failed",
            reason=(ModelFailureReason.MODEL_TIMEOUT if isinstance(error, httpx.TimeoutException)
                    else ModelFailureReason.MODEL_CONNECTION_ERROR),
        ) from error
    finally:
        unregister()
    if not received_choice or not terminal:
        raise ModelFailureError(
            "Vision stream ended without a terminal choice",
            reason=ModelFailureReason.MODEL_CONNECTION_ERROR,
        )
    response = state.current_completion_snapshot
    response.usage = usage
    return response
