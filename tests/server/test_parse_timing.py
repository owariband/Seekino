from datetime import timedelta
from uuid import uuid4

import pytest

from server.database import create_session_factory
from server.model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from server.model.job_timing import ParseJobAttemptOutcome
from server.model.job_timing import VideoChunkTimingStage, VideoChunkTimingStatus
from server.model.job_timing import ParseJobAttemptRecord
from server.model.video import SeriesRecord, VideoRecord
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.job_timing import ParseJobTimingRepository


def _seed_queue(repository):
    engine, _videos = repository
    factory = create_session_factory(engine)
    series_id, video_id, job_id = (str(uuid4()) for _ in range(3))
    now = 1_790_000_000_000
    with factory.begin() as session:
        session.add(SeriesRecord(
            series_id=series_id, workspace_id="local-default", name="Timing",
            description=None, status="INDEXING", important_extra_data=None,
            character_bible=[], created_at=now, updated_at=now,
        ))
        session.add(VideoRecord(
            video_id=video_id, series_id=series_id, episode_order=1,
            upload_sequence=1, original_filename="timing.mp4",
            original_source_uri="artifact://test/timing.mp4", inference_proxy_uri=None,
            content_hash="a" * 64, total_bytes=1, duration_ms=1_000,
            upload_status="COMPLETED", parse_status="QUEUED", upload_progress=100,
            parse_progress=0, created_at=now, updated_at=now,
        ))
        session.add(SeriesParseJobRecord(
            job_id=job_id, series_id=series_id, video_id=video_id,
            pipeline_version="test", status=ParseJobStatus.QUEUED.value,
            stage=ParseJobStage.QUEUED.value, progress=0, attempt_count=0,
            max_attempts=2, available_at=now, lease_version=0,
            created_at=now, updated_at=now,
        ))
    return MySqlParseJobQueue(factory), ParseJobTimingRepository(factory), now


def test_attempt_timing_is_closed_with_measured_processor_duration(repository) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=1), now=now)
    assert lease is not None
    running = timing.list_attempts(lease.job_id)
    assert [(item.outcome, item.duration_ms) for item in running] == [
        (ParseJobAttemptOutcome.RUNNING.value, None)
    ]

    assert queue.acknowledge(lease, duration_ms=321, now=now + 500)
    assert timing.totals(lease.job_id) == (321, True)
    finished = timing.list_attempts(lease.job_id)
    assert [(item.outcome, item.duration_ms, item.timing_complete) for item in finished] == [
        (ParseJobAttemptOutcome.SUCCEEDED.value, 321, True)
    ]


def test_expired_attempt_is_incomplete_and_does_not_invent_duration(repository) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(seconds=1), now=now)
    assert lease is not None
    assert queue.reap_expired(retry_delay=timedelta(), now=now + 1_001) == 1
    attempt = timing.list_attempts(lease.job_id)[0]
    assert attempt.outcome == ParseJobAttemptOutcome.LEASE_EXPIRED.value
    assert attempt.duration_ms is None
    assert timing.totals(lease.job_id) == (0, False)


def test_expired_attempt_closes_running_chunk_stage_and_rejects_late_finish(
    repository,
) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(seconds=1), now=now)
    assert lease is not None
    timing.start_chunk_stage(
        lease,
        sequence_no=0,
        video_chunk_id="chunk-0",
        stage=VideoChunkTimingStage.PARSE,
        started_at=now,
    )

    assert queue.reap_expired(retry_delay=timedelta(), now=now + 1_001) == 1

    [stage] = timing.list_chunk_stages(lease.job_id)
    assert stage.status == VideoChunkTimingStatus.LEASE_EXPIRED.value
    assert stage.completed_at == now + 1_001
    assert stage.duration_ms is None
    try:
        timing.finish_chunk_stage(
            lease,
            sequence_no=0,
            stage=VideoChunkTimingStage.PARSE,
            completed_at=now + 1_002,
            duration_ms=1_002,
            status=VideoChunkTimingStatus.SUCCEEDED,
        )
    except RuntimeError as exc:
        assert "no longer running" in str(exc)
    else:  # pragma: no cover - guards the lease fencing invariant
        raise AssertionError("an expired worker must not finish a stage")


def test_reaper_tolerates_a_legacy_running_job_without_attempt_timing(
    repository,
) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(seconds=1), now=now)
    assert lease is not None
    engine, _videos = repository
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.delete(
            session.get(
                ParseJobAttemptRecord,
                (lease.job_id, lease.attempt_count, lease.lease_version),
            )
        )

    assert queue.reap_expired(retry_delay=timedelta(), now=now + 1_001) == 1
    assert timing.totals(lease.job_id) == (0, False)


def test_chunk_stage_timings_keep_each_stage_and_segment_separate(repository) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=1), now=now)
    assert lease is not None

    # ASR may run ahead of sequential parsing.  Its row must be keyed by both
    # sequence and stage, rather than overwriting SPLIT/PARSE for the segment.
    for sequence_no, stage, duration_ms in (
        (0, VideoChunkTimingStage.SPLIT, 10),
        (0, VideoChunkTimingStage.ASR, 11),
        (1, VideoChunkTimingStage.ASR, 12),
        (0, VideoChunkTimingStage.PARSE, 13),
    ):
        timing.start_chunk_stage(
            lease,
            sequence_no=sequence_no,
            video_chunk_id=f"chunk-{sequence_no}",
            stage=stage,
            started_at=now,
        )
        timing.finish_chunk_stage(
            lease,
            sequence_no=sequence_no,
            stage=stage,
            completed_at=now + duration_ms,
            duration_ms=duration_ms,
            status=VideoChunkTimingStatus.SUCCEEDED,
        )

    rows = sorted(
        timing.list_chunk_stages(lease.job_id),
        key=lambda row: (row.sequence_no, row.stage),
    )
    assert [(row.sequence_no, row.stage, row.duration_ms, row.status) for row in rows] == [
        (0, VideoChunkTimingStage.ASR.value, 11, "SUCCEEDED"),
        (0, VideoChunkTimingStage.PARSE.value, 13, "SUCCEEDED"),
        (0, VideoChunkTimingStage.SPLIT.value, 10, "SUCCEEDED"),
        (1, VideoChunkTimingStage.ASR.value, 12, "SUCCEEDED"),
    ]

    # Calling an already persisted stage again is rejected.  Recovery code
    # therefore has to skip it, rather than making a duplicate timing row.
    try:
        timing.start_chunk_stage(
            lease, sequence_no=0, video_chunk_id="chunk-0",
            stage=VideoChunkTimingStage.PARSE, started_at=now + 20,
        )
    except RuntimeError as exc:
        assert "already started" in str(exc)
    else:  # pragma: no cover - assertion makes the reuse guarantee explicit
        raise AssertionError("checkpoint-reused stage must not create a timing row")


def test_measured_chunk_stage_rejects_lease_expired_terminal_status(repository) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=1), now=now)
    assert lease is not None
    timing.start_chunk_stage(
        lease,
        sequence_no=0,
        video_chunk_id="chunk-0",
        stage=VideoChunkTimingStage.PARSE,
        started_at=now,
    )

    with pytest.raises(ValueError, match="SUCCEEDED or FAILED"):
        timing.finish_chunk_stage(
            lease,
            sequence_no=0,
            stage=VideoChunkTimingStage.PARSE,
            completed_at=now + 10,
            duration_ms=10,
            status=VideoChunkTimingStatus.LEASE_EXPIRED,
        )

    [stage] = timing.list_chunk_stages(lease.job_id)
    assert stage.status == VideoChunkTimingStatus.RUNNING.value
    assert stage.completed_at is None
    assert stage.duration_ms is None


def test_legacy_attempt_gap_keeps_cumulative_timing_incomplete(repository) -> None:
    queue, timing, now = _seed_queue(repository)
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=1), now=now)
    assert lease is not None
    engine, _videos = repository
    factory = create_session_factory(engine)
    with factory.begin() as session:
        # The first execution happened before timing existed.  The job later
        # ran attempt two, which has a trustworthy measured duration.
        job = session.get(SeriesParseJobRecord, lease.job_id)
        job.attempt_count = 2
        session.delete(session.get(ParseJobAttemptRecord, (
            lease.job_id, lease.attempt_count, lease.lease_version,
        )))
        session.add(ParseJobAttemptRecord(
            job_id=lease.job_id, attempt_count=2, lease_version=2,
            started_at=now, completed_at=now + 77, duration_ms=77,
            outcome=ParseJobAttemptOutcome.SUCCEEDED.value, timing_complete=True,
        ))

    assert timing.totals(lease.job_id) == (77, False)
