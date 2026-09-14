from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from server.repository.video import VideoRepository
from server.service.parse_import import PreparedEmbedding

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
            compact, text_vector, vision_vector = connection.execute(
                select(
                    VideoChunkInfoRecord.video_compact,
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
