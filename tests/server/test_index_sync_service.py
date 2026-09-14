from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from qdrant_client import QdrantClient
from sqlalchemy import func, select

from server.database import DatabaseEnvironment, create_session_factory
from server.model.index_sync import IndexSyncStatus
from server.model.video import SeriesRecord, VideoSegmentRecord
from server.repository.index_sync import IndexSyncRepository
from server.service.index_sync import IndexSyncService
from server.service.parse_import import EmbeddingArtifact
from server.storage.qdrant import QdrantVideoSegmentStore, VideoSegmentIndexPoint


def _with_vectors(prepared_import):
    return replace(
        prepared_import,
        segments=tuple(
            replace(
                item,
                text_embedding=EmbeddingArtifact(
                    vector=[0.1 + index, 0.2 + index],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=EmbeddingArtifact(
                    vector=[0.3 + index, 0.4 + index],
                    metadata={"model": "vision-v1"},
                ),
            )
            for index, item in enumerate(prepared_import.segments)
        ),
    )


def _store(client: QdrantClient) -> QdrantVideoSegmentStore:
    return QdrantVideoSegmentStore(
        client,
        collection_name="video_segments_test",
        environment=DatabaseEnvironment.TEST,
        text_dimensions=2,
        vision_dimensions=2,
    )


def _series_status(engine, series_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            select(SeriesRecord.status).where(SeriesRecord.series_id == series_id)
        ).scalar_one()


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
    service = IndexSyncService(index_repository, store)

    first = service.sync_due()
    repeated = service.sync_due()

    assert first.selected_count == 2
    assert first.synced_count == 2
    assert first.failed_count == 0
    assert repeated.selected_count == 0
    assert client.count(collection_name=store.collection_name, exact=True).count == 2
    assert _series_status(engine, prepared.series_id) == "READY"

    missing_id = prepared.segments[0].video_segment_id
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
    service = IndexSyncService(
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
            select(func.count()).select_from(VideoSegmentRecord)
        ).scalar_one() == 2

    client = QdrantClient(":memory:")
    recovered = IndexSyncService(index_repository, _store(client)).sync_due(
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
    service = IndexSyncService(index_repository, store)
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
    service = IndexSyncService(index_repository, store)
    service.sync_due()
    current = index_repository.list_all()[0]
    store.upsert(
        VideoSegmentIndexPoint(
            series_id=current.series_id,
            video_id=current.video_id,
            video_segment_id=current.video_segment_id,
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
        [current.video_segment_id]
    )[current.video_segment_id].source_revision == current.source_revision


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
    service = IndexSyncService(index_repository, store)
    service.sync_due()
    first = prepared_import.segments[0]
    video_repository.import_video_parse(
        replace(
            prepared_import,
            segments=(
                replace(
                    first,
                    retrieval_chunk=first.retrieval_chunk.model_copy(
                        update={"text": first.retrieval_chunk.text + " changed"}
                    ),
                ),
                prepared_import.segments[1],
            ),
        )
    )

    report = service.sync_due()

    assert report.selected_count == 1
    assert report.synced_count == 1
    assert store.inspect(
        [first.video_segment_id]
    )[first.video_segment_id].present is False
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
    service = IndexSyncService(index_repository, store)
    service.sync_due()
    first = prepared_import.segments[0]
    video_repository.import_video_parse(
        replace(
            prepared_import,
            segments=(
                replace(
                    first,
                    retrieval_chunk=first.retrieval_chunk.model_copy(
                        update={"text": first.retrieval_chunk.text + " changed"}
                    ),
                ),
                prepared_import.segments[1],
            ),
        )
    )

    report = service.reconcile()

    assert report.repair_attempted_count == 1
    assert report.synced_count == 1
    assert store.inspect(
        [first.video_segment_id]
    )[first.video_segment_id].present is False


def test_reconcile_outage_is_persisted_for_retry(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    client = QdrantClient(":memory:")
    IndexSyncService(index_repository, _store(client)).sync_due()
    run_at = datetime.now(UTC)

    report = IndexSyncService(
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
        segments=tuple(
            replace(
                segment,
                text_embedding=EmbeddingArtifact(
                    vector=[9.1, 9.2],
                    metadata={"model": "text-v2"},
                ),
            )
            if segment.video_segment_id == old_item.video_segment_id
            else segment
            for segment in prepared.segments
        ),
    )
    client = QdrantClient(":memory:")
    actual_store = _store(client)

    def sync_new_before_old_write() -> None:
        video_repository.import_video_parse(updated)
        current = {
            item.video_segment_id: item for item in index_repository.list_all()
        }[old_item.video_segment_id]
        actual_store.upsert(_point_from_item(current))
        assert index_repository.mark_synced(
            current.video_segment_id,
            current.source_revision,
            current.operation,
            current.generation,
        )

    late_store = _LateWriteStore(actual_store, sync_new_before_old_write)
    stale_run = IndexSyncService(index_repository, late_store).sync_due(limit=1)
    invalidated = {
        item.video_segment_id: item for item in index_repository.list_all()
    }[old_item.video_segment_id]

    assert stale_run.stale_result_count == 1
    assert invalidated.status is IndexSyncStatus.PENDING
    assert invalidated.generation > old_item.generation
    assert actual_store.inspect(
        [old_item.video_segment_id]
    )[old_item.video_segment_id].source_revision == old_item.source_revision

    recovered = IndexSyncService(index_repository, actual_store).sync_due(limit=10)
    current = {
        item.video_segment_id: item for item in index_repository.list_all()
    }[old_item.video_segment_id]
    assert recovered.synced_count >= 1
    assert current.status is IndexSyncStatus.SYNCED
    assert actual_store.inspect(
        [current.video_segment_id]
    )[current.video_segment_id].source_revision == current.source_revision


class _UnavailableStore:
    def upsert(self, _point) -> None:
        raise ConnectionError("Qdrant is unavailable")

    def delete(self, _video_segment_id: str) -> None:
        raise ConnectionError("Qdrant is unavailable")

    def inspect(self, _video_segment_ids):
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

    def delete(self, video_segment_id: str) -> None:
        self._store.delete(video_segment_id)


def _point_from_item(item) -> VideoSegmentIndexPoint:
    return VideoSegmentIndexPoint(
        series_id=item.series_id,
        video_id=item.video_id,
        video_segment_id=item.video_segment_id,
        source_revision=item.source_revision,
        is_fallback=item.is_fallback,
        text_vector=item.text_vector or [],
        vision_vector=item.vision_vector or [],
    )
