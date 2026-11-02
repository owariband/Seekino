"""Coalesce validated Chat snapshots before persistence and subscriber delivery."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy


class ChatSnapshotCoalescer:
    """Keep the latest cumulative body; the caller serializes events and flushes boundaries."""

    def __init__(self, clock: Callable[[], float], *, interval_seconds: float = 0.05) -> None:
        self._clock = clock
        self._interval = interval_seconds
        self._call_id: str | None = None
        self._published_at: float | None = None
        self._pending: dict | None = None

    def offer(self, event: dict) -> dict | None:
        source = event.get("textStream")
        if not source:
            # A business snapshot already includes the most recent validated body.
            self._pending = None
            return event
        now = self._clock()
        if (source["callId"] == self._call_id and self._published_at is not None
                and now - self._published_at < self._interval):
            self._pending = deepcopy(event)
            return None
        self._call_id, self._published_at = source["callId"], now
        self._pending = None
        return event

    def take_pending(self) -> dict | None:
        event, self._pending = self._pending, None
        if event is not None:
            # Boundary/error flushes must not claim a live provider arrival.
            event.pop("textStream", None)
        return event
