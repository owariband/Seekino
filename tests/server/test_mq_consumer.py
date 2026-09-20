from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from server.database import create_session_factory
from server.model.dto.jobDTO import ParseJobLease
from server.model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from server.model.video import SeriesRecord, VideoRecord
from server.mq.consumer import ParseJobConsumer
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.job import ParseJobRepository
from server.utils.time import now_unix_time_ms, timedelta_to_ms


@dataclass
class _Clock:
    now: int

    def __call__(self) -> int:
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
    now = 1_789_376_400_000
    with session_factory.begin() as session:
        session.add(
            SeriesRecord(
                series_id=series_id,
                workspace_id="local-default",
                name=f"Consumer Series {series_id}",
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
    return (
        ParseJobRepository(session_factory),
        MySqlParseJobQueue(session_factory),
        job_id,
        now,
    )


def _consumer(queue, processor, clock, logs):
    return ParseJobConsumer(
        queue,
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


def test_consumer_claims_and_completes_one_job(repository) -> None:
    jobs, queue, job_id, now = _seed(repository)
    clock = _Clock(now)
    processor = _Processor([[str(uuid4()), str(uuid4())]])
    logs: list[str] = []

    worked = _consumer(queue, processor, clock, logs).run_once()

    assert worked
    assert jobs.get(job_id).status == ParseJobStatus.SUCCEEDED.value
    assert len(processor.leases) == 1
    assert any("completed job=" in item for item in logs)


def test_consumer_persists_monotonic_processing_duration(repository) -> None:
    jobs, queue, job_id, now = _seed(repository)
    readings = iter((1_000_000_000, 1_321_000_000))
    consumer = ParseJobConsumer(
        queue,
        _Processor([[str(uuid4())]]),
        worker_id="worker-a",
        lease_duration=timedelta(minutes=2),
        heartbeat_interval=timedelta(minutes=1),
        retry_base=timedelta(seconds=10),
        retry_max=timedelta(minutes=1),
        clock=_Clock(now),
        monotonic_clock=lambda: next(readings),
    )

    assert consumer.run_once()

    job = jobs.get(job_id)
    assert job.status == ParseJobStatus.SUCCEEDED.value
    assert job.processing_duration_ms == 321
    assert job.processing_timing_complete is True


def test_consumer_retries_with_a_new_lease_version(repository) -> None:
    jobs, queue, job_id, now = _seed(repository)
    clock = _Clock(now)
    processor = _Processor([RuntimeError("provider down"), [str(uuid4())]])
    consumer = _consumer(queue, processor, clock, [])

    assert consumer.run_once()
    waiting = jobs.get(job_id)
    assert waiting.status == ParseJobStatus.RETRY_WAIT.value
    assert waiting.available_at == now + timedelta_to_ms(timedelta(seconds=10))

    clock.now = waiting.available_at
    assert consumer.run_once()
    assert jobs.get(job_id).status == ParseJobStatus.SUCCEEDED.value
    assert [item.lease_version for item in processor.leases] == [1, 2]


def test_consumer_stops_retrying_after_max_attempts(repository) -> None:
    jobs, queue, job_id, now = _seed(repository, max_attempts=1)
    processor = _Processor([RuntimeError("permanent failure")])
    consumer = _consumer(queue, processor, _Clock(now), [])

    assert consumer.run_once()

    state = jobs.get(job_id)
    assert state.status == ParseJobStatus.FAILED.value
    assert "permanent failure" in (state.last_error or "")
    assert not consumer.run_once()


class _IdleQueue:
    def __init__(self) -> None:
        self.polled = threading.Event()

    def reap_expired(self, **_kwargs) -> int:
        return 0

    def claim_next(self, *_args, **_kwargs) -> None:
        self.polled.set()
        return None


def test_consumer_start_and_close_own_the_background_thread() -> None:
    queue = _IdleQueue()
    consumer = _consumer(queue, _Processor([]), _Clock(now_unix_time_ms()), [])

    consumer.start()
    assert queue.polled.wait(timeout=1)
    assert consumer.running

    consumer.close(timeout=1)

    assert not consumer.running
    assert consumer.failure is None


class _FailingQueue:
    def reap_expired(self, **_kwargs) -> int:
        raise RuntimeError("database unavailable")


def test_consumer_exposes_fatal_background_failure() -> None:
    logs: list[str] = []
    consumer = _consumer(
        _FailingQueue(),
        _Processor([]),
        _Clock(now_unix_time_ms()),
        logs,
    )

    consumer.start()
    consumer.join(timeout=1)

    assert not consumer.running
    assert isinstance(consumer.failure, RuntimeError)
    assert any("fatal error" in item for item in logs)


class _StaleQueue:
    def __init__(self, lease: ParseJobLease) -> None:
        self.lease = lease
        self.heartbeat_called = threading.Event()
        self.acknowledge_calls = 0
        self.retry_calls = 0

    def reap_expired(self, **_kwargs) -> int:
        return 0

    def claim_next(self, *_args, **_kwargs) -> ParseJobLease:
        return self.lease

    def heartbeat(self, *_args, **_kwargs) -> bool:
        self.heartbeat_called.set()
        return False

    def acknowledge(self, *_args, **_kwargs) -> bool:
        self.acknowledge_calls += 1
        return True

    def retry(self, *_args, **_kwargs) -> bool:
        self.retry_calls += 1
        return True


class _WaitForStaleHeartbeat:
    def __init__(self, heartbeat_called: threading.Event) -> None:
        self._heartbeat_called = heartbeat_called

    def process(self, _lease: ParseJobLease) -> list[str]:
        assert self._heartbeat_called.wait(timeout=1)
        return [str(uuid4())]


def test_consumer_discards_stale_result_without_acknowledge_or_retry(repository) -> None:
    _jobs, mysql_queue, _job_id, now = _seed(repository)
    lease = mysql_queue.claim_next(
        "worker-a", lease_duration=timedelta(minutes=2), now=now,
    )
    assert lease is not None
    queue = _StaleQueue(lease)
    logs: list[str] = []
    consumer = ParseJobConsumer(
        queue,
        _WaitForStaleHeartbeat(queue.heartbeat_called),
        worker_id="worker-a",
        lease_duration=timedelta(minutes=2),
        heartbeat_interval=timedelta(milliseconds=1),
        retry_base=timedelta(seconds=10),
        retry_max=timedelta(minutes=1),
        clock=_Clock(now),
        on_log=logs.append,
    )

    assert consumer.run_once()

    assert queue.acknowledge_calls == 0
    assert queue.retry_calls == 0
    assert any("abandoned stale job=" in item for item in logs)
