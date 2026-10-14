from __future__ import annotations

from typing import Any, NotRequired, TypedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from engine.parsev2.planning import ChunkPlan

from .input import ParseInput


class VideoTaskStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    COMPLETE = "COMPLETE"
    PARSING_FAILED = "PARSING_FAILED"
    INDEXING_FAILED = "INDEXING_FAILED"


class ChunkTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    UNDERSTANDING = "UNDERSTANDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """The exact claim fence held by this execution attempt."""

    owner: str
    version: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("lease owner must be nonblank")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("lease version must be a positive integer")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("lease expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VideoChunk:
    video_chunk_id: str
    sequence_no: int
    start_seconds: int
    end_seconds: int

    @property
    def plan(self) -> ChunkPlan:
        return ChunkPlan(
            sequence_no=self.sequence_no,
            start_seconds=self.start_seconds,
            end_seconds=self.end_seconds,
        )


@dataclass(frozen=True, slots=True)
class VideoRunSnapshot:
    video_parse_task_id: str
    status: VideoTaskStatus
    inputs: ParseInput
    duration_seconds: int
    lease: LeaseToken | None
    chunks: tuple[VideoChunk, ...] = ()


@dataclass(frozen=True, slots=True)
class ChunkScheduleEntry:
    chunk_parse_task_id: str
    sequence_no: int
    status: ChunkTaskStatus


@dataclass(frozen=True, slots=True)
class ChunkScheduleSnapshot:
    tasks: tuple[ChunkScheduleEntry, ...]
    wait_timeout_seconds: float
    terminal_error: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkExecution:
    chunk_parse_task_id: str
    video_parse_task_id: str
    sequence_no: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    allowed_start_seconds: int
    allowed_end_seconds: int
    inputs: ParseInput
    lease: LeaseToken
    background: dict[str, Any] = field(default_factory=dict)
    source_media_ref: str | None = None


class RunState(TypedDict):
    """主 Graph 的运行状态；持久化任务状态由 DB 维护。"""

    video_parse_task_id: str
    inputs: NotRequired[ParseInput]
    duration_seconds: NotRequired[int]
    status: NotRequired[VideoTaskStatus]
    lease: NotRequired[LeaseToken]
    video_chunks: NotRequired[tuple[VideoChunk, ...]]
    # 模型实例、DB 连接和临时媒体路径不进入 RunState。
