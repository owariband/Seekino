from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic_ns

from ..identity import derive_video_chunk_id
from ..model.dto.jobDTO import ParseJobLease
from ..model.job_timing import VideoChunkTimingStage, VideoChunkTimingStatus
from ..repository.job_timing import ParseJobTimingRepository
from ..utils.time import now_unix_time_ms


class ParseChunkStageTimer:
    def __init__(
        self,
        repository: ParseJobTimingRepository,
        *,
        epoch_clock: Callable[[], int] = now_unix_time_ms,
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._repository = repository
        self._epoch_clock = epoch_clock
        self._monotonic_clock = monotonic_clock

    @contextmanager
    def measure(
        self,
        lease: ParseJobLease,
        *,
        sequence_no: int,
        stage: VideoChunkTimingStage,
    ) -> Iterator[None]:
        self._repository.start_chunk_stage(
            lease,
            sequence_no=sequence_no,
            video_chunk_id=derive_video_chunk_id(lease.video_id, sequence_no),
            stage=stage,
            started_at=self._epoch_clock(),
        )
        started_ns = self._monotonic_clock()
        try:
            yield
        except BaseException:
            self._finish(
                lease,
                sequence_no=sequence_no,
                stage=stage,
                started_ns=started_ns,
                status=VideoChunkTimingStatus.FAILED,
            )
            raise
        else:
            self._finish(
                lease,
                sequence_no=sequence_no,
                stage=stage,
                started_ns=started_ns,
                status=VideoChunkTimingStatus.SUCCEEDED,
            )

    def _finish(
        self,
        lease: ParseJobLease,
        *,
        sequence_no: int,
        stage: VideoChunkTimingStage,
        started_ns: int,
        status: VideoChunkTimingStatus,
    ) -> None:
        duration_ms = max(0, self._monotonic_clock() - started_ns) // 1_000_000
        self._repository.finish_chunk_stage(
            lease,
            sequence_no=sequence_no,
            stage=stage,
            completed_at=self._epoch_clock(),
            duration_ms=duration_ms,
            status=status,
        )
