from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
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
from server.repository.job import (
    ParseJobCheckpointConflictError,
    ParseJobLeaseLostError,
    ParseJobRepository,
)
from server.model.dto.jobDTO import VideoChunkCheckpointState
from server.api import jobs as jobs_api
from server.model.vo.commonVO import unix_time_ms
from server.api.jobs import project_job
from server.repository.library import LibraryRepository
from server.model.dto.jobDTO import ParseJobDetail
from server.service.job import ParseJobService
from server.service.library import LibraryService


def _seed_job(repository, *, max_attempts: int = 3, workspace_id: str = "local-default"):
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
                duration_ms=60_000,
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
    return ParseJobRepository(session_factory), job_id, video_id, now


def test_public_job_version_tracks_semantic_changes_and_not_heartbeats(repository):
    jobs, job_id, _video_id, now = _seed_job(repository)
    assert jobs.get(job_id).version == 1
    lease = jobs.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None
    assert jobs.get(job_id).version == 2
    assert jobs.heartbeat(lease, lease_duration=timedelta(minutes=2), now=now + timedelta(seconds=1))
    assert jobs.get(job_id).version == 2
    assert jobs.update_progress(lease, stage=ParseJobStage.PARSING, progress=30, now=now + timedelta(seconds=2))
    assert jobs.get(job_id).version == 3
    assert jobs.update_progress(lease, stage=ParseJobStage.PREPARING, progress=20, now=now + timedelta(seconds=3))
    assert jobs.get(job_id).version == 3
    checkpoint = VideoChunkCheckpointState(
        job_id=job_id, sequence_no=0, video_chunk_id=str(uuid4()),
        stage=VideoChunkCheckpointStage.PARSED.value,
        parse_artifact_uri="artifact://test/private/parse.json",
    )
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta(seconds=4))
    assert jobs.get(job_id).version == 4
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta(seconds=5))
    assert jobs.get(job_id).version == 4
    jobs.save_checkpoint(lease, replace(checkpoint, stage=VideoChunkCheckpointStage.TEXT_EMBEDDED.value), now=now + timedelta(seconds=6))
    assert jobs.get(job_id).version == 5
    assert jobs.mark_succeeded(lease, now=now + timedelta(seconds=7))
    assert jobs.get(job_id).version == 6


@pytest.mark.parametrize("expire_lease", [False, True])
def test_terminal_failure_has_completion_time_and_safe_public_projection(repository, expire_lease):
    jobs, job_id, _video_id, now = _seed_job(repository, max_attempts=1)
    lease = jobs.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None
    failed_at = now + timedelta(minutes=3) if expire_lease else now + timedelta(seconds=5)
    if expire_lease:
        assert jobs.reap_expired(retry_delay=timedelta(minutes=1), now=failed_at) == 1
    else:
        assert jobs.mark_failed(lease, error="private key https://internal/provider", retry_at=failed_at, now=failed_at)
    job = jobs.get(job_id)
    assert job.status == ParseJobStatus.FAILED
    assert job.completed_at == failed_at
    assert job.version == 3
    public = project_job(ParseJobDetail(job, ())).model_dump(by_alias=True)
    assert public["completedAt"] == unix_time_ms(failed_at)
    assert public["availableAt"] is None
    assert public["estimatedRemainingMs"] is None
    assert public["retryOfJobId"] is None
    assert public["canRetry"] is False
    assert public["canCancel"] is False
    assert public["lastError"]["code"] == "PROCESSING_FAILED"
    assert "internal/provider" not in public["lastError"]["message"]
    assert not {"leaseOwner", "leaseVersion", "leaseExpiresAt", "heartbeatAt"}.intersection(public)


def test_job_pages_and_summary_count_full_actual_scope(repository):
    jobs, first_job_id, _first_video, now = _seed_job(repository)
    _jobs, second_job_id, _second_video, _ = _seed_job(repository)
    _jobs, foreign_job_id, _foreign_video, _ = _seed_job(repository, workspace_id="foreign")
    engine, _video_repository = repository
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.get(SeriesParseJobRecord, foreign_job_id).available_at = now + timedelta(days=1)
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
    lease = jobs.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None and lease.job_id != foreign_job_id
    checkpoint = VideoChunkCheckpointState(
        job_id=lease.job_id, sequence_no=0, video_chunk_id=str(uuid4()),
        stage=VideoChunkCheckpointStage.PARSED.value,
        parse_artifact_uri="artifact://test/private/parse.json",
    )
    jobs.save_checkpoint(lease, checkpoint, now=now + timedelta(seconds=1))
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
    assert public_checkpoint == {"sequenceNo": 0, "videoChunkId": checkpoint.video_chunk_id, "stage": "PARSED", "lastError": None}
    with pytest.raises(ValueError, match="cursor"):
        jobs_api.list_jobs(request, "local-default", None, "RUNNING", first.next_cursor, 1)


def test_job_claim_progress_checkpoint_retry_and_success(repository) -> None:
    jobs, job_id, video_id, now = _seed_job(repository)

    first = jobs.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )

    assert first is not None
    assert first.job_id == job_id
    assert first.video_id == video_id
    assert first.lease_version == 1
    assert first.attempt_count == 1
    assert jobs.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=now,
    ) is None
    assert jobs.update_progress(
        first,
        stage=ParseJobStage.PARSING,
        progress=30,
        now=now + timedelta(seconds=10),
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
        now=now + timedelta(seconds=20),
    )
    assert checkpoint.stage == VideoChunkCheckpointStage.PARSED.value
    assert len(jobs.list_checkpoints(job_id)) == 1

    retry_at = now + timedelta(minutes=5)
    assert jobs.mark_failed(
        first,
        error="temporary provider failure",
        retry_at=retry_at,
        now=now + timedelta(seconds=30),
    )
    waiting = jobs.get(job_id)
    assert waiting.status == ParseJobStatus.RETRY_WAIT.value
    assert waiting.last_error == "temporary provider failure"
    assert waiting.series_name.startswith("Series ")
    assert waiting.episode_order == 1
    assert waiting.original_filename == "episode.mp4"

    second = jobs.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    assert second is not None
    assert second.lease_version == 2
    assert second.attempt_count == 2
    assert not jobs.heartbeat(
        first,
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    with pytest.raises(ParseJobLeaseLostError):
        jobs.save_checkpoint(
            first,
            checkpoint,
            now=retry_at,
        )

    assert jobs.mark_succeeded(second, now=retry_at + timedelta(seconds=30))
    completed = jobs.get(job_id)
    assert completed.status == ParseJobStatus.SUCCEEDED.value
    assert completed.stage == ParseJobStage.COMPLETED.value
    assert completed.progress == 100


def test_claim_allows_parallel_series_but_serializes_each_series(repository) -> None:
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    now = datetime(2026, 9, 14, 10, 0, 0)
    series_a = str(uuid4())
    series_b = str(uuid4())
    first_job = str(uuid4())
    sibling_job = str(uuid4())
    other_job = str(uuid4())
    first_video = str(uuid4())
    sibling_video = str(uuid4())
    other_video = str(uuid4())

    with session_factory.begin() as session:
        for series_id in (series_a, series_b):
            session.add(
                SeriesRecord(
                    series_id=series_id,
                    workspace_id="local-default",
                    name=f"Series {series_id}",
                    description=None,
                    status="INDEXING",
                    important_extra_data=None,
                    character_bible=[],
                )
            )
        for video_id, series_id, episode_order, content_hash in (
            (first_video, series_a, 1, "a" * 64),
            (sibling_video, series_a, 2, "b" * 64),
            (other_video, series_b, 1, "c" * 64),
        ):
            session.add(
                VideoRecord(
                    video_id=video_id,
                    series_id=series_id,
                    episode_order=episode_order,
                    upload_sequence=episode_order,
                    original_filename=f"episode-{episode_order}.mp4",
                    original_source_uri=f"artifact://test/{video_id}.mp4",
                    inference_proxy_uri=None,
                    content_hash=content_hash,
                    total_bytes=100,
                    duration_ms=60_000,
                    upload_status="COMPLETED",
                    parse_status=ParseJobStatus.QUEUED.value,
                    upload_progress=100,
                    parse_progress=0,
                )
            )
        for job_id, series_id, video_id, delay in (
            (first_job, series_a, first_video, 0),
            (sibling_job, series_a, sibling_video, 1),
            (other_job, series_b, other_video, 2),
        ):
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
                    max_attempts=3,
                    available_at=now + timedelta(seconds=delay),
                    lease_version=0,
                )
            )

    claim_at = now + timedelta(seconds=3)
    first = jobs.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=claim_at,
    )
    parallel = jobs.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=claim_at,
    )

    assert first is not None and first.job_id == first_job
    assert parallel is not None and parallel.job_id == other_job
    assert jobs.claim_next(
        "worker-c",
        lease_duration=timedelta(minutes=2),
        now=claim_at,
    ) is None

    assert jobs.mark_succeeded(first, now=claim_at + timedelta(seconds=1))
    sibling = jobs.claim_next(
        "worker-c",
        lease_duration=timedelta(minutes=2),
        now=claim_at + timedelta(seconds=2),
    )
    assert sibling is not None and sibling.job_id == sibling_job

    with session_factory() as session:
        assert session.get(SeriesRecord, series_a).status == "INDEXING"


def test_checkpoint_cannot_move_backwards_or_change_identity(repository) -> None:
    jobs, job_id, _video_id, now = _seed_job(repository)
    lease = jobs.claim_next(
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


def test_reaper_requeues_or_exhausts_expired_leases(repository) -> None:
    jobs, job_id, _video_id, now = _seed_job(repository, max_attempts=1)
    lease = jobs.claim_next(
        "worker-a",
        lease_duration=timedelta(seconds=30),
        now=now,
    )
    assert lease is not None

    reaped = jobs.reap_expired(
        retry_delay=timedelta(minutes=1),
        now=now + timedelta(seconds=31),
    )

    assert reaped == 1
    state = jobs.get(job_id)
    assert state.status == ParseJobStatus.FAILED.value
    assert state.last_error == "worker lease expired"


def test_retry_can_replay_earlier_stages_without_regressing_progress(repository) -> None:
    jobs, job_id, _video_id, now = _seed_job(repository)
    first = jobs.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=2),
        now=now,
    )
    assert first is not None
    assert jobs.update_progress(
        first,
        stage=ParseJobStage.INDEXING,
        progress=95,
        now=now,
    )
    retry_at = now + timedelta(seconds=10)
    assert jobs.mark_failed(
        first,
        error="index unavailable",
        retry_at=retry_at,
        now=now,
    )
    second = jobs.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=retry_at,
    )
    assert second is not None

    assert jobs.update_progress(
        second,
        stage=ParseJobStage.PREPARING,
        progress=2,
        now=retry_at,
    )

    state = jobs.get(job_id)
    assert state.stage == ParseJobStage.INDEXING.value
    assert state.progress == 95
