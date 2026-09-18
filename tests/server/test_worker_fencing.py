from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from server.database import create_session_factory
from server.model.job import ParseJobStage, ParseJobStatus, SeriesParseJobRecord
from server.model.video import SeriesRecord, VideoChunkRecord, VideoRecord
from server.mq.contracts import ParseJobLeaseLostError
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.video import ParseImportConflictError, VideoRepository
from server.model.dto.parseDTO import PreparedEmbedding
from server.mq.mysql_queue import MySqlParseJobQueue
from server.utils.time import now_unix_time_ms, timedelta_to_ms


def test_stale_worker_cannot_commit_parse_facts(repository, prepared_import) -> None:
    engine, video_repository = repository
    session_factory = create_session_factory(engine)
    prepared = replace(
        prepared_import,
        chunks=tuple(
            replace(
                chunk,
                text_embedding=PreparedEmbedding(
                    vector=[1.0, 0.0],
                    metadata={"modality": "text"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.0, 1.0],
                    metadata={"modality": "video"},
                ),
            )
            for chunk in prepared_import.chunks
        ),
    )
    now = now_unix_time_ms()
    job_id = "d1070c86-b716-4df7-bbb6-83072c7e9aea"
    with session_factory.begin() as session:
        session.add(
            SeriesRecord(
                series_id=prepared.series_id,
                workspace_id=prepared.workspace_id,
                name=prepared.series_name,
                description=prepared.series_description,
                status="INDEXING",
                important_extra_data=prepared.important_extra_data,
                character_bible=prepared.character_bible,
            )
        )
        session.add(
            VideoRecord(
                video_id=prepared.video_id,
                series_id=prepared.series_id,
                episode_order=prepared.episode_order,
                upload_sequence=prepared.upload_sequence,
                original_filename=prepared.original_filename,
                original_source_uri=prepared.original_source_uri,
                inference_proxy_uri=None,
                content_hash=prepared.content_hash,
                total_bytes=prepared.total_bytes,
                duration_ms=prepared.parse_result.duration_ms,
                upload_status="COMPLETED",
                parse_status="QUEUED",
                upload_progress=100,
                parse_progress=0,
            )
        )
        session.add(
            SeriesParseJobRecord(
                job_id=job_id,
                series_id=prepared.series_id,
                video_id=prepared.video_id,
                pipeline_version=prepared.pipeline_version,
                status=ParseJobStatus.QUEUED.value,
                stage=ParseJobStage.QUEUED.value,
                progress=0,
                attempt_count=0,
                max_attempts=3,
                available_at=now,
                lease_version=0,
            )
        )
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    first = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=5),
        now=now,
    )
    assert first is not None
    retry_at = now + timedelta_to_ms(timedelta(seconds=1))
    assert queue.retry(
        first,
        error="handoff",
        retry_at=retry_at,
        duration_ms=0,
        now=now,
    )
    second = queue.claim_next(
        "worker-b",
        lease_duration=timedelta(minutes=5),
        now=retry_at,
    )
    assert second is not None
    assert second.episode_order == prepared.episode_order

    LibraryRepository(session_factory).update_video_episode_order(
        prepared.video_id,
        workspace_id=prepared.workspace_id,
        episode_order=prepared.episode_order + 1,
    )

    with pytest.raises(ParseJobLeaseLostError):
        video_repository.import_video_parse_for_job(prepared, first)
    with session_factory() as session:
        assert session.scalar(select(func.count(VideoChunkRecord.video_chunk_id))) == 0

    report = VideoRepository(session_factory).import_video_parse_for_job(
        prepared,
        second,
    )

    assert report.chunk_count == len(prepared.chunks)
    assert jobs.get(job_id).stage == ParseJobStage.INDEXING.value
    with session_factory() as session:
        assert session.get(VideoRecord, prepared.video_id).episode_order == (
            prepared.episode_order + 1
        )
    with pytest.raises(ParseImportConflictError):
        video_repository.import_video_parse_for_job(
            replace(prepared, episode_order=prepared.episode_order + 2),
            second,
        )
    with pytest.raises(ParseImportConflictError):
        video_repository.import_video_parse(prepared)
