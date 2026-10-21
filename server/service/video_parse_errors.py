"""Errors reported by parse workflow services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class VideoParseRepositoryError(RuntimeError):
    pass


class VideoParseTaskNotFoundError(VideoParseRepositoryError):
    pass


class VideoParseConflictError(VideoParseRepositoryError):
    pass


class VideoParseSnapshotConflictError(VideoParseConflictError):
    """A refinement input changed; refresh facts without rerunning observations."""

    def __init__(self, message: str, *, reason: str = "snapshot_changed",
                 context: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.context = dict(context or {})
        super().__init__(message)


class VoiceprintRegistrationRejectedError(VideoParseConflictError):
    """A valid preparation can continue by choosing a different reference."""

    def __init__(self, code: str, message: str, retry_hint: str) -> None:
        self.code = code
        self.retry_hint = retry_hint
        super().__init__(message)


class VideoParseLeaseLostError(VideoParseRepositoryError):
    pass


class ModelCallBudgetExhaustedError(VideoParseRepositoryError):
    pass


class ModelCallLeaseConflictError(VideoParseRepositoryError):
    pass
