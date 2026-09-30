from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any, Literal, Protocol
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
    started_at: int
    request_id: str | None
    usage: TokenUsage | None
    status: Literal["completed", "failed"]


class ModelCallControlError(RuntimeError):
    """A host admission, lease or cancellation rule stopped a provider call."""


class ModelCallCancelledError(ModelCallControlError):
    """The owner cancelled a provider call before its result became usable."""


class ModelCallControl(Protocol):
    """Admission and lifetime control supplied by the execution host."""

    def acquire(self, call: "ModelCallTracker") -> None: ...

    def release(self, call: "ModelCallTracker") -> None: ...


_observer: ContextVar[Callable[[ModelCallUsage], None] | None] = ContextVar("model_usage_observer", default=None)
_control: ContextVar[ModelCallControl | None] = ContextVar(
    "model_call_control", default=None
)


@contextmanager
def capture_model_usage(observer: Callable[[ModelCallUsage], None]) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def control_model_calls(control: ModelCallControl) -> Iterator[None]:
    """Apply host admission, cancellation and release to real provider calls.

    Provider adapters enter :func:`track_model_call` immediately before their
    SDK request.  Local validation performed before that point neither consumes
    a persistent call budget nor emits a usage record.
    """

    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


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
        self.started_at = time.time_ns() // 1_000_000
        self.request_id: str | None = None
        self.usage: TokenUsage | None = None
        self._cancelled = Event()
        self._cancel_lock = Lock()
        self._cancel_callbacks: dict[int, Callable[[], None]] = {}
        self._next_cancel_callback = 0

    def mark_request_started(self) -> None:
        """Timestamp the admitted provider attempt, excluding queue wait."""

        self.started_at = time.time_ns() // 1_000_000

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ModelCallCancelledError(
                f"model call {self.call_id} was cancelled"
            )

    def add_cancel_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a best-effort transport closer for this one request."""

        if not callable(callback):
            raise TypeError("cancel callback must be callable")
        with self._cancel_lock:
            if self._cancelled.is_set():
                callback_id = None
            else:
                callback_id = self._next_cancel_callback
                self._next_cancel_callback += 1
                self._cancel_callbacks[callback_id] = callback
        if callback_id is None:
            self._run_cancel_callback(callback)
            return lambda: None

        def unregister() -> None:
            with self._cancel_lock:
                self._cancel_callbacks.pop(callback_id, None)

        return unregister

    def cancel(self) -> None:
        """Reject a late result and ask registered transports to stop."""

        with self._cancel_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            callbacks = tuple(self._cancel_callbacks.values())
            self._cancel_callbacks.clear()
        for callback in callbacks:
            self._run_cancel_callback(callback)

    @staticmethod
    def _run_cancel_callback(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            _logger.warning("Model call transport cancellation failed")

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
    control = _control.get()
    admitted = False
    request_started = False
    status: Literal["completed", "failed"] = "failed"
    try:
        if control is not None:
            control.acquire(tracker)
            admitted = True
        tracker.raise_if_cancelled()
        tracker.mark_request_started()
        request_started = True
        yield tracker
        tracker.raise_if_cancelled()
        status = "completed"
    except BaseException as error:
        if tracker.request_id is None:
            request_id = getattr(error, "request_id", None)
            if isinstance(request_id, str):
                tracker.request_id = request_id
        if tracker.cancelled and not isinstance(error, ModelCallCancelledError):
            raise ModelCallCancelledError(
                f"model call {tracker.call_id} was cancelled"
            ) from error
        raise
    finally:
        if admitted and control is not None:
            try:
                control.release(tracker)
            except Exception:
                _logger.warning("Model call control release failed")
        if request_started and observer is not None:
            try:
                observer(ModelCallUsage(
                    call_id=tracker.call_id, provider=tracker.provider, model=tracker.model,
                    operation=tracker.operation, started_at=tracker.started_at,
                    request_id=tracker.request_id, usage=tracker.usage, status=status,
                ))
            except Exception:
                _logger.warning("Model usage observer failed; model result is preserved")
