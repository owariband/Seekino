from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from engine.retrieval.video_retrieval import VideoChunkRetrievalDocument
from sqlalchemy import event, func, inspect, select, update

from server.model.index_sync import IndexSyncIntentRecord
from server.model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoRecord,
    VideoChunkInfoRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkRecord,
)
from server.repository.video import (
    SeriesNotReadyError,
    SeriesScopeNotFoundError,
    VideoChunkNotFoundError,
)
from server.model.dto.parseDTO import PreparedEmbedding
from server.service.parse_import import _load_embedding, prepare_video_parse_import


def _table_counts(engine) -> dict[str, int]:
    table_by_name = {
        model.__tablename__: model
        for model in (
            SeriesRecord,
            VideoRecord,
            VideoChunkRecord,
            VideoChunkInfoRecord,
            ContextSnapshotRecord,
            VideoChunkEmbeddingRecord,
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
    assert first.chunk_count == 2
    assert first.vectors_imported is False
    assert len(set(first.video_chunk_ids)) == 2
    assert UUID(first.video_id).version == 5
    assert all(UUID(item).version == 5 for item in first.video_chunk_ids)
    report_payload = first.model_dump(by_alias=True)
    assert report_payload["videoChunkIds"] == first.video_chunk_ids
    assert report_payload["chunkCount"] == 2
    assert _table_counts(engine) == {
        "series": 1,
        "video": 1,
        "video_chunk": 2,
        "video_chunk_info": 2,
        "context_snapshot": 2,
        "video_chunk_embedding": 2,
        "index_sync_intent": 2,
    }
    assert "episode" not in inspect(engine).get_table_names()

    with engine.connect() as connection:
        video_compact = connection.execute(
            select(VideoChunkInfoRecord.video_compact).where(
                VideoChunkInfoRecord.video_chunk_id
                == first.video_chunk_ids[0]
            )
        ).scalar_one()
        text_vector, vision_vector = connection.execute(
            select(
                VideoChunkEmbeddingRecord.text_vector,
                VideoChunkEmbeddingRecord.vision_vector,
            ).where(
                VideoChunkEmbeddingRecord.video_chunk_id
                == first.video_chunk_ids[0]
            )
        ).one()
    assert video_compact == prepared_import.chunks[0].parse_result.self_compact
    assert text_vector is None
    assert vision_vector is None


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
        urlparse(prepared_import.chunks[0].chunk_media_ref).path
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
    assert [item.video_chunk_id for item in reloaded.chunks] == [
        item.video_chunk_id for item in prepared_import.chunks
    ]


def test_video_chunk_id_keeps_the_published_uuid_seed(prepared_import) -> None:
    expected = uuid5(
        NAMESPACE_URL,
        f"anivag:video-segment:{prepared_import.video_id}:0",
    )

    assert prepared_import.chunks[0].video_chunk_id == str(expected)


def test_parse_import_accepts_legacy_segment_media_names(
    prepared_import,
    tmp_path: Path,
) -> None:
    parse_path = tmp_path / "legacy-media.parse.json"
    parse_path.write_text(
        prepared_import.parse_result.model_dump_json(),
        encoding="utf-8",
    )
    canonical_directory = Path(
        urlparse(prepared_import.chunks[0].chunk_media_ref).path
    ).parent
    legacy_paths = []
    for item in prepared_import.chunks:
        current_path = Path(urlparse(item.chunk_media_ref).path)
        window = item.parse_result.window
        legacy_path = current_path.with_name(
            f"segment-{window.sequence_no:04d}.analysis-"
            f"{window.chunk_start_ms:010d}-{window.chunk_end_ms:010d}.mp4"
        )
        current_path.rename(legacy_path)
        legacy_paths.append(legacy_path)

    reloaded = prepare_video_parse_import(
        parse_path,
        canonical_directory,
        series_name=prepared_import.series_name,
        episode_order=prepared_import.episode_order,
        pipeline_version=prepared_import.pipeline_version,
        workspace_id=prepared_import.workspace_id,
    )

    assert [item.chunk_media_ref for item in reloaded.chunks] == [
        path.as_uri() for path in legacy_paths
    ]


def test_parse_import_relocates_legacy_analysis_window_paths(
    prepared_import,
    tmp_path: Path,
) -> None:
    payload = prepared_import.parse_result.model_dump(mode="json")
    relocated_directory = tmp_path / "windows"
    relocated_directory.mkdir()
    expected_paths = []
    for chunk_payload, prepared_chunk in zip(
        payload["chunks"],
        prepared_import.chunks,
        strict=True,
    ):
        window_payload = chunk_payload["window"]
        source_path = Path(prepared_chunk.parse_result.window.media_path)
        relocated_path = relocated_directory / (
            f"segment-{window_payload['sequence_no']:04d}.analysis-"
            f"{window_payload['analysis_start_ms']:010d}-"
            f"{window_payload['analysis_end_ms']:010d}.mp4"
        )
        relocated_path.write_bytes(source_path.read_bytes())
        window_payload["media_path"] = str(
            Path("/obsolete/packages/engine/outputs/windows")
            / relocated_path.name
        )
        expected_paths.append(relocated_path)

    parse_path = tmp_path / "relocated.parse.json"
    parse_path.write_text(json.dumps(payload), encoding="utf-8")
    canonical_directory = Path(
        urlparse(prepared_import.chunks[0].chunk_media_ref).path
    ).parent

    reloaded = prepare_video_parse_import(
        parse_path,
        canonical_directory,
        series_name=prepared_import.series_name,
        episode_order=prepared_import.episode_order,
        pipeline_version=prepared_import.pipeline_version,
        workspace_id=prepared_import.workspace_id,
    )

    assert [item.analysis_window_media_ref for item in reloaded.chunks] == [
        path.as_uri() for path in expected_paths
    ]


def test_legacy_embedding_metadata_is_normalized_to_video_chunk(
    tmp_path: Path,
) -> None:
    source_video = (tmp_path / "episode.mp4").resolve()
    source_video.write_bytes(b"source")
    source_stat = source_video.stat()
    chunk_media = (
        tmp_path / "outputs/engine/cache/chunk-0000.mp4"
    ).resolve()
    chunk_media.parent.mkdir(parents=True)
    chunk_media.write_bytes(b"chunk")
    chunk_stat = chunk_media.stat()
    legacy_media = (
        tmp_path / "packages/engine/outputs/cache/segment-0000.mp4"
    ).resolve()
    document = VideoChunkRetrievalDocument(
        sequence_no=0,
        start_ms=0,
        end_ms=10_000,
        video_compact="compact",
        text="retrieval text",
        is_fallback=False,
    )
    max_height = 720
    media_identity = (
        f"{source_video}\0{source_stat.st_size}\0{source_stat.st_mtime_ns}"
        f"\0{document.start_ms}\0{document.end_ms}\0{max_height}"
        f"\0{legacy_media}\0{chunk_stat.st_size}\0{chunk_stat.st_mtime_ns}"
    )
    cache_path = tmp_path / "segment-0000.video.json"
    cache_path.write_text(
        json.dumps(
            {
                "vector": [0.1, 0.2],
                "dimensions": 2,
                "modality": "video",
                "segment": {
                    "sequence_no": 0,
                    "start_ms": 0,
                    "end_ms": 10_000,
                    "max_height": max_height,
                },
                "source": {
                    "resolved_path": str(source_video),
                    "size_bytes": source_stat.st_size,
                    "mtime_ns": source_stat.st_mtime_ns,
                },
                "input_sha256": hashlib.sha256(
                    media_identity.encode("utf-8")
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    embedding = _load_embedding(
        cache_path,
        expected_modality="video",
        expected_document=document,
        source_video=source_video,
        source_stat=source_stat,
        chunk_media=chunk_media,
    )

    assert "segment" not in embedding.metadata
    assert embedding.metadata["chunk"]["sequence_no"] == 0
    assert embedding.metadata["input_sha256"] != json.loads(
        cache_path.read_text(encoding="utf-8")
    )["input_sha256"]


def test_repeat_import_preserves_enrichment_metadata_and_cached_vectors(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
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
    video_repository.import_video_parse(prepared_with_vectors)

    first_chunk_id = prepared_import.chunks[0].video_chunk_id
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
            update(VideoChunkRecord)
            .where(VideoChunkRecord.video_chunk_id == first_chunk_id)
            .values(
                chunk_media_ref="s3://artifacts/chunk.mp4",
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
        chunk_record = connection.execute(
            select(
                VideoChunkRecord.chunk_media_ref,
                VideoChunkRecord.analysis_window_media_ref,
                VideoChunkRecord.baseline_visual_caption,
                VideoChunkRecord.keyframe_uris,
            ).where(VideoChunkRecord.video_chunk_id == first_chunk_id)
        ).one()
        embedding = connection.execute(
            select(
                VideoChunkEmbeddingRecord.text_vector,
                VideoChunkEmbeddingRecord.vision_vector,
                VideoChunkEmbeddingRecord.text_embedding_metadata,
                VideoChunkEmbeddingRecord.vision_embedding_metadata,
            ).where(
                VideoChunkEmbeddingRecord.video_chunk_id == first_chunk_id
            )
        ).one()

    assert series == (
        "curated description",
        "INDEXING",
        {"world": "curated"},
        [{"name": "Anon"}],
    )
    assert video == ("s3://artifacts/proxy.mp4", "PUBLISHED", 73)
    assert chunk_record == (
        "s3://artifacts/chunk.mp4",
        prepared_import.chunks[0].analysis_window_media_ref,
        "curated visual caption",
        ["s3://artifacts/keyframe.webp"],
    )
    assert embedding == (
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
    first_chunk = prepared_import.chunks[0]
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
    video_repository.import_video_parse(prepared_with_vectors)

    changed_text = first_chunk.retrieval_document.text + "\nnew evidence"
    changed_chunk = replace(
        first_chunk,
        retrieval_document=first_chunk.retrieval_document.model_copy(
            update={"text": changed_text}
        ),
    )
    video_repository.import_video_parse(
        replace(
            prepared_import,
            chunks=(changed_chunk, *prepared_import.chunks[1:]),
        )
    )

    with engine.connect() as connection:
        embedding = connection.execute(
            select(
                VideoChunkEmbeddingRecord.text_input,
                VideoChunkEmbeddingRecord.text_vector,
                VideoChunkEmbeddingRecord.text_embedding_metadata,
                VideoChunkEmbeddingRecord.vision_vector,
                VideoChunkEmbeddingRecord.vision_embedding_metadata,
            ).where(
                VideoChunkEmbeddingRecord.video_chunk_id
                == first_chunk.video_chunk_id
            )
        ).one()

    assert embedding == (
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
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunkRecord)
            .where(VideoChunkRecord.video_chunk_id == report.video_chunk_ids[1])
            .values(
                transcript=[
                    {"text": "第二段对白", "start_ms": 1_000, "end_ms": 2_000}
                ]
            )
        )
    select_count = 0

    def count_selects(_connection, _cursor, statement, *_args) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        evidence = video_repository.hydrate_video_chunks(
            list(reversed(report.video_chunk_ids))
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert select_count == 1
    assert [item.video_chunk_id for item in evidence] == list(
        reversed(report.video_chunk_ids)
    )
    assert [item.sequence_no for item in evidence] == [1, 0]
    assert evidence[0].series_name == "Test Series"
    assert evidence[0].episode_order == 7
    assert evidence[0].transcript[0].speaker is None
    assert evidence[0].transcript[0].text == "第二段对白"
    assert evidence[0].transcript[0].start_ms == 11_000
    assert evidence[0].transcript[0].end_ms == 12_000
    assert evidence[0].retrieval_text == (
        prepared_import.chunks[1].retrieval_document.text
    )
    assert evidence[0].model_dump(by_alias=True)["videoCompact"]
    payload = evidence[0].model_dump(by_alias=True)
    assert payload["retrievalText"] == (
        prepared_import.chunks[1].retrieval_document.text
    )
    assert "textVector" not in payload


def test_hydrate_rejects_a_stale_candidate_id(
    prepared_import,
    repository,
) -> None:
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)

    with pytest.raises(VideoChunkNotFoundError, match="missing from MySQL"):
        video_repository.hydrate_video_chunks(
            ["00000000-0000-0000-0000-000000000000"]
        )


def test_ready_series_scope_is_complete_ordered_and_deduplicated(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)

    with pytest.raises(SeriesNotReadyError, match="INDEXING"):
        video_repository.require_ready_series(
            [prepared_import.series_id],
            workspace_id=prepared_import.workspace_id,
        )

    with engine.begin() as connection:
        connection.execute(
            update(SeriesRecord)
            .where(SeriesRecord.series_id == prepared_import.series_id)
            .values(status="READY")
        )
    assert video_repository.require_ready_series(
        [prepared_import.series_id, prepared_import.series_id],
        workspace_id=prepared_import.workspace_id,
    ) == [prepared_import.series_id]

    missing_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(SeriesScopeNotFoundError, match=missing_id):
        video_repository.require_ready_series(
            [prepared_import.series_id, missing_id],
            workspace_id=prepared_import.workspace_id,
        )


def test_hydrate_state_includes_current_index_revision_in_the_same_result(
    prepared_import,
    repository,
) -> None:
    _engine, video_repository = repository
    report = video_repository.import_video_parse(prepared_import)

    states = video_repository.hydrate_video_chunk_states(report.video_chunk_ids)

    assert [item.evidence.video_chunk_id for item in states] == report.video_chunk_ids
    assert all(item.source_revision for item in states)
    assert all(item.index_operation == "DELETE" for item in states)
    assert all(item.index_status == "PENDING" for item in states)


def test_import_rolls_back_the_whole_video_when_a_late_write_fails(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository

    def fail_second_snapshot(_mapper, _connection, target) -> None:
        if target.after_chunk_sequence_no == 1:
            raise RuntimeError("injected late persistence failure")

    event.listen(ContextSnapshotRecord, "before_insert", fail_second_snapshot)
    try:
        with pytest.raises(RuntimeError, match="injected late persistence failure"):
            video_repository.import_video_parse(prepared_import)
    finally:
        event.remove(ContextSnapshotRecord, "before_insert", fail_second_snapshot)

    assert all(count == 0 for count in _table_counts(engine).values())


def test_import_rejects_an_empty_chunk_snapshot_before_writing(
    prepared_import,
    repository,
) -> None:
    engine, video_repository = repository

    with pytest.raises(ValueError, match="at least one VideoChunk"):
        video_repository.import_video_parse(
            replace(prepared_import, chunks=())
        )

    assert all(count == 0 for count in _table_counts(engine).values())


def test_import_rejects_a_different_chunk_snapshot_for_the_same_video(
    prepared_import,
    repository,
) -> None:
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)
    changed_chunk = replace(
        prepared_import.chunks[0],
        video_chunk_id="00000000-0000-0000-0000-000000000001",
    )
    changed_import = replace(
        prepared_import,
        chunks=(changed_chunk, *prepared_import.chunks[1:]),
    )

    with pytest.raises(RuntimeError, match="different canonical VideoChunk snapshot"):
        video_repository.import_video_parse(changed_import)
