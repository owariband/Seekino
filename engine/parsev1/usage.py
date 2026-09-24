from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from gateway.usage import ModelCallUsage, capture_model_usage


@dataclass(frozen=True, slots=True)
class ParseModelCall:
    call: ModelCallUsage
    sequence_no: int | None
    category: Literal["parse", "embedding"]


_chunk_scope: ContextVar[tuple[int | None, Literal["parse", "embedding"]]] = ContextVar("parse_chunk_usage_scope", default=(None, "parse"))


@contextmanager
def capture_parse_usage(observer: Callable[[ParseModelCall], None]) -> Iterator[None]:
    def observe(call: ModelCallUsage) -> None:
        sequence_no, category = _chunk_scope.get()
        observer(ParseModelCall(call=call, sequence_no=sequence_no, category=category))

    token = _chunk_scope.set((None, "parse"))
    try:
        with capture_model_usage(observe):
            yield
    finally:
        _chunk_scope.reset(token)


@contextmanager
def parse_chunk_scope(sequence_no: int | None, *, category: Literal["parse", "embedding"] = "parse") -> Iterator[None]:
    if sequence_no is not None and (isinstance(sequence_no, bool) or not isinstance(sequence_no, int) or sequence_no < 0):
        raise ValueError("sequence_no must be a nonnegative integer")
    if category not in {"parse", "embedding"}:
        raise ValueError("category must be parse or embedding")
    token = _chunk_scope.set((sequence_no, category))
    try:
        yield
    finally:
        _chunk_scope.reset(token)
