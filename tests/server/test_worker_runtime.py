from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from server.database import create_session_factory
from server.model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from server.model.video import SeriesRecord, VideoRecord
from server.repository.job import ParseJobLease, ParseJobRepository
from server.mq.worker import ParseWorker


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class _Processor:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.leases: list[ParseJobLease] = []

    def process(self, lease: ParseJobLease) -> list[str]:
        self.leases.append(lease)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _seed(repository, *, max_attempts: int = 3):
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    series_id = str(uuid4())
    video_id = str(uuid4())
    job_id = str(uuid4())
    now = datetime(2026, 9, 14, 10, 0, 0)
    with session_factory.begin() as session:
        session.add(
            SeriesRecord(
                series_id=series_id,
                workspace_id="local-default",
                name=f"Worker Series {series_id}",
                description=None,
                status="INDEXING",
                important_extra_data=None,
                character_bible=[],
            )
        )
        session.add(
            VideoRecord(
                video_id=video_id,
                series_id=series_id,
                episode_order=1,
                upload_sequence=1,
                original_filename="episode.mp4",
                original_source_uri="artifact://test/media/episode.mp4",
                inference_proxy_uri=None,
                content_hash="a" * 64,
                total_bytes=10,
                duration_ms=20_000,
                upload_status="COMPLETED",
                parse_status="QUEUED",
                upload_progress=100,
                parse_progress=0,
            )
        )
        session.add(
            SeriesParseJobRecord(
                job_id=job_id,
                series_id=series_id,
                video_id=video_id,
                pipeline_version="pipeline-v1",
                status=ParseJobStatus.QUEUED.value,
                stage=ParseJobStage.QUEUED.value,
                progress=0,
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=now,
                lease_version=0,
            )
        )
    return ParseJobRepository(session_factory), job_id, now


def _worker(jobs, processor, clock, logs):
    return ParseWorker(
        jobs,
        processor,
        worker_id="worker-a",
        lease_duration=timedelta(minutes=2),
        heartbeat_interval=timedelta(minutes=1),
        retry_base=timedelta(seconds=10),
        retry_max=timedelta(minutes=1),
        poll_interval_seconds=0.01,
        clock=clock,
        on_log=logs.append,
    )


def test_worker_claims_and_completes_one_job(repository) -> None:
    jobs, job_id, now = _seed(repository)
    clock = _Clock(now)
    processor = _Processor([[str(uuid4()), str(uuid4())]])
    logs: list[str] = []

    worked = _worker(jobs, processor, clock, logs).run_once()

    assert worked
    assert jobs.get(job_id).status == ParseJobStatus.SUCCEEDED.value
    assert len(processor.leases) == 1
    assert any("completed job=" in item for item in logs)


def test_worker_retries_with_a_new_lease_version(repository) -> None:
    jobs, job_id, now = _seed(repository)
    clock = _Clock(now)
    processor = _Processor([RuntimeError("provider down"), [str(uuid4())]])
    logs: list[str] = []
    worker = _worker(jobs, processor, clock, logs)

    assert worker.run_once()
    waiting = jobs.get(job_id)
    assert waiting.status == ParseJobStatus.RETRY_WAIT.value
    assert waiting.available_at == now + timedelta(seconds=10)

    clock.now = waiting.available_at
    assert worker.run_once()
    completed = jobs.get(job_id)
    assert completed.status == ParseJobStatus.SUCCEEDED.value
    assert [item.lease_version for item in processor.leases] == [1, 2]


def test_worker_stops_retrying_after_max_attempts(repository) -> None:
    jobs, job_id, now = _seed(repository, max_attempts=1)
    clock = _Clock(now)
    processor = _Processor([RuntimeError("permanent failure")])
    worker = _worker(jobs, processor, clock, [])

    assert worker.run_once()

    state = jobs.get(job_id)
    assert state.status == ParseJobStatus.FAILED.value
    assert "permanent failure" in (state.last_error or "")
    assert not worker.run_once()
