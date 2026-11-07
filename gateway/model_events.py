"""Provider-neutral observations from a model invocation, independent of its consumer."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """A text increment received from a model; durations use a monotonic clock."""

    text: str
    call_id: str
    sequence: int
    received_at: float
    elapsed_ms: float
    first_text_ms: float | None
    before_completion: bool


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    """A reasoning increment received from a model; kept separate from visible text."""

    text: str
    call_id: str
    sequence: int
    received_at: float


_text_observer: ContextVar[Callable[[ModelTextDelta], None] | None] = ContextVar(
    "model_text_observer", default=None,
)
_reasoning_observer: ContextVar[Callable[[ModelReasoningDelta], None] | None] = ContextVar(
    "model_reasoning_observer", default=None,
)


@contextmanager
def observe_model_text(observer: Callable[[ModelTextDelta], None] | None) -> Iterator[None]:
    """Subscribe in the current invocation scope; None suppresses a nested scope."""
    token = _text_observer.set(observer)
    try:
        yield
    finally:
        _text_observer.reset(token)


@contextmanager
def observe_model_reasoning(observer: Callable[[ModelReasoningDelta], None] | None) -> Iterator[None]:
    """Subscribe in the current invocation scope; None suppresses a nested scope."""
    token = _reasoning_observer.set(observer)
    try:
        yield
    finally:
        _reasoning_observer.reset(token)


def emit_model_text(delta: ModelTextDelta) -> None:
    """Deliver an observation without selecting a business consumer or transport."""
    observer = _text_observer.get()
    if observer is not None:
        observer(delta)


def emit_model_reasoning(delta: ModelReasoningDelta) -> None:
    """Deliver an observation without selecting a business consumer or transport."""
    observer = _reasoning_observer.get()
    if observer is not None:
        observer(delta)


_retry_observer: ContextVar[Callable[[str, int, int], None] | None] = ContextVar(
    "model_retry_observer", default=None,
)


@contextmanager
def observe_model_retry(observer: Callable[[str, int, int], None]) -> Iterator[None]:
    token = _retry_observer.set(observer)
    try:
        yield
    finally:
        _retry_observer.reset(token)


def emit_model_retry(call_id: str, attempt: int, maximum: int) -> None:
    observer = _retry_observer.get()
    if observer is not None:
        observer(call_id, attempt, maximum)
