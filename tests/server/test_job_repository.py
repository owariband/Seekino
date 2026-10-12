from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from server.config.parse_profile import default_video_parse_v2_config
from server.database import create_session_factory
from engine.parsev2.planning import interval_size, plan_intervals
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoChunkCheckpointStage,
)
from server.model.upload import UploadSession, UploadSessionStatus
from server.model.video import Series, VideoChunk, Video
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.job import (
    ParseJobCheckpointConflictError,
    ParseJobLeaseLostError,
    ParseJobNotFoundError,
    ParseJobRepository,
)
from server.model.dto.jobDTO import VideoChunkCheckpointState
from server.api import jobs as jobs_api
from server.api.jobs import project_job, project_video_parse_task
from server.repository.library import LibraryRepository
from server.model.dto.jobDTO import ParseJobDetail
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.utils.time import datetime_to_unix_time_ms, timedelta_to_ms


def _seed_v2_tracking(repository, *, chunk_count=2, completed_chunks=2, submitted_refines=0):
    jobs, _, _, video_id, now_ms = _seed_job(repository)
    factory = create_session_factory(repository[0])
    config = default_video_parse_v2_config().model_dump(mode="json")
    now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    task_id, chunk_ids, interval_ids = str(uuid4()), [], []
    with factory.begin() as session:
        session.add(VideoParseTask(
            id=task_id, video_id=video_id, pipeline_version="video-parse-v2",
            status="PARSING_FAILED", parse_config=config, chunk_count=chunk_count,
            version=1, next_execute_time=now, attempt_count=1, max_attempts=3,
            detail={"source": "upload"},
        ))
        for sequence in range(chunk_count):
            chunk_id, child_id = str(uuid4()), str(uuid4())
            chunk_ids.append(child_id)
            session.add(VideoChunk(
                id=chunk_id, video_id=video_id, sequence_no=sequence,
                start_ms=sequence * 1000, end_ms=(sequence + 1) * 1000,
                window_start_ms=sequence * 1000, window_end_ms=(sequence + 1) * 1000,
                chunk_media_ref="artifact://test/clip.mp4",
                analysis_window_media_ref="artifact://test/clip.mp4", keyframe_uris=[],
            ))
            complete = sequence < completed_chunks
            session.add(ChunkParseTask(
                id=child_id, video_task_id=task_id, chunk_id=chunk_id,
                status="COMPLETE" if complete else "QUEUED", lease_version=int(complete),
                next_execute_time=now, start_time=now if complete else None,
            ))
            if complete:
                session.add(ChunkParseContent(id=str(uuid4()), chunk_task_id=child_id, content={"persons": [], "timeline": []}))
        plans = plan_intervals(chunk_count=chunk_count, size=interval_size(
            interval_duration=config["interval_duration"], chunk_duration=config["chunk_duration"],
        ))
        for index, plan in enumerate(plans):
            interval_id = str(uuid4())
            interval_ids.append(interval_id)
            submitted = index < submitted_refines
            session.add(IntervalParseTask(
                id=interval_id, video_task_id=task_id, chunk_task_id=chunk_ids[plan.start_sequence_no],
                chunk_task_ids=chunk_ids[plan.start_sequence_no:plan.end_sequence_no],
                start_sequence_no=plan.start_sequence_no, chunk_count=plan.chunk_count,
                status="INDEXING_FAILED" if submitted else "WAITING", lease_version=2 if submitted else 0,
                next_execute_time=now, start_time=now if submitted else None,
            ))
            if submitted:
                session.add(IntervalParseContent(
                    interval_task_id=interval_id, content={"updates": []},
                    detail={"refinement_committed_lease_version": 1},
                ))
    return jobs, factory, task_id, chunk_ids, interval_ids, now_ms


def _timing_entry(phase, task_id, *, started=1000, elapsed=1000, version=1, finished=True):
    return {
        "phase": phase, "execution_kind": "chunk" if phase == "understand" else "interval",
        "execution_task_id": task_id, "lease_version": version, "worker_id": "worker",
        "started_at_ms": started, "elapsed_ms": elapsed,
        "finished": finished, "outcome": "completed" if finished else "running",
    }


def _store_timing(factory, task_id, entries):
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.detail = {**task.detail, "_worker_runtime_v2": {"execution_timing": {"version": 1, "entries": entries}}}


def _seed_job(
    repository,
    *,
    max_attempts: int = 3,
    tenant_id: str = "local-default",
    duration_seconds: int = 60,
):
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    series_id = str(uuid4())
    video_id = str(uuid4())
    job_id = str(uuid4())
    now = datetime_to_unix_time_ms(datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC))
    with session_factory.begin() as session:
        session.add(
            Series(
                id=series_id,
                tenant_id=tenant_id,
                name=f"Series {series_id}",
                description="description",
                status="INDEXING",
                important_extra_data={"kind": "anime"},
                character_bible=[{"name": "Anon"}],
            )
        )
        session.add(
            Video(
                id=video_id,
                series_id=series_id,
                episode_order=1,
                upload_sequence=1,
                filename="episode.mp4",
                source_uri="artifact://test/media/episode.mp4",
                proxy_uri=None,
                hash="a" * 64,
                size_bytes=100,
                duration_ms=duration_seconds,
                upload_status="COMPLETED",
                parse_status="QUEUED",
                upload_progress=100,
                parse_progress=0,
            )
        )
        session.add(
            SeriesParseJob(
                id=job_id,
                series_id=series_id,
                video_id=video_id,
                pipeline_version="pipeline-v1",
                status=ParseJobStatus.QUEUED.value,
                stage=ParseJobStage.QUEUED.value,
                progress=0,
                attempt_count=0,
                max_attempts=max_attempts,
                next_execute_time=now,
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


def test_v2_task_repository_reads_frozen_config_video_and_parse_versions(repository):
    jobs, _queue, job_id, video_id, _now = _seed_job(repository)
    engine, _video_repository = repository
    factory = create_session_factory(engine)
    first_id, second_id = str(uuid4()), str(uuid4())
    upload_session_id = str(uuid4())
    first_time = datetime(2026, 9, 14, 10, 0, 0, 123000, tzinfo=UTC)
    second_time = first_time + timedelta(minutes=1)
    frozen_config = default_video_parse_v2_config().model_dump(mode="json")
    with factory.begin() as session:
        session.add_all([
            VideoParseTask(
                id=first_id,
                video_id=video_id,
                pipeline_version="video-parse-v2",
                status="PARSING_FAILED",
                parse_config=frozen_config,
                chunk_count=1,
                version=1,
                next_execute_time=first_time,
                attempt_count=3,
                max_attempts=3,
                processing_duration_ms=100,
                processing_timing_complete=True,
                last_error={"code": "MODEL_ERROR"},
                detail={"source": "upload"},
                create_time=datetime_to_unix_time_ms(first_time),
                update_time=datetime_to_unix_time_ms(first_time),
            ),
            VideoParseTask(
                id=second_id,
                video_id=video_id,
                pipeline_version="video-parse-v2",
                status="PENDING",
                parse_config=frozen_config,
                chunk_count=1,
                version=2,
                next_execute_time=second_time,
                attempt_count=0,
                max_attempts=3,
                processing_duration_ms=0,
                processing_timing_complete=True,
                detail={"source": "reparse"},
                create_time=datetime_to_unix_time_ms(second_time),
                update_time=datetime_to_unix_time_ms(second_time),
            ),
            UploadSession(
                id=upload_session_id,
                tenant_id="local-default",
                series_id=session.get(Video, video_id).series_id,
                episode_order=1,
                upload_sequence=1,
                filename="episode.mp4",
                media_type="video/mp4",
                size_bytes=100,
                segment_size_bytes=100,
                segment_count=1,
                version=1,
                status=UploadSessionStatus.COMPLETED.value,
                idempotency_key="v2-task-observation",
                expire_time=second_time,
                finish_time=first_time,
                video_id=video_id,
                job_id=job_id,
                initial_task_id=first_id,
                create_time=datetime_to_unix_time_ms(first_time),
                update_time=datetime_to_unix_time_ms(first_time),
            ),
        ])

    first = jobs.get_video_parse_task(first_id)
    parse_input = jobs.get_video_parse_input(first_id)
    assert first.video_parse_task_id == first_id
    assert (first.version, first.status) == (1, "PARSING_FAILED")
    assert first.parse_config == frozen_config
    assert first.next_execute_time == first_time
    assert first.next_execute_time.utcoffset() == timedelta(0)
    assert parse_input.task == first
    assert parse_input.task.video_id == video_id
    assert parse_input.original_filename == "episode.mp4"
    assert parse_input.duration_seconds == 60
    assert [item.video_parse_task_id for item in jobs.list_video_parse_tasks(video_id)] == [
        first_id,
        second_id,
    ]
    latest = jobs.latest_video_parse_task(video_id)
    assert (latest.video_parse_task_id, latest.version) == (
        second_id,
        2,
    )
    assert jobs.get_video_parse_task(first_id) == first
    detail = jobs.get_video_parse_task_detail(
        second_id,
        tenant_id="local-default",
    )
    assert detail.task.video_parse_task_id == second_id
    assert detail.tenant_id == "local-default"
    assert detail.series_name.startswith("Series ")
    assert detail.episode_order == 1
    assert detail.original_filename == "episode.mp4"
    assert detail.duration_seconds == 60
    assert detail.upload_session_id == upload_session_id
    assert detail.is_latest is True
    assert project_video_parse_task(detail).can_reparse is False
    v2_service = ParseJobService(
        jobs,
        LibraryService(LibraryRepository(factory)),
        execution_mode="v2",
    )
    takeover = v2_service.get_video_parse_task(
        second_id,
        tenant_id="local-default",
    )
    assert takeover.pending_takeover_enabled is True
    assert project_video_parse_task(takeover).can_reparse is True
    assert jobs.get_video_parse_task_detail(
        first_id,
        tenant_id="local-default",
    ).is_latest is False
    failed_latest = replace(
        detail,
        task=replace(detail.task, status="PARSING_FAILED"),
    )
    assert project_video_parse_task(failed_latest).can_retry is True
    failed_history = replace(
        jobs.get_video_parse_task_detail(
            first_id,
            tenant_id="local-default",
        ),
        task=replace(first, status="PARSING_FAILED"),
    )
    assert project_video_parse_task(failed_history).can_retry is False
    with pytest.raises(ParseJobNotFoundError):
        jobs.get_video_parse_task_detail(second_id, tenant_id="foreign")

    pending = jobs.list_video_parse_tasks_for_workspace(
        "local-default",
        statuses=("PENDING",),
    )
    assert [item.task.video_parse_task_id for item in pending] == [second_id]
    assert jobs.count_video_parse_tasks_for_workspace(
        "local-default",
        statuses=("PENDING",),
    ) == 1
    first_page = jobs.list_video_parse_tasks_for_workspace(
        "local-default",
        limit=1,
    )
    second_page = jobs.list_video_parse_tasks_for_workspace(
        "local-default",
        limit=1,
        offset=1,
    )
    assert [item.task.video_parse_task_id for item in first_page + second_page] == [
        second_id,
        first_id,
    ]

    chunk_id = str(uuid4())
    chunk_task_id = str(uuid4())
    with factory.begin() as session:
        task = session.get(VideoParseTask, second_id)
        task.status = "PARSING_FAILED"
        task.detail = {"_worker_runtime_v2": {"accepted": True}}
        session.add(VideoChunk(
            id=chunk_id,
            video_id=video_id,
            sequence_no=0,
            start_ms=0,
            end_ms=10,
            window_start_ms=0,
            window_end_ms=10,
            chunk_media_ref="artifact://test/chunk.mp4",
            analysis_window_media_ref="artifact://test/window.mp4",
            transcript=[],
            keyframe_uris=[],
        ))
        session.add(ChunkParseTask(
            id=chunk_task_id,
            video_task_id=second_id,
            chunk_id=chunk_id,
            status="UNDERSTANDING",
            next_execute_time=second_time,
            retry_count=0,
            lease_owner="active-worker",
            lease_version=1,
            lease_expire_time=datetime(2099, 1, 1, tzinfo=UTC),
        ))

    executing = jobs.get_video_parse_task_detail(
        second_id,
        tenant_id="local-default",
    )
    assert executing.has_active_execution_lease is True
    assert executing.has_active_deletion_lease is True
    executing_view = project_video_parse_task(executing)
    assert executing_view.can_retry is False
    assert executing_view.can_reparse is True
    assert executing_view.can_delete is False

    with factory.begin() as session:
        child = session.get(ChunkParseTask, chunk_task_id)
        child.status = "FAILED"
        child.lease_owner = None
        child.lease_expire_time = None

    released = jobs.get_video_parse_task_detail(
        second_id,
        tenant_id="local-default",
    )
    assert released.has_active_execution_lease is False
    assert released.has_active_deletion_lease is False
    released_view = project_video_parse_task(released)
    assert released_view.can_retry is True
    assert released_view.can_delete is True

    later_video_id = str(uuid4())
    later_task_id = str(uuid4())
    active_prefix_until = datetime(2099, 1, 1, tzinfo=UTC)
    with factory.begin() as session:
        series_id = session.get(Video, video_id).series_id
        session.add(Video(
            id=later_video_id,
            series_id=series_id,
            episode_order=2,
            upload_sequence=2,
            filename="episode-2.mp4",
            source_uri="artifact://test/media/episode-2.mp4",
            hash="b" * 64,
            size_bytes=100,
            duration_ms=60,
            upload_status="COMPLETED",
            parse_status="QUEUED",
            upload_progress=100,
            parse_progress=0,
        ))
        session.add(VideoParseTask(
            id=later_task_id,
            video_id=later_video_id,
            pipeline_version="video-parse-v2",
            status="PARSING",
            parse_config=frozen_config,
            chunk_count=1,
            version=1,
            next_execute_time=second_time,
            attempt_count=1,
            max_attempts=3,
            lease_owner="parent-worker",
            lease_version=1,
            lease_expire_time=active_prefix_until,
            heartbeat_time=second_time,
            processing_duration_ms=0,
            processing_timing_complete=True,
            detail={
                "_worker_runtime_v2": {
                    "accepted": True,
                    "prefix": {
                        "lease_owner": "prefix-worker",
                        "lease_expire_time_ms": datetime_to_unix_time_ms(
                            active_prefix_until
                        ),
                    },
                },
            },
            create_time=datetime_to_unix_time_ms(second_time),
            update_time=datetime_to_unix_time_ms(second_time),
        ))

    suffix_fenced = jobs.get_video_parse_task_detail(
        second_id,
        tenant_id="local-default",
    )
    assert suffix_fenced.has_active_prefix_lease is False
    assert suffix_fenced.has_active_prefix_suffix_lease is True
    assert project_video_parse_task(suffix_fenced).can_reparse is False


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
        repository, tenant_id="foreign",
    )
    engine, _video_repository = repository
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.get(SeriesParseJob, foreign_job_id).next_execute_time = now + timedelta_to_ms(timedelta(days=1))
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
    detail = service.get(lease.job_id, tenant_id="local-default")
    public_checkpoint = project_job(detail).checkpoints[0].model_dump(by_alias=True)
    assert public_checkpoint == {
        "sequenceNo": 0,
        "videoChunkId": checkpoint.video_chunk_id,
        "stage": "PARSED",
        "chunkStartSeconds": None,
        "chunkEndSeconds": None,
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
    waiting_detail = service.get(job_id, tenant_id="local-default")
    assert waiting_detail.processing_duration_ms == 123
    assert waiting_detail.processing_timing_complete is False
    [waiting_listed] = service.list(tenant_id="local-default")
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
    completed_detail = service.get(job_id, tenant_id="local-default")
    assert completed_detail.processing_duration_ms == 579
    assert completed_detail.processing_timing_complete is True
    [completed_listed] = service.list(tenant_id="local-default")
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
                chunk_start_seconds=0,
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
                chunk_start_seconds=0,
                chunk_end_seconds=61,
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
            chunk_start_seconds=0,
            chunk_end_seconds=60,
        ),
        now=now,
    )
    assert (saved.chunk_start_seconds, saved.chunk_end_seconds) == (0, 60)
    with pytest.raises(ParseJobCheckpointConflictError, match="cannot change"):
        jobs.save_checkpoint(
            lease,
            replace(saved, chunk_end_seconds=59),
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
        replace(legacy, chunk_start_seconds=0, chunk_end_seconds=60),
        now=now,
    )

    assert filled.stage == VideoChunkCheckpointStage.PARSED.value
    assert (filled.chunk_start_seconds, filled.chunk_end_seconds) == (0, 60)
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
            chunk_start_seconds=0,
            chunk_end_seconds=40,
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
                chunk_start_seconds=41,
                chunk_end_seconds=60,
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
            chunk_start_seconds=0,
            chunk_end_seconds=40,
        ),
        VideoChunkCheckpointState(
            job_id=job_id,
            sequence_no=1,
            video_chunk_id=str(uuid4()),
            stage=VideoChunkCheckpointStage.PREPARED.value,
            chunk_start_seconds=40,
            chunk_end_seconds=60,
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
        assert session.get(Video, video_id).parse_progress == 99

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
        chunk_start_seconds=0,
        chunk_end_seconds=60,
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


def test_v2_progress_counts_submissions_and_batches_list_reads(repository):
    from sqlalchemy import event

    jobs, factory, task_id, _, _, _ = _seed_v2_tracking(
        repository, chunk_count=24, completed_chunks=24, submitted_refines=8,
    )
    _, _, other_id, _, _, _ = _seed_v2_tracking(repository, completed_chunks=1)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(repository[0], "before_cursor_execute", capture)
    try:
        listed = {item.task.video_parse_task_id: item for item in jobs.list_video_parse_tasks_for_workspace("local-default")}
    finally:
        event.remove(repository[0], "before_cursor_execute", capture)
    assert sum("chunk_parse_content" in statement for statement in statements) == 1
    assert sum("interval_parse_content" in statement for statement in statements) == 1
    assert all(statement.lstrip().startswith("select") for statement in statements)
    detail = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default")
    assert detail.task.detail == listed[task_id].task.detail
    assert detail.task.detail["progress"] == {
        "completedChunks": 24, "totalChunks": 24, "completedRefines": 8, "totalRefines": 22,
        "completedUnits": 32, "totalUnits": 68, "percent": pytest.approx(32 * 95 / 46),
        "topologyReady": True, "indexedIntervals": 0,
    }
    assert listed[other_id].task.detail["progress"]["completedChunks"] == 1
    assert detail.task.detail["source"] == "upload"
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail == {"source": "upload"}


def test_v2_progress_includes_persisted_index_publication(repository):
    jobs, factory, task_id, _, intervals, _ = _seed_v2_tracking(
        repository, chunk_count=24, completed_chunks=24, submitted_refines=22,
    )
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["completedUnits"] == 46
    assert progress["totalUnits"] == 68
    assert progress["indexedIntervals"] == 0
    assert progress["percent"] == 95

    with factory.begin() as session:
        session.get(IntervalParseTask, intervals[0]).status = "COMPLETE"
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["indexedIntervals"] == 1
    assert progress["completedUnits"] == 47
    assert progress["percent"] == pytest.approx(95 + 5 / 22)

    with factory.begin() as session:
        for interval_id in intervals:
            session.get(IntervalParseTask, interval_id).status = "COMPLETE"
        session.get(VideoParseTask, task_id).status = "COMPLETE"
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["indexedIntervals"] == 22
    assert progress["completedUnits"] == progress["totalUnits"] == 68
    assert progress["percent"] == 100

    with factory.begin() as session:
        session.delete(session.get(IntervalParseContent, intervals[0]))
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["indexedIntervals"] == 21
    assert progress["completedRefines"] == 21
    assert progress["completedUnits"] == 66
    assert progress["percent"] == pytest.approx(45 * 95 / 46 + 21 * 5 / 22)


@pytest.mark.parametrize("elapsed,expected", [(1000, 1500), (0, 0)])
def test_v2_timing_unions_parallel_spans_and_preserves_zero(repository, elapsed, expected):
    jobs, factory, task_id, chunks, _, _ = _seed_v2_tracking(repository)
    entries = {f"understand:{child}:1": _timing_entry("understand", child, started=1000 + index * 500, elapsed=elapsed)
               for index, child in enumerate(chunks)}
    _store_timing(factory, task_id, entries)
    detail = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default")
    assert detail.task.detail["timing"] == {
        "recorded": True, "complete": True, "activeExecutions": 0, "effectiveDurationMs": expected,
    }
    assert detail.task.processing_duration_ms == expected
    assert detail.task.processing_timing_complete is True
    assert "_worker_runtime_v2" not in project_video_parse_task(detail).detail
    listed = jobs.list_video_parse_tasks_for_workspace("local-default")[0]
    assert listed.task.detail == detail.task.detail
    with factory() as session:
        assert session.get(VideoParseTask, task_id).processing_duration_ms == 0


def test_v2_old_attempts_have_unknown_timing_and_unready_topology_has_no_percent(repository):
    jobs, factory, task_id, _, intervals, _ = _seed_v2_tracking(repository)
    detail = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default")
    assert detail.task.detail["timing"] == {
        "recorded": False, "complete": False, "activeExecutions": 0, "effectiveDurationMs": None,
    }
    assert detail.task.processing_timing_complete is False
    with factory.begin() as session:
        session.delete(session.get(IntervalParseTask, intervals[0]))
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["completedUnits"] == progress["totalUnits"] == 2
    assert progress["topologyReady"] is False and progress["percent"] is None


@pytest.mark.parametrize("config", [None, {}, {"chunk_duration": 0, "interval_duration": 180}])
def test_v2_pending_or_legacy_topology_is_safe_without_valid_config(repository, config):
    factory = create_session_factory(repository[0])
    record = SimpleNamespace(id=str(uuid4()), video_id=str(uuid4()), chunk_count=0, parse_config=config, detail={})
    with factory() as session:
        tracking = ParseJobRepository._video_parse_tracking(session, (record,), 1000)[record.id]
    assert tracking["progress"]["totalUnits"] == 0
    assert tracking["progress"]["topologyReady"] is False
    assert tracking["progress"]["percent"] is None
    assert tracking["timing"]["effectiveDurationMs"] is None


def test_v2_progress_requires_chunk_content_and_the_frozen_sequence_topology(repository):
    jobs, factory, task_id, chunks, _, _ = _seed_v2_tracking(repository)
    with factory.begin() as session:
        content = session.query(ChunkParseContent).filter_by(chunk_task_id=chunks[0]).one()
        session.delete(content)
        child = session.get(ChunkParseTask, chunks[1])
        session.get(VideoChunk, child.chunk_id).sequence_no = 2
    progress = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["progress"]
    assert progress["completedChunks"] == 1
    assert progress["totalChunks"] == 2
    assert progress["topologyReady"] is False and progress["percent"] is None


def test_v2_timing_requires_retry_coverage_for_refine_and_index(repository):
    jobs, factory, task_id, chunks, intervals, _ = _seed_v2_tracking(repository, submitted_refines=1)
    entries = {f"understand:{child}:1": _timing_entry("understand", child) for child in chunks}
    entries[f"refine:{intervals[0]}:1"] = _timing_entry("refine", intervals[0], started=3000)
    _store_timing(factory, task_id, entries)
    detail = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default")
    assert detail.task.detail["timing"]["complete"] is False  # Historical index lease 2 was not recorded.
    entries[f"index:{intervals[0]}:2"] = _timing_entry("index", intervals[0], version=2, started=7000)
    _store_timing(factory, task_id, entries)
    timing = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert timing["complete"] is True
    assert timing["effectiveDurationMs"] == 3000  # Gaps between executions are excluded.
    assert detail.task.detail["progress"]["completedRefines"] == 1  # The submitted updates list is empty.
    with factory.begin() as session:
        session.get(ChunkParseTask, chunks[0]).lease_version = 2
    assert jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]["complete"] is False
    entries[f"understand:{chunks[0]}:2"] = _timing_entry("understand", chunks[0], version=2, started=5000)
    _store_timing(factory, task_id, entries)
    assert jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]["complete"] is True
    with factory.begin() as session:
        session.get(IntervalParseContent, intervals[0]).detail = None
    legacy = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail
    assert legacy["progress"]["completedRefines"] == 1
    assert legacy["timing"]["complete"] is False


@pytest.mark.parametrize("refine_started,refine_elapsed,index_started,index_elapsed,expected", [
    (1600, 500, 2000, 1500, 2500), (3000, 1000, 7000, 3000, 5000),
])
def test_v2_timing_unions_index_with_parse_and_refine(
    repository, refine_started, refine_elapsed, index_started, index_elapsed, expected,
):
    jobs, factory, task_id, chunks, intervals, _ = _seed_v2_tracking(repository, submitted_refines=1)
    entries = {f"understand:{child}:1": _timing_entry("understand", child) for child in chunks}
    entries[f"refine:{intervals[0]}:1"] = _timing_entry("refine", intervals[0],
                                                     started=refine_started, elapsed=refine_elapsed)
    entries[f"index:{intervals[0]}:2"] = _timing_entry("index", intervals[0], version=2,
                                                               started=index_started, elapsed=index_elapsed)
    _store_timing(factory, task_id, entries)
    detail = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default")
    assert detail.task.detail["timing"] == {
        "recorded": True, "complete": True, "activeExecutions": 0, "effectiveDurationMs": expected,
    }
    assert detail.task.processing_duration_ms == expected


def test_v2_index_timing_requires_every_index_retry_and_preserves_failed_execution(repository):
    jobs, factory, task_id, chunks, intervals, _ = _seed_v2_tracking(repository, submitted_refines=1)
    with factory.begin() as session:
        session.get(IntervalParseTask, intervals[0]).lease_version = 3
    entries = {f"understand:{child}:1": _timing_entry("understand", child) for child in chunks}
    entries[f"refine:{intervals[0]}:1"] = _timing_entry("refine", intervals[0], started=3000)
    entries[f"index:{intervals[0]}:3"] = _timing_entry("index", intervals[0], version=3, started=9000)
    _store_timing(factory, task_id, entries)
    assert jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]["complete"] is False
    failed = _timing_entry("index", intervals[0], version=2, started=5000, elapsed=2000)
    failed["outcome"] = "failed"
    entries[f"index:{intervals[0]}:2"] = failed
    _store_timing(factory, task_id, entries)
    timing = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert timing["complete"] is True
    assert timing["effectiveDurationMs"] == 5000


def test_v2_active_index_timing_is_confirmed_prefix_and_expires(repository, monkeypatch):
    jobs, factory, task_id, chunks, intervals, now_ms = _seed_v2_tracking(repository, submitted_refines=1)
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms)
    with factory.begin() as session:
        child = session.get(IntervalParseTask, intervals[0])
        child.status, child.lease_owner = "INDEXING", "worker"
        child.lease_expire_time = datetime.fromtimestamp(now_ms / 1000, tz=UTC) + timedelta(minutes=1)
    entries = {f"understand:{child}:1": _timing_entry("understand", child, started=now_ms - 5000) for child in chunks}
    entries[f"refine:{intervals[0]}:1"] = _timing_entry("refine", intervals[0], started=now_ms - 3000)
    entries[f"index:{intervals[0]}:2"] = _timing_entry("index", intervals[0], version=2,
                                                     started=now_ms - 500, elapsed=100, finished=False)
    _store_timing(factory, task_id, entries)
    timing = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert timing == {"recorded": True, "complete": True, "activeExecutions": 1, "effectiveDurationMs": 2100}
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms + 10_000)
    assert jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"] == timing
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms + 60_000)
    expired = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert expired == {**timing, "complete": False, "activeExecutions": 0}


def test_v2_active_timing_is_recorded_prefix_without_clock_extrapolation(repository, monkeypatch):
    jobs, factory, task_id, chunks, _, now_ms = _seed_v2_tracking(repository, completed_chunks=1)
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms)
    with factory.begin() as session:
        child = session.get(ChunkParseTask, chunks[1])
        child.status, child.lease_version, child.lease_owner = "UNDERSTANDING", 1, "worker"
        child.lease_expire_time = datetime.fromtimestamp(now_ms / 1000, tz=UTC) + timedelta(minutes=1)
    entries = {f"understand:{chunks[0]}:1": _timing_entry("understand", chunks[0], started=now_ms - 2000),
               f"understand:{chunks[1]}:1": _timing_entry("understand", chunks[1], started=now_ms - 500, elapsed=100, finished=False)}
    _store_timing(factory, task_id, entries)
    timing = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert timing == {"recorded": True, "complete": True, "activeExecutions": 1, "effectiveDurationMs": 1100}
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms + 10_000)
    assert jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"] == timing
    monkeypatch.setattr("server.repository.job.now_unix_time_ms", lambda: now_ms + 60_000)
    expired = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert expired == {**timing, "complete": False, "activeExecutions": 0}


@pytest.mark.parametrize("corruption", ["foreign_id", "bool_elapsed", "negative_elapsed", "wrong_kind", "wrong_key"])
def test_v2_timing_rejects_malformed_or_foreign_entries(repository, corruption):
    jobs, factory, task_id, chunks, _, _ = _seed_v2_tracking(repository)
    _, _, _, foreign_chunks, _, _ = _seed_v2_tracking(repository)
    entries = {f"understand:{child}:1": _timing_entry("understand", child) for child in chunks}
    invalid = _timing_entry("understand", chunks[0], started=1, elapsed=10_000)
    if corruption == "foreign_id":
        invalid["execution_task_id"] = foreign_chunks[0]
    elif corruption == "bool_elapsed":
        invalid["elapsed_ms"] = True
    elif corruption == "negative_elapsed":
        invalid["elapsed_ms"] = -1
    elif corruption == "wrong_kind":
        invalid["execution_kind"] = "interval"
    key = f"understand:{invalid['execution_task_id']}:1" if corruption != "wrong_key" else "not-a-trace-key"
    entries[key] = invalid
    _store_timing(factory, task_id, entries)
    timing = jobs.get_video_parse_task_detail(task_id, tenant_id="local-default").task.detail["timing"]
    assert timing["complete"] is False
    assert timing["effectiveDurationMs"] == 1000
    with pytest.raises(ParseJobNotFoundError):
        jobs.get_video_parse_task_detail(task_id, tenant_id="foreign")
