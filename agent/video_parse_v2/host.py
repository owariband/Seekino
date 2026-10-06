"""Host dependency contract for the Video Parse V2 harness.

The harness owns orchestration and pure Engine validation.  The application
implements this protocol with repositories, queues, media workspaces, Gateway
models, indexing wakeups, leases, and transactional commits.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from langchain_core.tools import BaseTool

from engine.parsev2.planning import ChunkPlan, IntervalPlan
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    IntervalRefinementProposal,
    PrefixRefinementProposal,
    ValidatedIntervalRefinement,
    ValidatedPrefixRefinement,
)

from .input import ParseInput

if TYPE_CHECKING:
    from .prefix.context import PrefixContext
    from .refine.input import RefineInput


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
    dispatchable: bool = False


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


class VideoParseRuntime(Protocol):
    """Operations implemented by the Server/Gateway host.

    The Video coordinator keeps its lease through Prepare, Chunk scheduling,
    completion waiting, and final aggregation; the host heartbeats that lease
    while child workers run concurrently. Methods that write content must
    enforce the current lease/fence. The methods accepting validated Engine
    results own their complete DB transaction; no model or media request may run
    while those rows are locked.
    """

    async def load_video_run(self, video_parse_task_id: str) -> VideoRunSnapshot: ...

    async def ensure_video_chunks(
        self,
        video_parse_task_id: str,
        lease: LeaseToken,
        plans: Sequence[ChunkPlan],
    ) -> tuple[VideoChunk, ...]: ...

    async def ensure_parse_tasks(
        self,
        video_parse_task_id: str,
        lease: LeaseToken,
        chunks: Sequence[VideoChunk],
        intervals: Sequence[IntervalPlan],
    ) -> None: ...

    async def activate_video_task(
        self, video_parse_task_id: str, lease: LeaseToken
    ) -> None: ...

    async def load_chunk_schedule(
        self, video_parse_task_id: str
    ) -> ChunkScheduleSnapshot: ...

    async def dispatch_chunk(
        self, chunk_parse_task_id: str, lease: LeaseToken
    ) -> bool: ...

    async def wait_for_video_change(
        self, video_parse_task_id: str, *, timeout_seconds: float
    ) -> None: ...

    async def advance_video_task(
        self, video_parse_task_id: str, lease: LeaseToken
    ) -> VideoTaskStatus: ...

    async def load_chunk_execution(
        self, chunk_parse_task_id: str
    ) -> ChunkExecution: ...

    async def prepare_chunk_visual(self, execution: ChunkExecution) -> None: ...

    async def transcribe_chunk(self, execution: ChunkExecution) -> object: ...

    async def read_chunk_text(self, execution: ChunkExecution) -> object: ...

    async def understand_chunk(
        self,
        execution: ChunkExecution,
        *,
        asr_result: object,
        ocr_result: object | None,
        prompt: str,
    ) -> VideoParseResult: ...

    def build_chunk_without_understanding(
        self,
        execution: ChunkExecution,
        *,
        asr_result: object,
        ocr_result: object | None,
    ) -> VideoParseResult: ...

    async def commit_chunk_result(
        self,
        execution: ChunkExecution,
        result: VideoParseResult,
        update_interval_mask: Callable[[str], None],
    ) -> None: ...

    async def notify_chunk_committed(self, execution: ChunkExecution) -> None: ...

    async def cleanup_chunk_execution(self, execution: ChunkExecution) -> None: ...

    def update_interval_mask(self, chunk_parse_task_id: str) -> None: ...

    async def interval_is_ready(self, interval_parse_task_id: str) -> bool: ...

    async def enqueue_interval_refine(self, interval_parse_task_id: str) -> None: ...

    async def next_prefix_chunk(self, series_id: str) -> str | None: ...

    async def enqueue_prefix_context(self, chunk_parse_task_id: str) -> None: ...

    async def load_refine_input(self, interval_parse_task_id: str) -> RefineInput: ...

    async def generate_interval_refinement(
        self,
        snapshot: RefineInput,
        *,
        prompt: str,
    ) -> IntervalRefinementProposal: ...

    async def commit_interval_refinement(
        self,
        snapshot: RefineInput,
        result: ValidatedIntervalRefinement,
    ) -> None:
        """Apply explicit fields to locked current rows; union current DB scopes.

        The Interval lease and unsubmitted state are fenced. Member versions
        describe the model input but do not force a rerun merely because another
        Interval committed meanwhile. Omitted fields retain their current value.
        """

        ...

    async def notify_interval_committed(self, snapshot: RefineInput) -> None: ...

    async def load_prefix_context(self, chunk_parse_task_id: str) -> PrefixContext: ...

    async def generate_prefix_refinement(
        self,
        context: PrefixContext,
        *,
        prompt: str,
        tools: Sequence[BaseTool],
    ) -> PrefixRefinementProposal: ...

    async def commit_prefix_refinement(
        self,
        context: PrefixContext,
        result: ValidatedPrefixRefinement,
    ) -> None:
        """Fence the Prefix lease and target content version before advancing."""

        ...

    async def notify_prefix_committed(self, context: PrefixContext) -> None: ...


class MissingVideoParseRuntime:
    """Default that fails at the first external boundary instead of faking work."""

    def __getattr__(self, operation: str):  # type: ignore[no-untyped-def]
        def missing(*args: object, **kwargs: object) -> None:
            raise NotImplementedError(
                f"video_parse_v2 runtime operation {operation!r} is not configured"
            )

        return missing
