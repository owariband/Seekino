from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoRecord,
    VideoChunkInfoRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkRecord,
)
from server.repository.job import (
    ParseJobLeaseLostError,
    ParseJobRepository,
    VideoChunkCheckpointState,
)
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.repository.video import VideoRepository
from server.service.library import LibraryService
from server.service.parse_import import PreparedEmbedding
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore

TEST_DATABASE_URL = os.getenv("ANIVAG_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_mysql_import_hydrate_and_late_failure_rollback(prepared_import) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    repository = VideoRepository(create_session_factory(engine))
    failed_series_id = str(uuid4())
    failed_video_id = str(uuid4())
    failed_chunks = tuple(
        replace(
            item,
            video_chunk_id=str(uuid4()),
            snapshot_id=str(uuid4()),
        )
        for item in prepared_import.chunks
    )
    failed_import = replace(
        prepared_import,
        series_id=failed_series_id,
        series_name=f"Failed Test Series {failed_series_id}",
        video_id=failed_video_id,
        chunks=failed_chunks,
    )
    prepared_with_vectors = replace(
        prepared_import,
        chunks=tuple(
            replace(
                item,
                text_embedding=PreparedEmbedding(
                    vector=[0.1, 0.2],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for item in prepared_import.chunks
        ),
    )

    with engine.begin() as connection:
        connection.execute(
            delete(SeriesRecord).where(
                SeriesRecord.series_id.in_(
                    [prepared_import.series_id, failed_series_id]
                )
            )
        )

    try:
        start_barrier = Barrier(2)

        def import_concurrently():
            start_barrier.wait()
            return repository.import_video_parse(prepared_with_vectors)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(import_concurrently)
            second_future = executor.submit(import_concurrently)
            first = first_future.result()
            second = second_future.result()
        assert first == second
        assert first.vectors_imported is True

        chunk_ids = first.video_chunk_ids
        with engine.connect() as connection:
            counts = (
                connection.execute(
                    select(func.count()).select_from(SeriesRecord).where(
                        SeriesRecord.series_id == prepared_import.series_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoRecord).where(
                        VideoRecord.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoChunkRecord).where(
                        VideoChunkRecord.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkInfoRecord)
                    .where(
                        VideoChunkInfoRecord.video_chunk_id.in_(chunk_ids)
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(ContextSnapshotRecord).where(
                        ContextSnapshotRecord.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkEmbeddingRecord)
                    .where(
                        VideoChunkEmbeddingRecord.video_chunk_id.in_(
                            chunk_ids
                        )
                    )
                ).scalar_one(),
            )
            compact, text_input, text_vector, vision_vector = connection.execute(
                select(
                    VideoChunkInfoRecord.video_compact,
                    VideoChunkEmbeddingRecord.text_input,
                    VideoChunkEmbeddingRecord.text_vector,
                    VideoChunkEmbeddingRecord.vision_vector,
                ).join(
                    VideoChunkEmbeddingRecord,
                    VideoChunkEmbeddingRecord.video_chunk_id
                    == VideoChunkInfoRecord.video_chunk_id,
                ).where(
                    VideoChunkInfoRecord.video_chunk_id == chunk_ids[0]
                )
            ).one()
        assert counts == (1, 1, 2, 2, 2, 2)
        assert "动作" in compact
        assert text_input == (
            prepared_with_vectors.chunks[0].retrieval_document.text
        )
        assert text_vector == [0.1, 0.2]
        assert vision_vector == [0.3, 0.4]

        select_count = 0

        def count_selects(_connection, _cursor, statement, *_args) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            evidence = repository.hydrate_video_chunks(
                list(reversed(first.video_chunk_ids))
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
        assert select_count == 1
        assert [item.sequence_no for item in evidence] == [1, 0]
        assert evidence[0].retrieval_text == (
            prepared_with_vectors.chunks[1].retrieval_document.text
        )

        def fail_second_snapshot(_mapper, _connection, target) -> None:
            if target.after_chunk_sequence_no == 1:
                raise RuntimeError("injected MySQL persistence failure")

        event.listen(ContextSnapshotRecord, "before_insert", fail_second_snapshot)
        try:
            with pytest.raises(
                RuntimeError,
                match="injected MySQL persistence failure",
            ):
                repository.import_video_parse(failed_import)
        finally:
            event.remove(
                ContextSnapshotRecord,
                "before_insert",
                fail_second_snapshot,
            )

        with engine.connect() as connection:
            failed_counts = (
                connection.execute(
                    select(func.count()).select_from(SeriesRecord).where(
                        SeriesRecord.series_id == failed_series_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoRecord).where(
                        VideoRecord.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoChunkRecord).where(
                        VideoChunkRecord.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkInfoRecord)
                    .where(
                        VideoChunkInfoRecord.video_chunk_id.in_(
                            [item.video_chunk_id for item in failed_chunks]
                        )
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(ContextSnapshotRecord).where(
                        ContextSnapshotRecord.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkEmbeddingRecord)
                    .where(
                        VideoChunkEmbeddingRecord.video_chunk_id.in_(
                            [item.video_chunk_id for item in failed_chunks]
                        )
                    )
                ).scalar_one(),
            )
        assert failed_counts == (0, 0, 0, 0, 0, 0)
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(SeriesRecord).where(
                    SeriesRecord.series_id.in_(
                        [prepared_import.series_id, failed_series_id]
                    )
                )
            )


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_mysql_chunked_upload_claim_and_lease_fencing(tmp_path) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    unique_name = f"Upload Integration {uuid4()}"
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        workspace_id="mysql-integration",
        name=unique_name,
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        artifacts,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-upload-v1",
        ),
        probe_duration=lambda _path: 20_000,
    )
    source = b"abcdefghij"
    upload = uploads.create_session(
        workspace_id="mysql-integration",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=len(source),
        idempotency_key="mysql-upload",
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        start = number * 4
        uploads.upload_segment(
            upload.upload_session_id,
            number,
            data,
            workspace_id="mysql-integration",
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            start_byte=start,
            end_byte_exclusive=start + len(data),
            total_size_bytes=len(source),
        )
    completed = uploads.complete(
        upload.upload_session_id,
        workspace_id="mysql-integration",
    )
    second_source = b"klmnopqrst"
    second_upload = uploads.create_session(
        workspace_id="mysql-integration",
        series_id=series.series_id,
        episode_order=2,
        original_filename="episode-2.mp4",
        media_type="video/mp4",
        total_size_bytes=len(second_source),
        idempotency_key="mysql-upload-2",
    )
    for number, data in enumerate((b"klmn", b"opqr", b"st")):
        start = number * 4
        uploads.upload_segment(
            second_upload.upload_session_id,
            number,
            data,
            workspace_id="mysql-integration",
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            start_byte=start,
            end_byte_exclusive=start + len(data),
            total_size_bytes=len(second_source),
        )
    second_completed = uploads.complete(
        second_upload.upload_session_id,
        workspace_id="mysql-integration",
    )
    jobs = ParseJobRepository(session_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return jobs.claim_next(
            worker_id,
            lease_duration=timedelta(minutes=2),
            now=now,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = [
                executor.submit(claim, "worker-a"),
                executor.submit(claim, "worker-b"),
            ]
            leases = [future.result() for future in claims]
        claimed = [item for item in leases if item is not None]
        assert len(claimed) == 1
        first = claimed[0]
        assert first.job_id in {completed.job_id, second_completed.job_id}
        checkpoint = jobs.save_checkpoint(
            first,
            VideoChunkCheckpointState(
                job_id=first.job_id,
                sequence_no=0,
                video_chunk_id=str(uuid4()),
                stage="PARSED",
                parse_artifact_uri="artifact://test/jobs/chunk-0000.json",
            ),
            now=now,
        )
        retry_at = now + timedelta(seconds=10)
        assert jobs.mark_failed(
            first,
            error="retry integration",
            retry_at=retry_at,
            now=now,
        )
        sibling = jobs.claim_next(
            "worker-c",
            lease_duration=timedelta(minutes=2),
            now=now + timedelta(seconds=1),
        )
        assert sibling is not None
        assert sibling.job_id != first.job_id
        assert jobs.mark_succeeded(
            sibling,
            now=now + timedelta(seconds=2),
        )
        second = jobs.claim_next(
            "worker-d",
            lease_duration=timedelta(minutes=2),
            now=retry_at,
        )
        assert second is not None
        assert second.job_id == first.job_id
        assert second.lease_version == first.lease_version + 1
        with pytest.raises(ParseJobLeaseLostError):
            jobs.save_checkpoint(first, checkpoint, now=retry_at)
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(SeriesRecord).where(
                    SeriesRecord.series_id == series.series_id
                )
            )
        engine.dispose()
