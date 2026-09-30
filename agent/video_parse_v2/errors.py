"""Failures whose retry policy is known to the parse harness."""

from __future__ import annotations


class ParseError(RuntimeError):
    """A classified failure; the owning Worker persists its disposition.

    Use ``raise ParseError(..., retryable=...) from cause`` to retain the cause.
    Cancellation, lease loss and unexpected programming errors propagate as-is.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(message)
