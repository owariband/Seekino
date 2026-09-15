from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    text_input_tokens: int | None = None
    image_input_tokens: int | None = None
    audio_input_tokens: int | None = None
    cached_audio_input_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            optional = name not in {"input_tokens", "output_tokens", "total_tokens"}
            if value is None and optional:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output tokens")
        for name in ("cached_input_tokens", "text_input_tokens", "image_input_tokens", "audio_input_tokens", "cached_audio_input_tokens"):
            value = getattr(self, name)
            if value is not None and value > self.input_tokens:
                raise ValueError(f"{name} must be contained in input tokens")
        if self.reasoning_tokens is not None and self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens must be contained in output tokens")
        if self.cached_audio_input_tokens is not None:
            for bound in (self.audio_input_tokens, self.cached_input_tokens):
                if bound is not None and self.cached_audio_input_tokens > bound:
                    raise ValueError("cached audio tokens must be contained in audio/cached input tokens")


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    call_id: str
    provider: str
    model: str
    operation: str
    started_at: datetime
    request_id: str | None
    usage: TokenUsage | None
    status: Literal["completed", "failed"]


_observer: ContextVar[Callable[[ModelCallUsage], None] | None] = ContextVar("model_usage_observer", default=None)


@contextmanager
def capture_model_usage(observer: Callable[[ModelCallUsage], None]) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _normalize_usage(value: Any, *, embedding: bool) -> TokenUsage | None:
    if value is None:
        return None
    input_tokens = _field(value, "input_tokens", _field(value, "prompt_tokens"))
    output_tokens = _field(value, "output_tokens", _field(value, "completion_tokens", 0 if embedding else None))
    total_tokens = _field(value, "total_tokens")
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    input_details = _field(value, "input_tokens_details") or _field(value, "prompt_tokens_details")
    output_details = _field(value, "output_tokens_details") or _field(value, "completion_tokens_details")
    try:
        return TokenUsage(
            input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
            cached_input_tokens=_field(input_details, "cached_tokens"),
            reasoning_tokens=_field(output_details, "reasoning_tokens"),
            text_input_tokens=_field(input_details, "text_tokens"),
            image_input_tokens=_field(input_details, "image_tokens"),
            audio_input_tokens=_field(input_details, "audio_tokens"),
            cached_audio_input_tokens=_field(input_details, "audio_cached_tokens"),
        )
    except ValueError:
        _logger.warning("Model returned invalid token usage metadata")
        return None


class ModelCallTracker:
    def __init__(self, *, provider: str, model: str, operation: str) -> None:
        self.call_id = str(uuid4())
        self.provider = provider
        self.model = model
        self.operation = operation
        self.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.request_id: str | None = None
        self.usage: TokenUsage | None = None

    def capture_response(self, response: Any, *, include_usage: bool = True) -> None:
        headers = _field(response, "headers", {}) or {}
        request_id = (
            _field(response, "_request_id") or _field(response, "request_id")
            or headers.get("x-client-request-id") or headers.get("x-request-id")
        )
        if isinstance(request_id, str) and request_id:
            self.request_id = request_id
        model = _field(response, "model")
        if isinstance(model, str) and model:
            self.model = model
        if include_usage:
            self.usage = _normalize_usage(_field(response, "usage"), embedding=self.operation in {"text_embedding", "video_embedding"})


@contextmanager
def track_model_call(*, provider: str, model: str, operation: str) -> Iterator[ModelCallTracker]:
    tracker = ModelCallTracker(provider=provider, model=model, operation=operation)
    observer = _observer.get()
    status: Literal["completed", "failed"] = "failed"
    try:
        yield tracker
        status = "completed"
    except BaseException as error:
        if tracker.request_id is None:
            request_id = getattr(error, "request_id", None)
            if isinstance(request_id, str):
                tracker.request_id = request_id
        raise
    finally:
        if observer is not None:
            try:
                observer(ModelCallUsage(
                    call_id=tracker.call_id, provider=tracker.provider, model=tracker.model,
                    operation=tracker.operation, started_at=tracker.started_at,
                    request_id=tracker.request_id, usage=tracker.usage, status=status,
                ))
            except Exception:
                _logger.warning("Model usage observer failed; model result is preserved")
