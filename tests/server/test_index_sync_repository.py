from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from server.database import create_session_factory
from server.model.index_sync import (
    IndexSyncIntent,
    IndexSyncOperation,
    IndexSyncStatus,
)
from server.model.video import (
    Series,
    Video,
    VideoChunkInfo,
    VideoChunkEmbedding,
)
from server.repository.index_sync import (
    IndexSyncRepository,
    list_ready_video_ids,
    materialize_index_sync_intents,
    refresh_series_statuses,
)
from server.model.dto.parseDTO import PreparedEmbedding
from server.utils.time import now_unix_time_ms, timedelta_to_ms


def _with_vectors(prepared_import, *, first_text: list[float] | None = None):
    return replace(
        prepared_import,
        chunks=tuple(
            replace(
                item,
                text_embedding=PreparedEmbedding(
                    vector=(
                        first_text
                        if index == 0 and first_text is not None
                        else [0.1, 0.2]
                    ),
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for index, item in enumerate(prepared_import.chunks)
        ),
    )


def _series_status(engine, series_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            select(Series.status).where(Series.id == series_id)
        ).scalar_one()


def test_index_sync_intent_schema_is_mysql_compatible() -> None:
    ddl = str(
        CreateTable(IndexSyncIntent.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "ENGINE=InnoDB" in ddl
    assert "CHARSET=utf8mb4" in ddl
    assert "source_revision VARCHAR(64) NOT NULL" in ddl
    assert "generation INTEGER NOT NULL" in ddl
    assert "FOREIGN KEY(chunk_id)" in ddl


def test_materialize_starts_with_a_mysql_current_read_of_facts() -> None:
    class EmptyResult:
        def all(self):
            return []

    class CapturingSession:
        def __init__(self) -> None:
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            return EmptyResult()

        def flush(self) -> None:
            pass

    session = CapturingSession()

    assert materialize_index_sync_intents(session) == 0
    sql = str(session.statements[0].compile(dialect=mysql.dialect()))

    assert "FROM video_chunk INNER JOIN video" in sql
    assert "LEFT OUTER JOIN video_chunk_info" in sql
    assert "LEFT OUTER JOIN video_chunk_embedding" in sql
    assert "ORDER BY video_chunk.id" in sql
    assert sql.rstrip().endswith("FOR UPDATE")
    assert "index_sync_intent" not in sql


def test_import_commits_delete_intents_with_the_source_facts(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    report = video_repository.import_video_parse(prepared_import)
    index_repository = IndexSyncRepository(create_session_factory(engine))

    items = index_repository.list_all()

    assert [item.video_chunk_id for item in items] == sorted(
        report.video_chunk_ids
    )
    assert all(item.operation is IndexSyncOperation.DELETE for item in items)
    assert all(item.generation == 1 for item in items)
    assert all(item.status is IndexSyncStatus.PENDING for item in items)
    assert all(len(item.source_revision) == 64 for item in items)
    assert items[0].payload == {
        "seriesId": prepared_import.series_id,
        "videoId": prepared_import.video_id,
        "videoChunkId": items[0].video_chunk_id,
        "sourceRevision": items[0].source_revision,
        "isFallback": False,
    }
    assert _series_status(engine, prepared_import.series_id) == "INDEXING"


def test_same_desired_state_stays_synced_and_all_chunks_gate_ready(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared_with_vectors = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared_with_vectors)
    index_repository = IndexSyncRepository(create_session_factory(engine))
    original = index_repository.list_all()

    assert index_repository.mark_synced(
        original[0].video_chunk_id,
        original[0].source_revision,
        original[0].operation,
        original[0].generation,
    )
    assert _series_status(engine, prepared_import.series_id) == "INDEXING"
    assert index_repository.mark_synced(
        original[1].video_chunk_id,
        original[1].source_revision,
        original[1].operation,
        original[1].generation,
    )
    assert _series_status(engine, prepared_import.series_id) == "READY"

    video_repository.import_video_parse(prepared_with_vectors)
    repeated = index_repository.list_all()

    assert [item.source_revision for item in repeated] == [
        item.source_revision for item in original
    ]
    assert [item.generation for item in repeated] == [
        item.generation for item in original
    ]
    assert all(item.status is IndexSyncStatus.SYNCED for item in repeated)
    assert _series_status(engine, prepared_import.series_id) == "READY"


def test_unparsed_sibling_does_not_hide_ready_video(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared_with_vectors = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared_with_vectors)
    session_factory = create_session_factory(engine)
    index_repository = IndexSyncRepository(session_factory)
    for item in index_repository.list_all():
        assert index_repository.mark_synced(
            item.video_chunk_id,
            item.source_revision,
            item.operation,
            item.generation,
        )
    assert _series_status(engine, prepared_import.series_id) == "READY"

    queued_video_id = str(uuid4())
    with session_factory.begin() as session:
        session.add(
            Video(
                id=queued_video_id,
                series_id=prepared_import.series_id,
                episode_order=prepared_import.episode_order + 1,
                upload_sequence=prepared_import.upload_sequence + 1,
                filename="next-episode.mp4",
                source_uri="artifact://test/media/next-episode.mp4",
                proxy_uri=None,
                hash="f" * 64,
                size_bytes=100,
                duration_ms=60,
                upload_status="COMPLETED",
                parse_status="QUEUED",
                upload_progress=100,
                parse_progress=0,
            )
        )

    assert index_repository.materialize_all() == 0
    assert _series_status(engine, prepared_import.series_id) == "READY"
    with session_factory() as session:
        assert list_ready_video_ids(session, [prepared_import.series_id]) == [
            prepared_import.video_id
        ]

    with session_factory.begin() as session:
        queued_video = session.get(Video, queued_video_id)
        queued_video.parse_status = "FAILED"
        refresh_series_statuses(session, {prepared_import.series_id})
    assert _series_status(engine, prepared_import.series_id) == "READY"

    with session_factory.begin() as session:
        queued_video = session.get(Video, queued_video_id)
        queued_video.parse_status = "SUCCEEDED"
        refresh_series_statuses(session, {prepared_import.series_id})
        assert list_ready_video_ids(session, []) == []
        assert list_ready_video_ids(session, [prepared_import.series_id]) == [
            prepared_import.video_id
        ]
    assert _series_status(engine, prepared_import.series_id) == "READY"


def test_changed_vector_resets_only_current_intent_and_series(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(_with_vectors(prepared_import))
    index_repository = IndexSyncRepository(create_session_factory(engine))
    original = index_repository.list_all()
    target_id = prepared_import.chunks[0].video_chunk_id
    original_by_id = {item.video_chunk_id: item for item in original}
    for item in original:
        assert index_repository.mark_synced(
            item.video_chunk_id,
            item.source_revision,
            item.operation,
            item.generation,
        )

    video_repository.import_video_parse(
        _with_vectors(prepared_import, first_text=[0.9, 0.8])
    )
    changed = index_repository.list_all()
    changed_by_id = {item.video_chunk_id: item for item in changed}
    target_before = original_by_id[target_id]
    target_after = changed_by_id[target_id]

    assert target_after.source_revision != target_before.source_revision
    assert target_after.generation == target_before.generation + 1
    assert target_after.status is IndexSyncStatus.PENDING
    assert target_after.attempt_count == 0
    unchanged_ids = set(original_by_id) - {target_id}
    assert all(
        changed_by_id[item_id].source_revision
        == original_by_id[item_id].source_revision
        and changed_by_id[item_id].status is IndexSyncStatus.SYNCED
        for item_id in unchanged_ids
    )
    assert _series_status(engine, prepared_import.series_id) == "INDEXING"

    assert not index_repository.mark_synced(
        target_before.video_chunk_id,
        target_before.source_revision,
        target_before.operation,
        target_before.generation,
    )
    assert {
        item.video_chunk_id: item for item in index_repository.list_all()
    }[target_id].status is IndexSyncStatus.PENDING


def test_stale_external_result_invalidates_the_current_generation(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(_with_vectors(prepared_import))
    index_repository = IndexSyncRepository(create_session_factory(engine))
    original = index_repository.list_all()
    target_id = prepared_import.chunks[0].video_chunk_id
    original_target = {
        item.video_chunk_id: item for item in original
    }[target_id]
    for item in original:
        assert index_repository.mark_synced(
            item.video_chunk_id,
            item.source_revision,
            item.operation,
            item.generation,
        )

    video_repository.import_video_parse(
        _with_vectors(prepared_import, first_text=[0.9, 0.8])
    )
    current = {
        item.video_chunk_id: item for item in index_repository.list_all()
    }[target_id]
    assert current.generation == original_target.generation + 1
    assert index_repository.mark_synced(
        current.video_chunk_id,
        current.source_revision,
        current.operation,
        current.generation,
    )
    assert _series_status(engine, prepared_import.series_id) == "READY"

    assert not index_repository.mark_synced(
        original_target.video_chunk_id,
        original_target.source_revision,
        original_target.operation,
        original_target.generation,
    )
    invalidated_generation = index_repository.invalidate_after_stale_result(
        original_target.video_chunk_id,
        original_target.generation,
    )

    assert invalidated_generation == current.generation + 1
    invalidated = {
        item.video_chunk_id: item for item in index_repository.list_all()
    }[target_id]
    assert invalidated.generation == invalidated_generation
    assert invalidated.status is IndexSyncStatus.PENDING
    assert invalidated.synced_at is None
    assert _series_status(engine, prepared_import.series_id) == "INDEXING"
    assert not index_repository.mark_synced(
        current.video_chunk_id,
        current.source_revision,
        current.operation,
        current.generation,
    )


def test_missing_new_text_vector_materializes_delete(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared_with_vectors = _with_vectors(prepared_import)
    video_repository.import_video_parse(prepared_with_vectors)
    index_repository = IndexSyncRepository(create_session_factory(engine))
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

    by_id = {item.video_chunk_id: item for item in index_repository.list_all()}
    changed = by_id[first.video_chunk_id]
    assert changed.operation is IndexSyncOperation.DELETE
    assert changed.text_vector is None
    assert changed.vision_vector == [0.3, 0.4]
    assert changed.status is IndexSyncStatus.PENDING


def test_retry_schedule_and_conditional_state_changes(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(_with_vectors(prepared_import))
    index_repository = IndexSyncRepository(create_session_factory(engine))
    item = index_repository.list_all()[0]
    now = now_unix_time_ms()
    retry_at = now + timedelta_to_ms(timedelta(minutes=5))

    assert index_repository.mark_failed(
        item.video_chunk_id,
        item.source_revision,
        item.operation,
        item.generation,
        error="qdrant unavailable",
        available_at=retry_at,
    )
    failed = {
        current.video_chunk_id: current for current in index_repository.list_all()
    }[item.video_chunk_id]
    assert failed.status is IndexSyncStatus.RETRY_WAIT
    assert failed.attempt_count == 1
    assert item.video_chunk_id not in {
        current.video_chunk_id
        for current in index_repository.list_due(10, now=now)
    }
    assert item.video_chunk_id in {
        current.video_chunk_id
        for current in index_repository.list_due(10, now=retry_at)
    }
    assert not index_repository.mark_pending_if_current(
        item.video_chunk_id,
        "0" * 64,
        item.operation,
        item.generation,
    )
    next_generation = index_repository.mark_pending_if_current(
        item.video_chunk_id,
        item.source_revision,
        item.operation,
        item.generation,
    )
    assert next_generation == item.generation + 1
    pending = {
        current.video_chunk_id: current for current in index_repository.list_all()
    }[item.video_chunk_id]
    assert pending.status is IndexSyncStatus.PENDING
    assert pending.generation == next_generation
    assert pending.attempt_count == 0


def test_materialize_all_repairs_a_historical_database_without_intents(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(_with_vectors(prepared_import))
    index_repository = IndexSyncRepository(create_session_factory(engine))
    with engine.begin() as connection:
        connection.execute(delete(IndexSyncIntent))
        connection.execute(
            update(Series)
            .where(Series.id == prepared_import.series_id)
            .values(status="READY")
        )

    assert index_repository.materialize_all() == 2
    assert len(index_repository.list_all()) == 2
    assert _series_status(engine, prepared_import.series_id) == "INDEXING"


def test_incomplete_chunk_materializes_a_listable_delete_intent(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(_with_vectors(prepared_import))
    index_repository = IndexSyncRepository(create_session_factory(engine))
    original = index_repository.list_all()
    for item in original:
        assert index_repository.mark_synced(
            item.video_chunk_id,
            item.source_revision,
            item.operation,
            item.generation,
        )

    incomplete_id = original[0].video_chunk_id
    with engine.begin() as connection:
        connection.execute(
            delete(VideoChunkInfo).where(
                VideoChunkInfo.chunk_id == incomplete_id
            )
        )
        connection.execute(
            delete(VideoChunkEmbedding).where(
                VideoChunkEmbedding.chunk_id == incomplete_id
            )
        )

    assert index_repository.materialize_all() == 1
    due = index_repository.list_due(10)

    assert len(due) == 1
    assert due[0].video_chunk_id == incomplete_id
    assert due[0].operation is IndexSyncOperation.DELETE
    assert due[0].generation == original[0].generation + 1
    assert due[0].is_fallback is False
    assert due[0].text_vector is None
    assert due[0].vision_vector is None


def test_late_intent_failure_rolls_back_facts_and_outbox(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    insert_count = 0

    def fail_second_intent(_mapper, _connection, _target) -> None:
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("injected intent persistence failure")

    event.listen(
        IndexSyncIntent,
        "before_insert",
        fail_second_intent,
    )
    try:
        with pytest.raises(RuntimeError, match="intent persistence failure"):
            video_repository.import_video_parse(_with_vectors(prepared_import))
    finally:
        event.remove(
            IndexSyncIntent,
            "before_insert",
            fail_second_intent,
        )

    with engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(Series)
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(IndexSyncIntent)
        ).scalar_one() == 0
