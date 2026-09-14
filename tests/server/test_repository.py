from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import pytest
from sqlalchemy import event, func, inspect, select, update

from server.model.index_sync import IndexSyncIntentRecord
from server.model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoRecord,
    VideoSegmentInfoRecord,
    VideoSegmentParseChunkRecord,
    VideoSegmentRecord,
)
from server.repository.video import VideoSegmentNotFoundError
from server.service.parse_import import EmbeddingArtifact, prepare_video_parse_import


def _table_counts(engine) -> dict[str, int]:
    table_by_name = {
        model.__tablename__: model
        for model in (
            SeriesRecord,
            VideoRecord,
            VideoSegmentRecord,
            VideoSegmentInfoRecord,
            ContextSnapshotRecord,
            VideoSegmentParseChunkRecord,
            IndexSyncIntentRecord,
        )
    }
    with engine.connect() as connection:
        return {
            name: connection.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            for name, model in table_by_name.items()
        }


def test_import_is_idempotent_and_maps_video_compact(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository

    first = video_repository.import_video_parse(prepared_import)
    second = video_repository.import_video_parse(prepared_import)

    assert first == second
    assert first.segment_count == 2
    assert first.vectors_imported is False
    assert len(set(first.video_segment_ids)) == 2
    assert UUID(first.video_id).version == 5
    assert all(UUID(item).version == 5 for item in first.video_segment_ids)
    assert _table_counts(engine) == {
        "series": 1,
        "video": 1,
        "video_segment": 2,
        "video_segment_info": 2,
        "context_snapshot": 2,
        "video_segment_parse_chunk": 2,
        "index_sync_intent": 2,
    }
    assert "episode" not in inspect(engine).get_table_names()

    with engine.connect() as connection:
        video_compact = connection.execute(
            select(VideoSegmentInfoRecord.video_compact).where(
                VideoSegmentInfoRecord.video_segment_id
                == first.video_segment_ids[0]
            )
        ).scalar_one()
        text_chunk, vision_chunk = connection.execute(
            select(
                VideoSegmentParseChunkRecord.text_chunk,
                VideoSegmentParseChunkRecord.vision_chunk,
            ).where(
                VideoSegmentParseChunkRecord.video_segment_id
                == first.video_segment_ids[0]
            )
        ).one()
    assert video_compact == prepared_import.segments[0].parse_result.self_compact
    assert text_chunk is None
    assert vision_chunk is None


def test_default_video_id_does_not_change_with_episode_order(
    prepared_import,
    tmp_path: Path,
) -> None:
    parse_path = tmp_path / "reloaded.parse.json"
    parse_path.write_text(
        prepared_import.parse_result.model_dump_json(),
        encoding="utf-8",
    )
    canonical_directory = Path(
        urlparse(prepared_import.segments[0].segment_media_ref).path
    ).parent

    reloaded = prepare_video_parse_import(
        parse_path,
        canonical_directory,
        series_name=prepared_import.series_name,
        episode_order=8,
        pipeline_version=prepared_import.pipeline_version,
        workspace_id=prepared_import.workspace_id,
    )

    assert reloaded.video_id == prepared_import.video_id
    assert [item.video_segment_id for item in reloaded.segments] == [
        item.video_segment_id for item in prepared_import.segments
    ]


def test_repeat_import_preserves_enrichment_metadata_and_cached_vectors(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    prepared_with_vectors = replace(
        prepared_import,
        segments=tuple(
            replace(
                item,
                text_embedding=EmbeddingArtifact(
                    vector=[0.1, 0.2],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=EmbeddingArtifact(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for item in prepared_import.segments
        ),
    )
    video_repository.import_video_parse(prepared_with_vectors)

    first_segment_id = prepared_import.segments[0].video_segment_id
    with engine.begin() as connection:
        connection.execute(
            update(SeriesRecord)
            .where(SeriesRecord.series_id == prepared_import.series_id)
            .values(
                description="curated description",
                status="READY",
                important_extra_data={"world": "curated"},
                character_bible=[{"name": "Anon"}],
            )
        )
        connection.execute(
            update(VideoRecord)
            .where(VideoRecord.video_id == prepared_import.video_id)
            .values(
                inference_proxy_uri="s3://artifacts/proxy.mp4",
                upload_status="PUBLISHED",
                upload_progress=73,
            )
        )
        connection.execute(
            update(VideoSegmentRecord)
            .where(VideoSegmentRecord.video_segment_id == first_segment_id)
            .values(
                segment_media_ref="s3://artifacts/segment.mp4",
                analysis_window_media_ref="s3://artifacts/window.mp4",
                baseline_visual_caption="curated visual caption",
                keyframe_uris=["s3://artifacts/keyframe.webp"],
            )
        )

    video_repository.import_video_parse(prepared_import)

    with engine.connect() as connection:
        series = connection.execute(
            select(
                SeriesRecord.description,
                SeriesRecord.status,
                SeriesRecord.important_extra_data,
                SeriesRecord.character_bible,
            ).where(SeriesRecord.series_id == prepared_import.series_id)
        ).one()
        video = connection.execute(
            select(
                VideoRecord.inference_proxy_uri,
                VideoRecord.upload_status,
                VideoRecord.upload_progress,
            ).where(VideoRecord.video_id == prepared_import.video_id)
        ).one()
        segment = connection.execute(
            select(
                VideoSegmentRecord.segment_media_ref,
                VideoSegmentRecord.analysis_window_media_ref,
                VideoSegmentRecord.baseline_visual_caption,
                VideoSegmentRecord.keyframe_uris,
            ).where(VideoSegmentRecord.video_segment_id == first_segment_id)
        ).one()
        chunk = connection.execute(
            select(
                VideoSegmentParseChunkRecord.text_chunk,
                VideoSegmentParseChunkRecord.vision_chunk,
                VideoSegmentParseChunkRecord.text_embedding_metadata,
                VideoSegmentParseChunkRecord.vision_embedding_metadata,
            ).where(
                VideoSegmentParseChunkRecord.video_segment_id == first_segment_id
            )
        ).one()

    assert series == (
        "curated description",
        "INDEXING",
        {"world": "curated"},
        [{"name": "Anon"}],
    )
    assert video == ("s3://artifacts/proxy.mp4", "PUBLISHED", 73)
    assert segment == (
        "s3://artifacts/segment.mp4",
        prepared_import.segments[0].analysis_window_media_ref,
        "curated visual caption",
        ["s3://artifacts/keyframe.webp"],
    )
    assert chunk == (
        [0.1, 0.2],
        [0.3, 0.4],
        {"model": "text-v1"},
        {"model": "vision-v1"},
    )


def test_repeat_import_clears_a_stale_text_vector(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    first_segment = prepared_import.segments[0]
    prepared_with_vectors = replace(
        prepared_import,
        segments=tuple(
            replace(
                item,
                text_embedding=EmbeddingArtifact(
                    vector=[0.1, 0.2],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=EmbeddingArtifact(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for item in prepared_import.segments
        ),
    )
    video_repository.import_video_parse(prepared_with_vectors)

    changed_text = first_segment.retrieval_chunk.text + "\nnew evidence"
    changed_segment = replace(
        first_segment,
        retrieval_chunk=first_segment.retrieval_chunk.model_copy(
            update={"text": changed_text}
        ),
    )
    video_repository.import_video_parse(
        replace(
            prepared_import,
            segments=(changed_segment, *prepared_import.segments[1:]),
        )
    )

    with engine.connect() as connection:
        chunk = connection.execute(
            select(
                VideoSegmentParseChunkRecord.text_input,
                VideoSegmentParseChunkRecord.text_chunk,
                VideoSegmentParseChunkRecord.text_embedding_metadata,
                VideoSegmentParseChunkRecord.vision_chunk,
                VideoSegmentParseChunkRecord.vision_embedding_metadata,
            ).where(
                VideoSegmentParseChunkRecord.video_segment_id
                == first_segment.video_segment_id
            )
        ).one()

    assert chunk == (
        changed_text,
        None,
        None,
        [0.3, 0.4],
        {"model": "vision-v1"},
    )


def test_hydrate_preserves_candidate_order_with_one_select(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    report = video_repository.import_video_parse(prepared_import)
    select_count = 0

    def count_selects(_connection, _cursor, statement, *_args) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        evidence = video_repository.hydrate_video_segments(
            list(reversed(report.video_segment_ids))
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert select_count == 1
    assert [item.video_segment_id for item in evidence] == list(
        reversed(report.video_segment_ids)
    )
    assert [item.sequence_no for item in evidence] == [1, 0]
    assert evidence[0].series_name == "Test Series"
    assert evidence[0].episode_order == 7
    assert evidence[0].model_dump(by_alias=True)["videoCompact"]
    assert "textChunk" not in evidence[0].model_dump(by_alias=True)


def test_hydrate_rejects_a_stale_candidate_id(
    prepared_import,
    repository,
) -> None:
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)

    with pytest.raises(VideoSegmentNotFoundError, match="missing from MySQL"):
        video_repository.hydrate_video_segments(
            ["00000000-0000-0000-0000-000000000000"]
        )


def test_import_rolls_back_the_whole_video_when_a_late_write_fails(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository

    def fail_second_snapshot(_mapper, _connection, target) -> None:
        if target.after_segment_sequence_no == 1:
            raise RuntimeError("injected late persistence failure")

    event.listen(ContextSnapshotRecord, "before_insert", fail_second_snapshot)
    try:
        with pytest.raises(RuntimeError, match="injected late persistence failure"):
            video_repository.import_video_parse(prepared_import)
    finally:
        event.remove(ContextSnapshotRecord, "before_insert", fail_second_snapshot)

    assert all(count == 0 for count in _table_counts(engine).values())


def test_import_rejects_an_empty_segment_snapshot_before_writing(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository

    with pytest.raises(ValueError, match="at least one Segment"):
        video_repository.import_video_parse(
            replace(prepared_import, segments=())
        )

    assert all(count == 0 for count in _table_counts(engine).values())


def test_import_rejects_a_different_segment_snapshot_for_the_same_video(
    prepared_import,
    repository,
) -> None:
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)
    changed_segment = replace(
        prepared_import.segments[0],
        video_segment_id="00000000-0000-0000-0000-000000000001",
    )
    changed_import = replace(
        prepared_import,
        segments=(changed_segment, *prepared_import.segments[1:]),
    )

    with pytest.raises(RuntimeError, match="different canonical Segment snapshot"):
        video_repository.import_video_parse(changed_import)
