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


_text_observer: ContextVar[Callable[[ModelTextDelta], None] | None] = ContextVar(
    "model_text_observer", default=None,
)


@contextmanager
def observe_model_text(observer: Callable[[ModelTextDelta], None] | None) -> Iterator[None]:
    """Subscribe in the current invocation scope; None suppresses a nested scope."""
    token = _text_observer.set(observer)
    try:
        yield
    finally:
        _text_observer.reset(token)


def emit_model_text(delta: ModelTextDelta) -> None:
    """Deliver an observation without selecting a business consumer or transport."""
    observer = _text_observer.get()
    if observer is not None:
        observer(delta)
