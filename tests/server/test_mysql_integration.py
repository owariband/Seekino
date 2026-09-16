from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
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
from server.model.job import SeriesParseJobRecord
from server.model.parse_usage import ParseModelCallRecord, ParseUsageAttemptRecord
from server.model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSessionRecord,
    UploadSessionStatus,
)
from server.mq.mysql_queue import MySqlParseJobQueue
from server.mq.contracts import ParseJobLeaseLostError
from server.repository.job import ParseJobRepository
from server.repository.index_sync import IndexSyncRepository
from server.model.dto.jobDTO import VideoChunkCheckpointState
from server.repository.library import LibraryRepository
from server.repository.upload import UploadConflictError, UploadRepository, UploadStateError
from server.repository.video import VideoRepository
from server.service.library import LibraryService
from server.model.dto.parseDTO import PreparedEmbedding
from server.service.qdrant_mutation_lock import (
    QdrantMutationLock,
    QdrantMutationLockBusyError,
)
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore

TEST_DATABASE_URL = os.getenv("ANIVAG_TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_manual_parse_retry_is_single_delivery_under_concurrent_requests(tmp_path):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        workspace_id="mysql-manual-retry",
        name=f"Manual Retry {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        store,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-manual-retry-v1",
            max_job_attempts=3,
        ),
        probe_duration=lambda _path: 10_000,
    )
    upload = uploads.create_session(
        workspace_id="mysql-manual-retry",
        series_id=series.series_id,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=4,
        idempotency_key="manual-retry",
    )
    uploads.upload_segment(
        upload.upload_session_id,
        0,
        b"abcd",
        workspace_id="mysql-manual-retry",
        checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
        start_byte=0,
        end_byte_exclusive=4,
        total_size_bytes=4,
    )
    completed = uploads.complete(
        upload.upload_session_id,
        workspace_id="mysql-manual-retry",
    )
    with session_factory.begin() as session:
        job = session.get(SeriesParseJobRecord, completed.job_id)
        job.status = "FAILED"
        job.attempt_count = 3
        job.max_attempts = 3
        job.version = 7
        job.last_error = "failed"
        session.get(VideoRecord, completed.video_id).parse_status = "FAILED"
        session.get(SeriesRecord, series.series_id).status = "FAILED"

    barrier = Barrier(2)

    def retry_once():
        barrier.wait()
        try:
            return uploads.retry_parse(
                upload.upload_session_id,
                workspace_id="mysql-manual-retry",
                expected_job_version=7,
            )
        except UploadConflictError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(retry_once) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert sum(result == completed.job_id for result in results) == 1
        assert sum(isinstance(result, UploadConflictError) for result in results) == 1
        with session_factory() as session:
            job = session.get(SeriesParseJobRecord, completed.job_id)
            assert (job.status, job.attempt_count, job.max_attempts, job.version) == (
                "QUEUED", 3, 4, 8,
            )
    finally:
        with session_factory.begin() as session:
            session.execute(delete(SeriesRecord).where(SeriesRecord.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_qdrant_mutation_lock_is_shared_across_connections():
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    holder = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_lock_test",
    )
    contender = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_lock_test",
        acquire_timeout_seconds=0,
    )

    try:
        with holder.hold(), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_try_qdrant_mutation_lock, contender)
            with pytest.raises(QdrantMutationLockBusyError, match="retry"):
                future.result(timeout=2)
        with contender.hold():
            pass
    finally:
        engine.dispose()


def _try_qdrant_mutation_lock(lock: QdrantMutationLock) -> None:
    with lock.hold():
        pass


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_index_sync_hides_video_while_upload_deletion_is_pending(
    prepared_import,
) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    prepared = replace(
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
        connection.execute(delete(SeriesRecord).where(
            SeriesRecord.series_id == prepared.series_id
        ))

    try:
        VideoRepository(session_factory).import_video_parse(prepared)
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_factory.begin() as session:
            session.add(UploadSessionRecord(
                upload_session_id=str(uuid4()),
                workspace_id=prepared.workspace_id,
                series_id=prepared.series_id,
                episode_order=prepared.episode_order,
                upload_sequence=prepared.upload_sequence,
                original_filename=prepared.original_filename,
                media_type="video/mp4",
                total_size_bytes=prepared.total_bytes,
                preferred_segment_size_bytes=prepared.total_bytes,
                total_segments=1,
                content_sha256=prepared.content_hash,
                version=1,
                status=UploadSessionStatus.EXPIRED.value,
                idempotency_key="mysql-pending-delete-crash",
                expires_at=now,
                completed_at=now,
                video_id=prepared.video_id,
                last_error_code=UPLOAD_DELETION_PENDING_ERROR_CODE,
            ))

        index_repository = IndexSyncRepository(session_factory)
        assert index_repository.materialize_all() == 0
        assert index_repository.list_all() == []
    finally:
        with engine.begin() as connection:
            connection.execute(delete(SeriesRecord).where(
                SeriesRecord.series_id == prepared.series_id
            ))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_delete_fences_in_flight_upload_and_preserves_order(tmp_path, monkeypatch):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        workspace_id="mysql-delete", name=f"Upload Delete {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(UploadRepository(session_factory), store,
        config=UploadServiceConfig(preferred_segment_size_bytes=4, maximum_upload_size_bytes=1024))
    entered = Event()
    release = Event()
    put = store.put_upload_segment

    def delayed_put(*args):
        entered.set()
        assert release.wait(10)
        return put(*args)

    try:
        upload = uploads.create_session(
            workspace_id="mysql-delete", series_id=series.series_id,
            original_filename="episode.mp4", media_type="video/mp4",
            total_size_bytes=4, idempotency_key="first",
        )
        monkeypatch.setattr(store, "put_upload_segment", delayed_put)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(uploads.upload_segment,
                upload.upload_session_id, 0, b"abcd", workspace_id="mysql-delete",
                checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
                start_byte=0, end_byte_exclusive=4, total_size_bytes=4)
            try:
                assert entered.wait(10)
                assert uploads.delete_session(upload.upload_session_id, workspace_id="mysql-delete").status == "EXPIRED"
            finally:
                release.set()
            with pytest.raises(UploadStateError, match="expired"):
                future.result(timeout=10)
        assert not (store.base_directory / "staging" / "uploads" / upload.upload_session_id).exists()
        assert uploads.list_sessions(workspace_id="mysql-delete") == []
        next_upload = uploads.create_session(
            workspace_id="mysql-delete", series_id=series.series_id,
            original_filename="next.mp4", media_type="video/mp4",
            total_size_bytes=4, idempotency_key="next",
        )
        assert next_upload.episode_order == next_upload.upload_sequence == 2
    finally:
        release.set()
        with session_factory.begin() as session:
            session.execute(delete(SeriesRecord).where(SeriesRecord.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_delete_completed_upload_removes_video_job_and_usage(tmp_path):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        workspace_id="mysql-completed-delete",
        name=f"Completed Upload Delete {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        store,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-delete-v1",
        ),
        probe_duration=lambda _path: 10_000,
    )
    upload = uploads.create_session(
        workspace_id="mysql-completed-delete",
        series_id=series.series_id,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=4,
        idempotency_key="completed-delete",
    )
    uploads.upload_segment(
        upload.upload_session_id,
        0,
        b"abcd",
        workspace_id="mysql-completed-delete",
        checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
        start_byte=0,
        end_byte_exclusive=4,
        total_size_bytes=4,
    )
    completed = uploads.complete(
        upload.upload_session_id,
        workspace_id="mysql-completed-delete",
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    call_id = str(uuid4())
    with session_factory.begin() as session:
        session.add(ParseUsageAttemptRecord(
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            started_at=now,
            recording_complete=True,
        ))
        session.add(ParseModelCallRecord(
            call_id=call_id,
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            workspace_id="mysql-completed-delete",
            series_id=series.series_id,
            video_id=completed.video_id,
            provider="test",
            model="test-model",
            category="parse",
            operation="parse",
            started_at=now,
            status="failed",
        ))

    try:
        deleted = uploads.delete_session(
            upload.upload_session_id,
            workspace_id="mysql-completed-delete",
        )
        repeated = uploads.delete_session(
            upload.upload_session_id,
            workspace_id="mysql-completed-delete",
        )
        assert deleted.status == UploadSessionStatus.EXPIRED.value
        assert repeated.version == deleted.version
        with session_factory() as session:
            tombstone = session.get(UploadSessionRecord, upload.upload_session_id)
            assert tombstone.video_id is None and tombstone.job_id is None
            assert tombstone.last_error_code is None
            assert session.get(VideoRecord, completed.video_id) is None
            assert session.get(SeriesParseJobRecord, completed.job_id) is None
            assert session.get(ParseUsageAttemptRecord, (completed.job_id, 1, 1)) is None
            assert session.get(ParseModelCallRecord, call_id) is None
    finally:
        with session_factory.begin() as session:
            session.execute(delete(SeriesRecord).where(SeriesRecord.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_allocates_upload_order_under_concurrent_requests(tmp_path) -> None:
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        workspace_id="mysql-order", name=f"Concurrent Upload Order {uuid4()}",
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"),
    )
    barrier = Barrier(4)

    def create(number: int):
        barrier.wait()
        return uploads.create_session(
            workspace_id="mysql-order", series_id=series.series_id,
            episode_order=999, upload_sequence=999,
            original_filename=f"episode-999-{number}.mp4", media_type="video/mp4",
            total_size_bytes=100, idempotency_key=f"upload-{number}",
        )

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            uploads_created = list(executor.map(create, range(4)))
        assert sorted(item.episode_order for item in uploads_created) == [1, 2, 3, 4]
        assert all(item.episode_order == item.upload_sequence for item in uploads_created)
        replay = uploads.create_session(
            workspace_id="mysql-order", series_id=series.series_id,
            original_filename="episode-999-0.mp4", media_type="video/mp4",
            total_size_bytes=100, idempotency_key="upload-0",
        )
        assert replay.upload_session_id == uploads_created[0].upload_session_id
        assert replay.episode_order == uploads_created[0].episode_order
    finally:
        with engine.begin() as connection:
            connection.execute(delete(SeriesRecord).where(SeriesRecord.series_id == series.series_id))
        engine.dispose()


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
    queue = MySqlParseJobQueue(session_factory)
    # MySQL DATETIME stores whole seconds; claim after the completed uploads.
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return queue.claim_next(
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
        assert first.job_id == completed.job_id
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
        assert queue.retry(
            first,
            error="retry integration",
            retry_at=retry_at,
            now=now,
        )
        sibling = queue.claim_next(
            "worker-c",
            lease_duration=timedelta(minutes=2),
            now=now + timedelta(seconds=1),
        )
        assert sibling is None
        second = queue.claim_next(
            "worker-d",
            lease_duration=timedelta(minutes=2),
            now=retry_at,
        )
        assert second is not None
        assert second.job_id == first.job_id
        assert second.lease_version == first.lease_version + 1
        with pytest.raises(ParseJobLeaseLostError):
            jobs.save_checkpoint(first, checkpoint, now=retry_at)
        assert queue.acknowledge(second, now=retry_at)
        sibling = queue.claim_next(
            "worker-e", lease_duration=timedelta(minutes=2), now=retry_at,
        )
        assert sibling is not None and sibling.job_id == second_completed.job_id
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(SeriesRecord).where(
                    SeriesRecord.series_id == series.series_id
                )
            )
        engine.dispose()
