from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from threading import Lock
from typing import Any, Protocol

from engine.rag.contracts import VideoChunkEvidence
from gateway.media import ToolImageArtifact


@dataclass(frozen=True, slots=True)
class FrameReadRequest:
    evidence: VideoChunkEvidence
    inspection_id: str
    role: str
    conversation_id: str
    message_id: str
    start_ms: int
    end_ms: int
    frame_count: int
    max_height: int
    timeout_seconds: float | None
    check_control: Callable[[], None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (type(self.start_ms) is not int or type(self.end_ms) is not int
                or not self.evidence.start_seconds <= self.start_ms < self.end_ms <= self.evidence.end_seconds):
            raise ValueError("Frame range must be inside the canonical Chunk in absolute milliseconds")
        if type(self.frame_count) is not int or not 1 <= self.frame_count <= 4:
            raise ValueError("frame_count must be an integer between 1 and 4")
        if self.frame_count > self.end_ms - self.start_ms:
            raise ValueError("Frame range is too short for distinct millisecond sampling positions")
        if type(self.max_height) is not int or self.max_height <= 0:
            raise ValueError("max_height must be a positive integer")

    def cache_key(self) -> tuple:
        evidence = self.evidence
        return (
            evidence.tenant_id, evidence.series_id, evidence.video_id,
            evidence.video_chunk_id, evidence.original_source_uri, evidence.content_hash,
            evidence.parsed_at, json.dumps(evidence.parse_provenance, sort_keys=True),
            self.start_ms, self.end_ms, self.frame_count, self.max_height,
        )


@dataclass(frozen=True, slots=True)
class FrameInspection:
    payload: dict[str, Any]
    artifact: ToolImageArtifact


class FrameReader(Protocol):
    def __call__(
        self, request: FrameReadRequest, cached: FrameInspection | None = None,
    ) -> FrameInspection: ...


@dataclass(slots=True)
class FrameInspectionSession:
    maximum_inspections: int | None
    used_inspections: int = 0
    cache: dict[tuple, FrameInspection] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    lock: Any = field(default_factory=Lock, repr=False)

    def summary(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(receipt) for receipt in self.receipts]


class FrameReadError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
