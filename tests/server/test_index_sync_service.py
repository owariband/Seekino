from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import func, select

from server.database import DatabaseEnvironment, create_session_factory
from server.model.index_sync import IndexSyncIntentRecord, IndexSyncStatus
from server.model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSessionRecord,
    UploadSessionStatus,
)
from server.model.video import SeriesRecord, VideoChunkRecord
from server.repository.index_sync import IndexSyncRepository
from server.service.index_sync import IndexSyncInvariantError, IndexSyncService
from server.service.qdrant_mutation_lock import QdrantMutationLock
from server.model.dto.parseDTO import PreparedEmbedding
from server.storage.qdrant import QdrantVideoChunkStore, VideoChunkIndexPoint


def _with_vectors(prepared_import):
    return replace(
        prepared_import,
        chunks=tuple(
            replace(
                item,
                text_embedding=PreparedEmbedding(
                    vector=[0.1 + index, 0.2 + index],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.3 + index, 0.4 + index],
                    metadata={"model": "vision-v1"},
                ),
            )
            for index, item in enumerate(prepared_import.chunks)
        ),
    )


def _store(client: QdrantClient) -> QdrantVideoChunkStore:
    return QdrantVideoChunkStore(
        client,
        collection_name="video_chunks_test",
        environment=DatabaseEnvironment.TEST,
        text_dimensions=2,
        vision_dimensions=2,
    )


def _service(engine, repository, store, **kwargs) -> IndexSyncService:
    return IndexSyncService(
        repository,
        store,
        mutation_lock=QdrantMutationLock(
            engine,
            environment=DatabaseEnvironment.TEST,
            collection_name="video_chunks_test",
        ),
        **kwargs,
    )


class _ImmediateLock:
    def hold(self):
        return nullcontext()


def _series_status(engine, series_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            select(SeriesRecord.status).where(SeriesRecord.series_id == series_id)
        ).scalar_one()


def test_sync_captures_default_due_cutoff_after_materialization() -> None:
    calls: list[str] = []
    run_at = datetime.now(UTC)

    class OrderingRepository:
        def materialize_all(self) -> int:
            calls.append("materialize")
            return 0

        def list_due(self, limit: int, *, now: datetime):
            assert limit == 100
            assert now == run_at
            calls.append("list_due")
            return []

    def clock() -> datetime:
        calls.append("clock")
        return run_at

    report = IndexSyncService(
        OrderingRepository(),
        object(),
        mutation_lock=_ImmediateLock(),
        clock=clock,
    ).sync_due()

    assert calls == ["materialize", "clock", "list_due"]
    assert report.selected_count == 0


def test_sync_is_idempotent_and_reconcile_repairs_a_deleted_point(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)

    first = service.sync_due()
    repeated = service.sync_due()

    assert first.selected_count == 2
    assert first.synced_count == 2
    assert first.failed_count == 0
    assert repeated.selected_count == 0
    assert client.count(collection_name=store.collection_name, exact=True).count == 2
    assert _series_status(engine, prepared.series_id) == "READY"

    missing_id = prepared.chunks[0].video_chunk_id
    store.delete(missing_id)
    repaired = service.reconcile()

    assert repaired.inspected_count == 2
    assert repaired.consistent_count == 1
    assert repaired.repair_attempted_count == 1
    assert repaired.synced_count == 1
    assert repaired.failed_count == 0
    assert store.inspect([missing_id])[missing_id].present is True
    assert client.count(collection_name=store.collection_name, exact=True).count == 2
    assert _series_status(engine, prepared.series_id) == "READY"


def test_collection_lock_orders_a_stale_sync_before_concurrent_delete(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    sync_lock = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_concurrent_test",
    )
    delete_lock = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_concurrent_test",
    )
    delete_started = Event()
    delete_finished = Event()
    points: set[str] = set()
    deleted_id = prepared.chunks[0].video_chunk_id

    def delete_point() -> None:
        delete_started.set()
        with delete_lock.hold():
            points.discard(deleted_id)
            delete_finished.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        delete_future = None

        class Store:
            def upsert(self, point) -> None:
                nonlocal delete_future
                if point.video_chunk_id == deleted_id:
                    delete_future = executor.submit(delete_point)
                    assert delete_started.wait(1)
                    assert not delete_finished.wait(0.05)
                points.add(point.video_chunk_id)

            def delete(self, video_chunk_id: str) -> None:
                points.discard(video_chunk_id)

        report = IndexSyncService(
            index_repository,
            Store(),  # type: ignore[arg-type]
            mutation_lock=sync_lock,
        ).sync_due()
        assert delete_future is not None
        delete_future.result(timeout=1)

    assert report.synced_count == 2
    assert deleted_id not in points
    assert delete_finished.is_set()


def test_pending_upload_deletion_is_not_rematerialized_or_repaired(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)
    service.sync_due()
    chunk_ids = [item.video_chunk_id for item in prepared.chunks]
    for chunk_id in chunk_ids:
        store.delete(chunk_id)

    now = datetime.now(UTC).replace(tzinfo=None)
    with create_session_factory(engine).begin() as session:
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
            idempotency_key="pending-delete-crash",
            expires_at=now,
            completed_at=now,
            video_id=prepared.video_id,
            last_error_code=UPLOAD_DELETION_PENDING_ERROR_CODE,
        ))
        intents = list(session.scalars(select(IndexSyncIntentRecord)))
        generations = {
            intent.video_chunk_id: intent.generation for intent in intents
        }
        for intent in intents:
            intent.status = IndexSyncStatus.PENDING.value
            intent.available_at = now

    assert index_repository.materialize_all() == 0
    assert index_repository.list_all() == []
    sync_report = service.sync_due(now=now + timedelta(seconds=1))
    reconcile_report = service.reconcile()
    rebuild_report = service.rebuild()
    with pytest.raises(IndexSyncInvariantError, match="missing index intents"):
        service.sync_video_chunks(chunk_ids)

    assert sync_report.selected_count == 0
    assert reconcile_report.inspected_count == 0
    assert rebuild_report.selected_count == 0
    assert all(not state.present for state in store.inspect(chunk_ids).values())
    with create_session_factory(engine)() as session:
        raw_intents = list(session.scalars(select(IndexSyncIntentRecord)))
        assert {
            intent.video_chunk_id: intent.generation for intent in raw_intents
        } == generations
        assert all(
            intent.operation == "UPSERT"
            and intent.status == IndexSyncStatus.PENDING.value
            for intent in raw_intents
        )


def test_qdrant_outage_keeps_facts_and_retries_to_convergence(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)

    # The business transaction commits before any Qdrant call is attempted.
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    unavailable = _UnavailableStore()
    run_at = datetime.now(UTC) + timedelta(seconds=1)
    service = _service(
        engine,
        index_repository,
        unavailable,
        clock=lambda: run_at,
    )

    failed = service.sync_due(now=run_at)

    assert failed.failed_count == 2
    assert failed.synced_count == 0
    assert all(
        item.status is IndexSyncStatus.RETRY_WAIT
        and item.attempt_count == 1
        for item in index_repository.list_all()
    )
    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(VideoChunkRecord)
        ).scalar_one() == 2

    client = QdrantClient(":memory:")
    recovered = _service(engine, index_repository, _store(client)).sync_due(
        now=run_at + timedelta(seconds=5)
    )

    assert recovered.selected_count == 2
    assert recovered.synced_count == 2
    assert recovered.failed_count == 0
    assert all(
        item.status is IndexSyncStatus.SYNCED
        for item in index_repository.list_all()
    )
    assert _series_status(engine, prepared.series_id) == "READY"


def test_rebuild_restores_all_points_after_collection_loss(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)
    service.sync_due()
    client.delete_collection(store.collection_name)

    rebuilt = service.rebuild()

    assert rebuilt.selected_count == 2
    assert rebuilt.synced_count == 2
    assert rebuilt.failed_count == 0
    assert client.count(collection_name=store.collection_name, exact=True).count == 2
    assert _series_status(engine, prepared.series_id) == "READY"


def test_reconcile_replaces_a_point_with_a_stale_revision(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)
    service.sync_due()
    current = index_repository.list_all()[0]
    store.upsert(
        VideoChunkIndexPoint(
            series_id=current.series_id,
            video_id=current.video_id,
            video_chunk_id=current.video_chunk_id,
            source_revision="stale-revision",
            is_fallback=current.is_fallback,
            text_vector=current.text_vector or [],
            vision_vector=current.vision_vector or [],
        )
    )

    report = service.reconcile()

    assert report.repair_attempted_count == 1
    assert report.synced_count == 1
    assert store.inspect(
        [current.video_chunk_id]
    )[current.video_chunk_id].source_revision == current.source_revision


def test_sync_delete_removes_stale_point_when_a_vector_becomes_invalid(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)
    service.sync_due()
    first = prepared_import.chunks[0]
    video_repository.import_video_parse(
        replace(
            prepared_import,
            chunks=(
                replace(
                    first,
                    retrieval_document=first.retrieval_document.model_copy(
                        update={"text": first.retrieval_document.text + " changed"}
                    ),
                ),
                prepared_import.chunks[1],
            ),
        )
    )

    report = service.sync_due()

    assert report.selected_count == 1
    assert report.synced_count == 1
    assert store.inspect(
        [first.video_chunk_id]
    )[first.video_chunk_id].present is False
    assert _series_status(engine, prepared.series_id) == "INDEXING"


def test_reconcile_deletes_a_point_that_is_no_longer_desired(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    store = _store(client)
    service = _service(engine, index_repository, store)
    service.sync_due()
    first = prepared_import.chunks[0]
    video_repository.import_video_parse(
        replace(
            prepared_import,
            chunks=(
                replace(
                    first,
                    retrieval_document=first.retrieval_document.model_copy(
                        update={"text": first.retrieval_document.text + " changed"}
                    ),
                ),
                prepared_import.chunks[1],
            ),
        )
    )

    report = service.reconcile()

    assert report.repair_attempted_count == 1
    assert report.synced_count == 1
    assert store.inspect(
        [first.video_chunk_id]
    )[first.video_chunk_id].present is False


def test_reconcile_outage_is_persisted_for_retry(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    _service(engine, index_repository, _store(client)).sync_due()
    run_at = datetime.now(UTC)

    report = _service(
        engine,
        index_repository,
        _UnavailableStore(),
        clock=lambda: run_at,
    ).reconcile()

    assert report.inspected_count == 2
    assert report.failed_count == 2
    assert all(
        item.status is IndexSyncStatus.RETRY_WAIT
        and item.available_at == (run_at + timedelta(seconds=5)).replace(tzinfo=None)
        for item in index_repository.list_all()
    )
    assert _series_status(engine, prepared.series_id) == "INDEXING"


def test_late_old_write_invalidates_a_newer_synced_generation(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    old_item = index_repository.list_due(
        1,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )[0]
    updated = replace(
        prepared,
        chunks=tuple(
            replace(
                chunk,
                text_embedding=PreparedEmbedding(
                    vector=[9.1, 9.2],
                    metadata={"model": "text-v2"},
                ),
            )
            if chunk.video_chunk_id == old_item.video_chunk_id
            else chunk
            for chunk in prepared.chunks
        ),
    )
    client = QdrantClient(":memory:")
    actual_store = _store(client)

    def sync_new_before_old_write() -> None:
        video_repository.import_video_parse(updated)
        current = {
            item.video_chunk_id: item for item in index_repository.list_all()
        }[old_item.video_chunk_id]
        actual_store.upsert(_point_from_item(current))
        assert index_repository.mark_synced(
            current.video_chunk_id,
            current.source_revision,
            current.operation,
            current.generation,
        )

    late_store = _LateWriteStore(actual_store, sync_new_before_old_write)
    stale_run = _service(engine, index_repository, late_store).sync_due(limit=1)
    invalidated = {
        item.video_chunk_id: item for item in index_repository.list_all()
    }[old_item.video_chunk_id]

    assert stale_run.stale_result_count == 1
    assert invalidated.status is IndexSyncStatus.PENDING
    assert invalidated.generation > old_item.generation
    assert actual_store.inspect(
        [old_item.video_chunk_id]
    )[old_item.video_chunk_id].source_revision == old_item.source_revision

    recovered = _service(engine, index_repository, actual_store).sync_due(limit=10)
    current = {
        item.video_chunk_id: item for item in index_repository.list_all()
    }[old_item.video_chunk_id]
    assert recovered.synced_count >= 1
    assert current.status is IndexSyncStatus.SYNCED
    assert actual_store.inspect(
        [current.video_chunk_id]
    )[current.video_chunk_id].source_revision == current.source_revision


class _UnavailableStore:
    def upsert(self, _point) -> None:
        raise ConnectionError("Qdrant is unavailable")

    def delete(self, _video_chunk_id: str) -> None:
        raise ConnectionError("Qdrant is unavailable")

    def inspect(self, _video_chunk_ids):
        raise ConnectionError("Qdrant is unavailable")


class _LateWriteStore:
    def __init__(self, store, before_first_upsert) -> None:
        self._store = store
        self._before_first_upsert = before_first_upsert
        self._triggered = False

    def upsert(self, point) -> None:
        if not self._triggered:
            self._triggered = True
            self._before_first_upsert()
        self._store.upsert(point)

    def delete(self, video_chunk_id: str) -> None:
        self._store.delete(video_chunk_id)


def _point_from_item(item) -> VideoChunkIndexPoint:
    return VideoChunkIndexPoint(
        series_id=item.series_id,
        video_id=item.video_id,
        video_chunk_id=item.video_chunk_id,
        source_revision=item.source_revision,
        is_fallback=item.is_fallback,
        text_vector=item.text_vector or [],
        vision_vector=item.vision_vector or [],
    )
