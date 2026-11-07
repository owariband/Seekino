"""Persist best-effort Parse model text drafts without affecting formal calls."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any

from gateway.model_events import ModelTextDelta, observe_model_text
from gateway.usage import (
    ModelCallTracker,
    ModelCallUsage,
    capture_model_usage,
)

from ..model.dto.parseOutputDraftDTO import (
    DRAFT_METADATA_FIELDS,
    DraftStatus,
    ParseOutputDraftState,
)
from ..repository.parse_output_draft import ParseOutputDraftRepository
from ..utils.time import unix_time_ms_to_datetime

_logger = logging.getLogger(__name__)

DRAFT_SAVE_BYTES = 1_024
_METADATA_CONTEXT_FIELDS = DRAFT_METADATA_FIELDS - {"provider", "model", "operation"}


@dataclass(slots=True)
class _DraftBuffer:
    call_id: str
    tenant_id: str
    video_task_id: str
    actor_user_id: str | None
    metadata: dict[str, Any]
    started_at: datetime
    parts: list[str] = field(default_factory=list)
    text_bytes: int = 0
    pending_bytes: int = 0
    middle_failed: bool = False
    finished: bool = False


class ParseOutputDraftService:
    """Track one buffer per call and save cumulative UTF-8 text by size."""

    def __init__(self, repository: ParseOutputDraftRepository) -> None:
        self._repository = repository
        self._lock = Lock()
        self._calls: dict[str, _DraftBuffer] = {}
        self._owner: ContextVar[tuple[str, str, str | None] | None] = ContextVar(
            "parse_draft_owner", default=None,
        )

    @contextmanager
    def capture(
        self, *, tenant_id: str, video_task_id: str, actor_user_id: str | None,
    ) -> Iterator[None]:
        token = self._owner.set((tenant_id, video_task_id, actor_user_id))
        try:
            with capture_model_usage(self, combine=True), observe_model_text(self._observe_text):
                yield
        finally:
            self._owner.reset(token)

    def __call__(self, call: ModelCallUsage) -> None:
        # A failed start hook must not create a second observation at the wrong time.
        pass

    def on_start(self, tracker: ModelCallTracker) -> Callable[[ModelCallUsage], None]:
        context = tracker.context
        owner = self._owner.get()
        tenant_id = _context_text(context.get("tenant_id"), 64)
        video_task_id = _context_text(context.get("video_parse_task_id"), 36)
        if (
            context.get("category") != "parse"
            or context.get("task_type") != "video_parse"
            or context.get("run_id") != video_task_id
            or context.get("assistant_message_id") is not None
            or tenant_id is None
            or video_task_id is None
            or owner != (tenant_id, video_task_id, context.get("actor_user_id"))
        ):
            return _discard
        actor_user_id = _context_text(context.get("actor_user_id"), 64)
        buffer = _DraftBuffer(
            call_id=tracker.call_id, tenant_id=tenant_id,
            video_task_id=video_task_id, actor_user_id=actor_user_id,
            metadata=_draft_metadata(tracker),
            started_at=unix_time_ms_to_datetime(tracker.started_at),
        )
        with self._lock:
            if tracker.call_id in self._calls:
                return _discard
            self._calls[tracker.call_id] = buffer

        def finish(call: ModelCallUsage) -> None:
            self._finish(tracker, buffer, call)

        return finish

    def _observe_text(self, delta: ModelTextDelta) -> None:
        if not isinstance(delta.text, str) or not delta.text:
            return
        encoded_bytes = len(delta.text.encode("utf-8"))
        if not encoded_bytes:
            return
        with self._lock:
            buffer = self._calls.get(delta.call_id)
            if buffer is None or buffer.finished:
                return
            buffer.parts.append(delta.text)
            buffer.text_bytes += encoded_bytes
            buffer.pending_bytes += encoded_bytes
            if buffer.middle_failed or buffer.pending_bytes < DRAFT_SAVE_BYTES:
                return
            text = "".join(buffer.parts)
            text_bytes = buffer.text_bytes
            buffer.pending_bytes = 0
        if not self._attempt_save(buffer, text, text_bytes, "streaming"):
            with self._lock:
                buffer.middle_failed = True

    def _finish(
        self, tracker: ModelCallTracker, buffer: _DraftBuffer, call: ModelCallUsage,
    ) -> None:
        with self._lock:
            if buffer.finished:
                return
            buffer.finished = True
            if self._calls.get(buffer.call_id) is buffer:
                self._calls.pop(buffer.call_id, None)
        if buffer.text_bytes == 0:
            return
        self._attempt_save(
            buffer, "".join(buffer.parts), buffer.text_bytes,
            _terminal_status(tracker, call),
        )

    def _attempt_save(
        self, buffer: _DraftBuffer, text: str, text_bytes: int, status: DraftStatus,
    ) -> bool:
        try:
            self._repository.save(ParseOutputDraftState(
                call_id=buffer.call_id, tenant_id=buffer.tenant_id,
                video_task_id=buffer.video_task_id,
                actor_user_id=buffer.actor_user_id,
                text=text, text_bytes=text_bytes, status=status,
                metadata=buffer.metadata, started_at=buffer.started_at,
            ))
            return True
        except Exception as error:
            # Auxiliary drafts never replace the model result; do not log text.
            _logger.warning(
                "Parse output draft save failed call_id=%s tenant_id=%s "
                "video_parse_task_id=%s status=%s error=%s",
                buffer.call_id, buffer.tenant_id, buffer.video_task_id,
                status, type(error).__name__,
            )
            return False


def _context_text(value: Any, maximum: int) -> str | None:
    if isinstance(value, str) and value and value == value.strip() and len(value) <= maximum:
        return value
    return None


def _draft_metadata(tracker: ModelCallTracker) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": tracker.provider,
        "model": tracker.model,
        "operation": tracker.operation,
    }
    for name in _METADATA_CONTEXT_FIELDS:
        value = tracker.context.get(name)
        if value is not None and isinstance(value, (str, bool, int)):
            metadata[name] = value
    return metadata


def _terminal_status(
    tracker: ModelCallTracker, call: ModelCallUsage,
) -> DraftStatus:
    if call.status == "completed":
        return "completed"
    return "cancelled" if tracker.cancelled else "failed"


def _discard(_call: ModelCallUsage) -> None:
    pass
