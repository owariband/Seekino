from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from server.database import create_session_factory
from server.model.dto.parseDTO import PreparedEmbedding
from server.model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from server.model.upload import UploadSessionRecord, UploadSessionStatus
from server.model.video import SeriesRecord, VideoRecord
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.index_sync import IndexSyncRepository
from server.repository.job import ParseJobRepository
from server.utils.time import timedelta_to_ms


def _seed(repository, *, max_attempts: int = 3):
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    series_id = str(uuid4())
    video_id = str(uuid4())
    job_id = str(uuid4())
    now = 1_789_549_200_000
    with session_factory.begin() as session:
        session.add(
            SeriesRecord(
                series_id=series_id,
                workspace_id="local-default",
                name=f"Queue Series {series_id}",
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
                duration_seconds=20,
                upload_status="COMPLETED",
                parse_status=ParseJobStatus.QUEUED.value,
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


def test_mysql_queue_claims_heartbeats_retries_and_acknowledges(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed(repository)

    first = queue.claim_next(
        "consumer-a", lease_duration=timedelta(minutes=2), now=now,
    )

    assert first is not None
    assert first.job_id == job_id
    assert queue.heartbeat(
        first,
        lease_duration=timedelta(minutes=2),
        now=now + timedelta_to_ms(timedelta(seconds=30)),
    )
    retry_at = now + timedelta_to_ms(timedelta(minutes=3))
    assert queue.retry(
        first,
        error="provider unavailable",
        retry_at=retry_at,
        duration_ms=0,
        now=now + timedelta_to_ms(timedelta(minutes=1)),
    )
    second = queue.claim_next(
        "consumer-b", lease_duration=timedelta(minutes=2), now=retry_at,
    )
    assert second is not None
    assert second.lease_version == first.lease_version + 1
    assert queue.acknowledge(second, duration_ms=0, now=retry_at + timedelta_to_ms(timedelta(seconds=1)))

    completed = jobs.get(job_id)
    assert completed.status == ParseJobStatus.SUCCEEDED.value
    assert completed.progress == 100


def test_claiming_new_episode_keeps_indexed_sibling_searchable(
    repository, prepared_import,
) -> None:
    engine, video_repository = repository
    _jobs, queue, _job_id, queued_video_id, now = _seed(repository)
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        queued_video = session.get(VideoRecord, queued_video_id)
        queued_video.episode_order = 2
        queued_video.upload_sequence = 2
        series = session.get(SeriesRecord, queued_video.series_id)
        series_id, series_name = series.series_id, series.name

    video_repository.import_video_parse(replace(
        prepared_import,
        series_id=series_id,
        series_name=series_name,
        episode_order=1,
        upload_sequence=1,
        chunks=tuple(replace(
            chunk,
            text_embedding=PreparedEmbedding([0.1], {"model": "test"}),
            vision_embedding=PreparedEmbedding([0.2], {"model": "test"}),
        ) for chunk in prepared_import.chunks),
    ))
    index_repository = IndexSyncRepository(session_factory)
    for item in index_repository.list_all():
        assert index_repository.mark_synced(
            item.video_chunk_id, item.source_revision, item.operation,
            item.generation,
        )
    with session_factory() as session:
        assert session.get(SeriesRecord, series_id).status == "READY"

    lease = queue.claim_next("worker", lease_duration=timedelta(minutes=2), now=now)
    assert lease is not None and lease.video_id == queued_video_id
    with session_factory() as session:
        assert session.get(SeriesRecord, series_id).status == "READY"


def test_mysql_queue_reaps_an_expired_final_attempt(repository) -> None:
    jobs, queue, job_id, _video_id, now = _seed(repository, max_attempts=1)
    lease = queue.claim_next(
        "consumer-a", lease_duration=timedelta(seconds=30), now=now,
    )
    assert lease is not None

    assert queue.reap_expired(
        retry_delay=timedelta(minutes=1),
        now=now + timedelta_to_ms(timedelta(seconds=31)),
    ) == 1

    failed = jobs.get(job_id)
    assert failed.status == ParseJobStatus.FAILED.value
    assert failed.completed_at == now + timedelta_to_ms(timedelta(seconds=31))
    assert failed.processing_duration_ms == 0
    assert failed.processing_timing_complete is False


def test_claim_allows_parallel_series_but_serializes_each_series(repository) -> None:
    engine, _video_repository = repository
    session_factory = create_session_factory(engine)
    queue = MySqlParseJobQueue(session_factory)
    now = 1_789_376_400_000
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
                    duration_seconds=60,
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
                    available_at=now + timedelta_to_ms(timedelta(seconds=delay)),
                    lease_version=0,
                )
            )

    claim_at = now + timedelta_to_ms(timedelta(seconds=3))
    first = queue.claim_next(
        "worker-a", lease_duration=timedelta(minutes=2), now=claim_at,
    )
    parallel = queue.claim_next(
        "worker-b", lease_duration=timedelta(minutes=2), now=claim_at,
    )

    assert first is not None and first.job_id == first_job
    assert parallel is not None and parallel.job_id == other_job
    assert queue.claim_next(
        "worker-c", lease_duration=timedelta(minutes=2), now=claim_at,
    ) is None
    assert queue.acknowledge(first, duration_ms=0, now=claim_at + timedelta_to_ms(timedelta(seconds=1)))
    sibling = queue.claim_next(
        "worker-c",
        lease_duration=timedelta(minutes=2),
        now=claim_at + timedelta_to_ms(timedelta(seconds=2)),
    )
    assert sibling is not None and sibling.job_id == sibling_job


def test_later_upload_waits_for_earlier_video_and_its_retry(repository) -> None:
    _jobs, queue, first_job_id, first_video_id, now = _seed(repository)
    engine, _repository = repository
    session_factory = create_session_factory(engine)
    later_job_id = str(uuid4())
    later_video_id = str(uuid4())
    with session_factory.begin() as session:
        first_video = session.get(VideoRecord, first_video_id)
        session.add(
            VideoRecord(
                video_id=later_video_id,
                series_id=first_video.series_id,
                episode_order=2,
                upload_sequence=2,
                original_filename="later.mp4",
                original_source_uri="artifact://test/later.mp4",
                content_hash="b" * 64,
                total_bytes=100,
                duration_seconds=60,
                upload_status="COMPLETED",
                parse_status="QUEUED",
                upload_progress=100,
                parse_progress=0,
            )
        )
        session.add(
            SeriesParseJobRecord(
                job_id=later_job_id,
                series_id=first_video.series_id,
                video_id=later_video_id,
                pipeline_version="pipeline-v1",
                status="QUEUED",
                stage="QUEUED",
                progress=0,
                attempt_count=0,
                max_attempts=3,
                lease_version=0,
                available_at=now - timedelta_to_ms(timedelta(minutes=1)),
            )
        )

    first = queue.claim_next(
        "worker-a", lease_duration=timedelta(minutes=2), now=now,
    )
    assert first is not None and first.job_id == first_job_id
    retry_at = now + timedelta_to_ms(timedelta(minutes=5))
    assert queue.retry(
        first, error="temporary error", retry_at=retry_at, duration_ms=0, now=now,
    )
    assert queue.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=2),
        now=now + timedelta_to_ms(timedelta(seconds=1)),
    ) is None
    retry = queue.claim_next(
        "worker-b", lease_duration=timedelta(minutes=2), now=retry_at,
    )
    assert retry is not None and retry.job_id == first_job_id
    assert queue.acknowledge(retry, duration_ms=0, now=retry_at)
    later = queue.claim_next(
        "worker-c", lease_duration=timedelta(minutes=2), now=retry_at,
    )
    assert later is not None and later.job_id == later_job_id


@pytest.mark.parametrize("earlier_status", ["FAILED", "RUNNING", "QUEUED"])
def test_earlier_unfinished_video_without_due_job_blocks_later_parse(
    repository,
    earlier_status,
) -> None:
    jobs, queue, job_id, video_id, now = _seed(repository)
    engine, _repository = repository
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        candidate = session.get(VideoRecord, video_id)
        candidate.episode_order = 2
        candidate.upload_sequence = 2
        session.add(
            VideoRecord(
                video_id=str(uuid4()),
                series_id=candidate.series_id,
                episode_order=1,
                upload_sequence=1,
                original_filename="earlier.mp4",
                original_source_uri="artifact://test/earlier.mp4",
                content_hash="b" * 64,
                total_bytes=100,
                duration_seconds=60,
                upload_status="COMPLETED",
                parse_status=earlier_status,
                upload_progress=100,
                parse_progress=0,
            )
        )
    assert queue.claim_next(
        "worker", lease_duration=timedelta(minutes=2), now=now,
    ) is None
    assert jobs.get(job_id).status == ParseJobStatus.QUEUED.value


@pytest.mark.parametrize(
    "upload_status",
    [UploadSessionStatus.UPLOADING, UploadSessionStatus.COMPLETING],
)
def test_earlier_upload_without_job_blocks_later_parse_until_expired(
    repository,
    upload_status,
) -> None:
    jobs, queue, job_id, video_id, now = _seed(repository)
    engine, _repository = repository
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        candidate = session.get(VideoRecord, video_id)
        candidate.episode_order = 2
        candidate.upload_sequence = 2
        session.add(
            UploadSessionRecord(
                upload_session_id=str(uuid4()),
                workspace_id="local-default",
                series_id=candidate.series_id,
                episode_order=1,
                upload_sequence=1,
                original_filename="earlier.mp4",
                media_type="video/mp4",
                total_size_bytes=100,
                preferred_segment_size_bytes=100,
                total_segments=1,
                status=upload_status.value,
                idempotency_key="earlier-upload",
                expires_at=now + timedelta_to_ms(timedelta(minutes=1)),
            )
        )
    assert queue.claim_next(
        "worker", lease_duration=timedelta(minutes=2), now=now,
    ) is None
    assert jobs.get(job_id).status == ParseJobStatus.QUEUED.value
    lease = queue.claim_next(
        "worker",
        lease_duration=timedelta(minutes=2),
        now=now + timedelta_to_ms(timedelta(minutes=1)),
    )
    assert lease is not None and lease.job_id == job_id
