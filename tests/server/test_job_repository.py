from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from server.database import create_session_factory
from server.model.job import (
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJobRecord,
    VideoChunkCheckpointStage,
)
from server.model.video import SeriesRecord, VideoRecord
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.job import (
    ParseJobCheckpointConflictError,
    ParseJobLeaseLostError,
    ParseJobRepository,
)
from server.model.dto.jobDTO import VideoChunkCheckpointState
from server.api import jobs as jobs_api
from server.api.jobs import project_job
from server.repository.library import LibraryRepository
from server.model.dto.jobDTO import ParseJobDetail
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.utils.time import datetime_to_unix_time_ms, timedelta_to_ms


def _seed_job(
    repository,
    *,
    max_attempts: int = 3,
    workspace_id: str = "local-default",
    duration_ms: int = 60_000,
):
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    series_id = str(uuid4())
    video_id = str(uuid4())
    job_id = str(uuid4())
    now = datetime_to_unix_time_ms(datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC))
    with session_factory.begin() as session:
        session.add(
            SeriesRecord(
                series_id=series_id,
                workspace_id=workspace_id,
                name=f"Series {series_id}",
                description="description",
                status="INDEXING",
                important_extra_data={"kind": "anime"},
                character_bible=[{"name": "Anon"}],
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
                total_bytes=100,
                duration_ms=duration_ms,
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
        video_id,
        now,
    )


def test_public_job_version_tracks_semantic_changes_and_not_heartbeats(repository):
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    assert jobs.get(job_id).version == 1
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None
    assert jobs.get(job_id).version == 2
    assert queue.heartbeat(lease, lease_duration=timedelta(minutes=2), now=now + timedelta_to_ms(timedelta(seconds=1)))
    assert jobs.get(job_id).version == 2
    assert jobs.update_progress(lease, stage=ParseJobStage.PARSING, progress=30, now=now + timedelta_to_ms(timedelta(seconds=2)))
    assert jobs.get(job_id).version == 3
    assert jobs.update_progress(lease, stage=ParseJobStage.PREPARING, progress=20, now=now + timedelta_to_ms(timedelta(seconds=3)))
    assert jobs.get(job_id).version == 3
    checkpoint = VideoChunkCheckpointState(
        job_id=job_id, sequence_no=0, video_chunk_id=str(uuid4()),
        stage=VideoChunkCheckpointStage.PARSED.value,
        parse_artifact_uri="artifact://test/private/parse.json",
    )
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta_to_ms(timedelta(seconds=4)))
    assert jobs.get(job_id).version == 4
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta_to_ms(timedelta(seconds=5)))
    assert jobs.get(job_id).version == 4
    jobs.save_checkpoint(lease, replace(checkpoint, stage=VideoChunkCheckpointStage.TEXT_EMBEDDED.value), now=now + timedelta_to_ms(timedelta(seconds=6)))
    assert jobs.get(job_id).version == 5
    assert queue.acknowledge(lease, duration_ms=0, now=now + timedelta_to_ms(timedelta(seconds=7)))
    assert jobs.get(job_id).version == 6


@pytest.mark.parametrize("expire_lease", [False, True])
def test_terminal_failure_has_completion_time_and_safe_public_projection(repository, expire_lease):
    jobs, queue, job_id, _video_id, now = _seed_job(
        repository, max_attempts=1,
    )
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None
    failed_at = now + timedelta_to_ms(timedelta(minutes=3) if expire_lease else timedelta(seconds=5))
    if expire_lease:
        assert queue.reap_expired(retry_delay=timedelta(minutes=1), now=failed_at) == 1
    else:
        assert queue.retry(lease, error="private key https://internal/provider", retry_at=failed_at, duration_ms=0, now=failed_at)
    job = jobs.get(job_id)
    assert job.status == ParseJobStatus.FAILED
    assert job.completed_at == failed_at
    assert job.version == 3
    public = project_job(ParseJobDetail(job, ())).model_dump(by_alias=True)
    assert public["completedAt"] == failed_at
    assert public["availableAt"] is None
    assert public["estimatedRemainingMs"] is None
    assert public["retryOfJobId"] is None
    assert public["canRetry"] is False
    assert public["canReparse"] is False
    assert public["canCancel"] is False
    assert public["lastError"]["code"] == "PROCESSING_FAILED"
    assert "internal/provider" not in public["lastError"]["message"]
    assert not {"leaseOwner", "leaseVersion", "leaseExpiresAt", "heartbeatAt"}.intersection(public)


def test_job_pages_and_summary_count_full_actual_scope(repository):
    jobs, queue, first_job_id, _first_video, now = _seed_job(repository)
    _jobs, _queue, second_job_id, _second_video, _ = _seed_job(repository)
    _jobs, _queue, foreign_job_id, _foreign_video, _ = _seed_job(
        repository, workspace_id="foreign",
    )
    engine, _video_repository = repository
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.get(SeriesParseJobRecord, foreign_job_id).available_at = now + timedelta_to_ms(timedelta(days=1))
    service = ParseJobService(jobs, LibraryService(LibraryRepository(factory)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(jobs=service))))
    first = jobs_api.list_jobs(request, "local-default", None, "QUEUED", None, 1)
    second = jobs_api.list_jobs(request, "local-default", None, "QUEUED", first.next_cursor, 1)
    assert first.total == second.total == 2
    assert first.has_more and not second.has_more
    assert {first.items[0].job_id, second.items[0].job_id} == {first_job_id, second_job_id}
    summary = jobs_api.get_job_summary(request, "local-default", None)
    assert summary.counts.queued == 2
    assert summary.checkpoint_count == 0
    assert summary.version == 2
    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None and lease.job_id != foreign_job_id
    checkpoint = VideoChunkCheckpointState(
        job_id=lease.job_id, sequence_no=0, video_chunk_id=str(uuid4()),
        stage=VideoChunkCheckpointStage.PARSED.value,
        parse_artifact_uri="artifact://test/private/parse.json",
    )
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta_to_ms(timedelta(seconds=1)))
    summary = jobs_api.get_job_summary(request, "local-default", None)
    assert summary.counts.queued == 1
    assert summary.counts.running == 1
    assert summary.checkpoint_count == 1
    assert summary.version == 4
    subset = jobs_api.get_job_summary(request, "local-default", lease.series_id)
    assert subset.counts.running == 1
    assert subset.counts.queued == 0
    assert subset.version == 3
    detail = service.get(lease.job_id, workspace_id="local-default")
    public_checkpoint = project_job(detail).checkpoints[0].model_dump(by_alias=True)
    assert public_checkpoint == {
        "sequenceNo": 0,
        "videoChunkId": checkpoint.video_chunk_id,
        "stage": "PARSED",
        "chunkStartMs": None,
        "chunkEndMs": None,
        "lastError": None,
    }
    with pytest.raises(ValueError, match="cursor"):
        jobs_api.list_jobs(request, "local-default", None, "RUNNING", first.next_cursor, 1)


def test_job_claim_progress_checkpoint_retry_and_success(repository) -> None:
    jobs, queue, job_id, video_id, now = _seed_job(repository)
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    service = ParseJobService(
        jobs,
        LibraryService(LibraryRepository(session_factory)),
    )

    first = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )

    assert first is not None
    assert first.job_id == job_id
    assert first.video_id == video_id
    assert first.lease_version == 1
    assert first.attempt_count == 1
    assert first.processing_duration_base_ms == 0
    assert queue.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=now,
    ) is None
    assert jobs.update_progress(
        first,
        stage=ParseJobStage.PARSING,
        progress=30,
        now=now + timedelta_to_ms(timedelta(seconds=10)),
    )

    checkpoint = jobs.save_checkpoint(
        first,
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PARSED.value,
            parse_artifact_uri="artifact://test/jobs/chunk-0000.json",
        ),
        processing_duration_ms=100,
        now=now + timedelta_to_ms(timedelta(seconds=20)),
    )
    assert checkpoint.stage == VideoChunkCheckpointStage.PARSED.value
    assert len(jobs.list_checkpoints(job_id)) == 1
    assert jobs.get(job_id).processing_duration_ms == 100

    jobs.save_checkpoint(
        first,
        checkpoint,
        processing_duration_ms=90,
        now=now + timedelta_to_ms(timedelta(seconds=21)),
    )
    assert jobs.get(job_id).processing_duration_ms == 100

    retry_at = now + timedelta_to_ms(timedelta(minutes=5))
    assert queue.retry(
        first,
        error="temporary provider failure",
        retry_at=retry_at,
        duration_ms=123,
        now=now + timedelta_to_ms(timedelta(seconds=30)),
    )
    waiting = jobs.get(job_id)
    assert waiting.status == ParseJobStatus.RETRY_WAIT.value
    assert waiting.last_error == "temporary provider failure"
    assert waiting.series_name.startswith("Series ")
    assert waiting.episode_order == 1
    assert waiting.original_filename == "episode.mp4"
    waiting_detail = service.get(job_id, workspace_id="local-default")
    assert waiting_detail.processing_duration_ms == 123
    assert waiting_detail.processing_timing_complete is False
    [waiting_listed] = service.list(workspace_id="local-default")
    assert waiting_listed.processing_duration_ms == 123
    assert waiting_listed.processing_timing_complete is False

    second = queue.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    assert second is not None
    assert second.lease_version == 2
    assert second.attempt_count == 2
    assert second.processing_duration_base_ms == 123
    assert not queue.heartbeat(
        first,
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    with pytest.raises(ParseJobLeaseLostError):
        jobs.save_checkpoint(
            first,
            checkpoint,
            processing_duration_ms=999,
            now=retry_at,
        )
    assert jobs.get(job_id).processing_duration_ms == 123

    jobs.save_checkpoint(
        second,
        checkpoint,
        processing_duration_ms=500,
        now=retry_at + timedelta_to_ms(timedelta(seconds=20)),
    )
    assert jobs.get(job_id).processing_duration_ms == 500

    assert queue.acknowledge(second, duration_ms=456, now=retry_at + timedelta_to_ms(timedelta(seconds=30)))
    completed = jobs.get(job_id)
    assert completed.status == ParseJobStatus.SUCCEEDED.value
    assert completed.stage == ParseJobStage.COMPLETED.value
    assert completed.progress == 100
    completed_detail = service.get(job_id, workspace_id="local-default")
    assert completed_detail.processing_duration_ms == 579
    assert completed_detail.processing_timing_complete is True
    [completed_listed] = service.list(workspace_id="local-default")
    assert completed_listed.processing_duration_ms == 579
    assert completed_listed.processing_timing_complete is True


def test_checkpoint_cannot_move_backwards_or_change_identity(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert lease is not None
    video_chunk_id = str(uuid4())
    jobs.save_checkpoint(
        lease,
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.TEXT_EMBEDDED.value,
        ),
        now=now,
    )

    with pytest.raises(ParseJobCheckpointConflictError, match="backwards"):
        jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=video_chunk_id,
                stage=VideoChunkCheckpointStage.PARSED.value,
            ),
            now=now,
        )
    with pytest.raises(ParseJobCheckpointConflictError, match="another"):
        jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=str(uuid4()),
                stage=VideoChunkCheckpointStage.VISION_EMBEDDED.value,
            ),
            now=now,
        )


def test_checkpoint_ranges_are_paired_bounded_and_immutable(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert lease is not None
    video_chunk_id = str(uuid4())

    with pytest.raises(ValueError, match="provided together"):
        jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=video_chunk_id,
                stage=VideoChunkCheckpointStage.PREPARED.value,
                chunk_start_ms=0,
            ),
            now=now,
        )
    with pytest.raises(ValueError, match="exceeds"):
        jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=video_chunk_id,
                stage=VideoChunkCheckpointStage.PREPARED.value,
                chunk_start_ms=0,
                chunk_end_ms=60_001,
            ),
            now=now,
        )

    saved = jobs.save_checkpoint(
        lease,
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.PREPARED.value,
            chunk_start_ms=0,
            chunk_end_ms=60_000,
        ),
        now=now,
    )
    assert (saved.chunk_start_ms, saved.chunk_end_ms) == (0, 60_000)
    with pytest.raises(ParseJobCheckpointConflictError, match="cannot change"):
        jobs.save_checkpoint(
            lease,
            replace(saved, chunk_end_ms=59_000),
            now=now,
        )


def test_retry_can_fill_legacy_checkpoint_range_without_regressing_stage(
    repository,
) -> None:
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert lease is not None
    legacy = jobs.save_checkpoint(
        lease,
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PARSED.value,
        ),
        now=now,
    )

    filled = jobs.save_checkpoint(
        lease,
        replace(legacy, chunk_start_ms=0, chunk_end_ms=60_000),
        now=now,
    )

    assert filled.stage == VideoChunkCheckpointStage.PARSED.value
    assert (filled.chunk_start_ms, filled.chunk_end_ms) == (0, 60_000)
    assert jobs.get(job_id).progress == 99


def test_checkpoint_ranges_must_be_contiguous(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert lease is not None
    jobs.save_checkpoint(
        lease,
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PREPARED.value,
            chunk_start_ms=0,
            chunk_end_ms=40_000,
        ),
        now=now,
    )
    with pytest.raises(ParseJobCheckpointConflictError, match="contiguous"):
        jobs.save_checkpoint(
            lease,
            VideoChunkCheckpointState(
                job_id=job_id,
                sequence_no=1,
                video_chunk_id=str(uuid4()),
                stage=VideoChunkCheckpointStage.PREPARED.value,
                chunk_start_ms=41_000,
                chunk_end_ms=60_000,
            ),
            now=now,
        )


def test_progress_uses_split_gate_and_duration_weighted_parse_coverage(
    repository,
) -> None:
    jobs, queue, job_id, video_id, now = _seed_job(repository)
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert lease is not None
    checkpoints = [
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PREPARED.value,
            chunk_start_ms=0,
            chunk_end_ms=40_000,
        ),
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=1,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PREPARED.value,
            chunk_start_ms=40_000,
            chunk_end_ms=60_000,
        ),
    ]

    # A partial range set is not enough to establish the denominator.
    jobs.save_checkpoint(lease, checkpoints[0], now=now)
    assert jobs.get(job_id).progress == 0
    jobs.save_checkpoint(lease, checkpoints[1], now=now)
    assert jobs.get(job_id).progress == 0

    checkpoints[0] = replace(
        checkpoints[0], stage=VideoChunkCheckpointStage.SPLIT_READY.value,
    )
    jobs.save_checkpoint(lease, checkpoints[0], now=now)
    # The split point is awarded only after every chunk is split-ready.
    assert jobs.get(job_id).progress == 0
    checkpoints[1] = replace(
        checkpoints[1], stage=VideoChunkCheckpointStage.SPLIT_READY.value,
    )
    jobs.save_checkpoint(lease, checkpoints[1], now=now)
    assert jobs.get(job_id).progress == 1

    checkpoints[0] = replace(
        checkpoints[0], stage=VideoChunkCheckpointStage.PARSED.value,
    )
    jobs.save_checkpoint(lease, checkpoints[0], now=now)
    # Parsed work is weighted by canonical duration, not chunk count.
    assert jobs.get(job_id).progress == 66
    checkpoints[1] = replace(
        checkpoints[1], stage=VideoChunkCheckpointStage.PARSED.value,
    )
    jobs.save_checkpoint(lease, checkpoints[1], now=now)
    assert jobs.get(job_id).progress == 99
    with session_factory() as session:
        assert session.get(VideoRecord, video_id).parse_progress == 99

    for milestone in (
        VideoChunkCheckpointStage.TEXT_EMBEDDED,
        VideoChunkCheckpointStage.VISION_EMBEDDED,
        VideoChunkCheckpointStage.INDEXED,
    ):
        for index, checkpoint in enumerate(checkpoints):
            checkpoints[index] = replace(checkpoint, stage=milestone.value)
            jobs.save_checkpoint(lease, checkpoints[index], now=now)
        assert jobs.get(job_id).progress == 99


def test_retry_can_replay_earlier_stages_without_regressing_progress(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed_job(repository)
    first = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert first is not None
    checkpoint = VideoChunkCheckpointState(
        job_id=job_id,
        sequence_no=0,
        video_chunk_id=str(uuid4()),
        stage=VideoChunkCheckpointStage.VISION_EMBEDDED.value,
        chunk_start_ms=0,
        chunk_end_ms=60_000,
    )
    jobs.save_checkpoint(first, checkpoint, now=now)
    assert jobs.update_progress(first, stage=ParseJobStage.INDEXING, now=now)
    retry_at = now + timedelta_to_ms(timedelta(seconds=10))
    assert queue.retry(
        first,
        error="index unavailable",
        retry_at=retry_at,
        duration_ms=0,
        now=now,
    )
    second = queue.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    assert second is not None

    assert jobs.update_progress(
        second,
        stage=ParseJobStage.PREPARING,
        now=retry_at,
    )

    state = jobs.get(job_id)
    assert state.stage == ParseJobStage.INDEXING.value
    assert state.progress == 99
