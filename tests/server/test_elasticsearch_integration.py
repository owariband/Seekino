"""Optional real ES checks; coordinator fixtures use SQLite, not real MySQL."""

from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.server.test_video_parse_repository import _refinement_inputs
from elasticsearch import Elasticsearch
from sqlalchemy import event, select

from engine.rag import (
    ChunkIndexProjection,
    ChunkIndexingContract,
    EncoderContract,
    LexicalField,
    RecallRequest,
    RetrievalScope,
    SelectedVideoTask,
    VIDEO_CLIP_PREPROCESS_REVISION,
    VideoChunkIndexDocument,
    VideoClipInputContract,
)
from gateway.embedding import EmbeddingResult
from engine.common.config import VideoParseRuntimeConfig
from gateway.config import load_config
from server.config.media import ImageUploadConfig
from server.config.upload import UploadServiceConfig
from server.config.parse_profile import ParseConfig, default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    VideoParseTask,
)
from server.model.upload import UPLOAD_DELETION_PENDING_ERROR_CODE, UploadSession
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.upload import UploadRepository
from server.repository.video import SeriesNotReadyError
from server.repository.video_parse import VideoParseRepository
from server.service.upload import UploadService
from server.service.video_parse_index_coordinator import VideoParseIndexCoordinator
from server.service.video_parse_media import VideoParseMediaService
from server.service.video_retrieval import VideoParseRetrievalService
from server.storage.artifact import LocalArtifactStore
from server.storage.elasticsearch import (
    ElasticsearchBulkWriteError,
    ElasticsearchVideoChunkStore,
    document_id,
)
from server.utils.time import datetime_to_unix_time_ms


@pytest.fixture
def isolated_store():
    url = os.getenv("ANIVAG_TEST_ELASTICSEARCH_URL")
    if not url:
        pytest.skip("ANIVAG_TEST_ELASTICSEARCH_URL is not configured")
    api_key = os.getenv("ANIVAG_TEST_ELASTICSEARCH_API_KEY")
    client = Elasticsearch(url, api_key=api_key, request_timeout=15)
    index_name = f"anivag_test_{uuid4().hex}"
    store = ElasticsearchVideoChunkStore(
        client, index_name=index_name, environment="test",
        semantic_dimensions=2, vision_dimensions=2,
    )
    try:
        store.ensure_index()
        yield store
    finally:
        try:
            if client.indices.exists(index=index_name):
                client.indices.delete(index=index_name)
        finally:
            client.close()


def test_event_index_real_filter_visibility_and_cleanup(isolated_store):
    from tests.server.test_elasticsearch_events import _contract, _document
    from engine.rag.retrieval.contracts import EventFilters
    from server.storage.elasticsearch_events import ElasticsearchVideoEventStore

    client = isolated_store._client
    store = ElasticsearchVideoEventStore(
        client, index_name=f"seekino_event_test_{uuid4().hex}", environment="test",
        semantic_dimensions=2, vision_dimensions=2,
    )
    document = _document(dialogue=True)
    newer = document.model_copy(update={"version": document.version + 1})
    try:
        store.ensure_index(_contract())
        store.bulk_upsert([document, newer], write_version=1, refresh="wait_for")
        assert store.verify_ready([document, newer])
        assert store.verify_set(document.parse_task_id, document.video_chunk_id, newer.version, [newer.event_id])
        sources = [(newer.parse_task_id, newer.video_chunk_id, newer.version)]
        filters = EventFilters(speaker="祥子")
        lexical = store.search_lexical("居高临下", sources=sources, filters=filters, limit=3)
        assert [hit.event_id for hit in lexical] == [newer.event_id]
        assert lexical[0].matched_fields[0].role == "TRANSLATION"
        for channel in ("semantic", "vision"):
            hits = store.search_vector([1, 0], channel=channel, sources=sources, filters=filters, limit=3)
            assert [hit.event_id for hit in hits] == [newer.event_id]
        store.delete_older(newer.parse_task_id, newer.video_chunk_id, newer.version)
        assert store.inspect([document.event_id])[document.event_id] is None
        keys = [(document.parse_task_id, document.video_chunk_id)]
        store.bulk_delete(keys, refresh="wait_for")
        assert store.verify_absent(keys)
    finally:
        if client.indices.exists(index=store.index_name):
            client.indices.delete(index=store.index_name)


def _document(*, series_id, video_id, chunk_id, parse_task_id, question):
    projection = ChunkIndexProjection(
        semantic_text="千早爱音说我没事，画面上有大丈夫",
        lexical_fields=[
            LexicalField(
                field="dialogue", raw_text="我没事，真的。", language="zh-Hans",
                role="ORIGINAL", speaker="千早爱音", start_seconds=12,
                end_seconds=15, timeline_index=3,
            ),
            LexicalField(
                field="screen_text", raw_text="大丈夫", language="ja",
                start_seconds=12, end_seconds=15, timeline_index=4,
            ),
            LexicalField(
                field="person_names", raw_text="千早爱音",
                start_seconds=12, end_seconds=15, timeline_index=3,
            ),
            LexicalField(
                field="questions", raw_text=question, language="zh-Hans",
                start_seconds=10, end_seconds=20, enrichment_index=0,
            ),
        ],
    )
    return VideoChunkIndexDocument(
        tenant_id="es-test-workspace", series_id=series_id,
        video_id=video_id, video_chunk_id=chunk_id,
        parse_task_id=parse_task_id,
        content_version=1, index_generation=1,
        chunk_start_seconds=10, chunk_end_seconds=20,
        projection=projection,
        semantic_vector=[1.0, 0.0], vision_vector=[0.0, 1.0],
    )


def _id(document: VideoChunkIndexDocument) -> str:
    return document_id(document.parse_task_id, document.video_chunk_id)


class _EmbeddingBoundary:
    """Deterministic responses: no provider, paid model or real media process."""

    def __init__(self, model):
        self.model = model
        self.text_calls = self.video_calls = self.query_calls = 0

    def embed_text(self, _text):
        self.text_calls += 1
        return EmbeddingResult(model=self.model, modality="text", vector=[1.0, 0.0])

    def embed_video(self, _path):
        self.video_calls += 1
        return EmbeddingResult(model=self.model, modality="video", vector=[0.0, 1.0])

    def embed_query(self, _query):
        self.query_calls += 1
        return EmbeddingResult(model=self.model, modality="text", vector=[1.0, 0.0])


@contextmanager
def _indexing_case(tmp_path, store):
    """Actual Repository/Queue state, injectable ES boundary, fake model/media."""

    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'es-coordinator.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    try:
        create_schema(engine)
        factory = create_session_factory(engine)
        repository = VideoParseRepository(factory)
        queue = MySqlVideoParseQueue(factory, video_concurrency=3)
        series_id, video_id, task_id, upload_id = (str(uuid4()) for _ in range(4))
        now = datetime.now(UTC) - timedelta(seconds=1)
        source = tmp_path / "episode.mp4"
        source.write_bytes(b"deterministic-source")
        config = default_video_parse_v2_config().model_dump(mode="json")
        config.update(chunk_duration=10, interval_duration=30, batch_max_size=2)
        config["models"]["embedding"]["dimensions"] = 2
        with factory.begin() as session:
            session.add(Series(
                id=series_id, tenant_id="es-test-workspace",
                name="ES recovery", status="INDEXING", character_bible=[],
            ))
            session.flush()
            session.add(Video(
                id=video_id, series_id=series_id, episode_order=1,
                upload_sequence=1, filename="episode.mp4",
                source_uri=str(source), hash="a" * 64,
                size_bytes=source.stat().st_size, duration_ms=20_000,
                upload_status="COMPLETED", parse_status="QUEUED",
                upload_progress=100, parse_progress=0,
            ))
            session.flush()
            session.add(VideoParseTask(
                id=task_id, video_id=video_id, pipeline_version="video-parse-v2",
                status="PENDING", parse_config=config, chunk_count=2, version=1,
                next_execute_time=now, attempt_count=0, max_attempts=3,
                lease_version=0, processing_duration_ms=0,
                processing_timing_complete=True, is_test=True, is_deleted=False,
                detail={"_worker_runtime_v2": {"accepted": True}},
                create_time=datetime_to_unix_time_ms(now), update_time=datetime_to_unix_time_ms(now),
            ))
            session.flush()
            session.add(UploadSession(
                id=upload_id, tenant_id="es-test-workspace",
                series_id=series_id, episode_order=1, upload_sequence=1,
                filename="episode.mp4", media_type="video/mp4",
                size_bytes=source.stat().st_size,
                segment_size_bytes=source.stat().st_size,
                segment_count=1, version=1, status="COMPLETED",
                idempotency_key=str(uuid4()), expire_time=now + timedelta(hours=1),
                finish_time=now, video_id=video_id,
                initial_task_id=task_id,
            ))
        video_lease = queue.claim_video("prepare", lease_duration=timedelta(minutes=10))
        assert video_lease is not None
        chunks = repository.ensure_video_chunks(video_lease, [(0, 0, 10_000), (1, 10_000, 20_000)])
        prepared = repository.ensure_parse_tasks(
            video_lease, [chunk.video_chunk_id for chunk in chunks], [(0, 2)],
        )
        repository.activate_video_task(video_lease)
        for sequence, child_id in enumerate(prepared.chunk_parse_task_ids):
            lease = queue.claim_chunk("understand", lease_duration=timedelta(minutes=10))
            assert lease is not None and lease.chunk_parse_task_id == child_id
            repository.commit_chunk_result(lease, content={
                "persons": [],
                "timeline": [{
                    "event_type": "ACTION", "start_seconds": sequence * 10_000 + 1_000,
                    "end_seconds": sequence * 10_000 + 4_000,
                    "description": f"恢复测试松饼行动，片段{sequence}",
                    "participants": [], "visible_persons": [],
                }],
            }, detail={"fixture": "real-es-recovery"})
        refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=10))
        assert refine is not None
        repository.commit_interval_refinement(
            refine, **_refinement_inputs(repository, refine, now=None), interval_content={"summary": "unchanged"}, member_updates={},
        )
        index_lease = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=10))
        assert index_lease is not None
        encoder = EncoderContract(
            provider="ark", model=config["models"]["embedding"]["model"],
            encoder_revision="integration-revision", dimensions=2,
        )
        embedder = _EmbeddingBoundary(encoder.model)

        def write_media(command):
            Path(command[-1]).write_bytes(b"deterministic-prepared-media")

        coordinator = VideoParseIndexCoordinator(
            repository, store, embedder, media_resolver=Path,
            media_service=VideoParseMediaService(tmp_path / "media", command_runner=write_media),
            contract=ChunkIndexingContract(
                retrieval_contract_id="integration-retrieval-v1",
                semantic_encoder=encoder, vision_encoder=encoder,
                visual_input_contract=VideoClipInputContract(
                    fps=1, max_height=720, mime_type="video/mp4",
                    preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                ),
            ),
        )
        retrieval = VideoParseRetrievalService(
            repository, store, embedder, semantic_encoder=encoder,
            expected_index_generation=1, exact=True,
        )
        yield SimpleNamespace(
            factory=factory, repository=repository, queue=queue, store=store,
            series_id=series_id, video_id=video_id, task_id=task_id,
            upload_id=upload_id, video_lease=video_lease, index_lease=index_lease,
            prepared=prepared, coordinator=coordinator, retrieval=retrieval,
            encoder=encoder, embedder=embedder, config=config,
        )
    finally:
        engine.dispose()


@pytest.fixture
def indexing_case(isolated_store, tmp_path):
    # isolated_store skips before creating even the local coordinator fixture.
    with _indexing_case(tmp_path, isolated_store) as case:
        yield case


def test_real_es_mapping_three_queries_and_authorized_tasks(isolated_store):
    store = isolated_store
    series_id = str(uuid4())
    video_id = str(uuid4())
    chunk_id = str(uuid4())
    old = _document(
        series_id=series_id, video_id=video_id, chunk_id=chunk_id,
        parse_task_id=str(uuid4()), question="旧版独特问法蓝莓雨伞",
    )
    current = replace(
        _document(
            series_id=series_id, video_id=video_id, chunk_id=chunk_id,
            parse_task_id=str(uuid4()), question="新版独特问法松饼行动",
        ),
        semantic_vector=[0.8, 0.2],
    )
    other_video = str(uuid4())
    other_parse_task = str(uuid4())
    other = _document(
        series_id=series_id, video_id=other_video, chunk_id=str(uuid4()),
        parse_task_id=other_parse_task, question="另一集独特问法秋日花园",
    )
    store.bulk_upsert([old, current, other], refresh="wait_for")
    assert store.verify_ready([current, other])
    state = store.inspect([(current.parse_task_id, chunk_id)])
    assert state[(current.parse_task_id, chunk_id)].vision_vector_present

    scope = {
        "tenant_id": "es-test-workspace",
        "series_ids": [series_id],
        "parse_tasks_by_video_id": {video_id: current.parse_task_id, other_video: other_parse_task},
        "index_generation": 1,
    }
    question_hits = store.search_lexical("松饼行动", **scope, limit=10)
    assert [hit.video_chunk_id for hit in question_hits] == [chunk_id]
    assert {item.field for item in question_hits[0].matched_fields} == {"questions"}
    assert not store.search_lexical("蓝莓雨伞", **scope, limit=10)

    dialogue_hits = store.search_lexical("我没事", **scope, limit=10)
    assert dialogue_hits and any(
        field.field == "dialogue" and field.timeline_index == 3
        for field in dialogue_hits[0].matched_fields
    )
    screen_hits = store.search_lexical("大丈夫", **scope, limit=10)
    assert screen_hits and any(
        field.field == "screen_text" and field.language == "ja"
        for field in screen_hits[0].matched_fields
    )
    name_hits = store.search_lexical("千早爱音", **scope, limit=10)
    assert name_hits and any(field.field == "person_names" for field in name_hits[0].matched_fields)

    semantic = store.search_semantic(
        [1, 0], **scope, limit=10,
    )
    vision = store.search_vision(
        [0, 1], **scope, limit=10, exact=True,
    )
    assert {hit.video_chunk_id for hit in semantic} == {chunk_id, other.video_chunk_id}
    assert {hit.video_chunk_id for hit in vision} == {chunk_id, other.video_chunk_id}
    assert not store.search_semantic(
        [1, 0], **{**scope, "index_generation": 2}, limit=10,
    )

    updated = replace(
        _document(
            series_id=series_id, video_id=video_id, chunk_id=chunk_id,
            parse_task_id=current.parse_task_id, question="更新后的独特问法星空布丁",
        ),
        content_version=2,
    )
    assert _id(updated) == _id(current)
    store.bulk_upsert([updated], refresh="wait_for")
    assert store.verify_ready([updated])
    assert not store.verify_ready([current])
    assert not store.search_lexical("松饼行动", **scope, limit=10)
    updated_hits = store.search_lexical("星空布丁", **scope, limit=10)
    assert [hit.content_version for hit in updated_hits] == [2]

    store.delete(parse_task_id=old.parse_task_id, video_chunk_id=chunk_id,
                 refresh="wait_for")
    assert store.verify_absent([(old.parse_task_id, chunk_id)])
    assert store.inspect([(current.parse_task_id, chunk_id)])[(current.parse_task_id, chunk_id)].present


def test_real_es_engine_recall_keeps_channels_and_sparse_inputs(isolated_store):
    store = isolated_store
    document = replace(_document(
        series_id=str(uuid4()), video_id=str(uuid4()), chunk_id=str(uuid4()),
        parse_task_id=str(uuid4()), question="松饼行动",
    ), sparse_token_weights=(("utterance", 0.5),))
    other_task = replace(document, parse_task_id=str(uuid4()))
    store.bulk_upsert([document, other_task], refresh="wait_for")
    scope = RetrievalScope(tenant_id=document.tenant_id, tasks=(SelectedVideoTask(
        series_id=document.series_id, video_id=document.video_id,
        parse_task_id=document.parse_task_id, status="COMPLETE",
    ),))
    for channel in ("lexical", "semantic", "vision", "sparse"):
        request = RecallRequest(
            channel=channel, scope=scope, index_generation=1, limit=3,
            text="松饼行动" if channel == "lexical" else None,
            vector=(1.0, 0.0) if channel in ("semantic", "vision") else None,
            token_weights=(("utterance", 0.8),) if channel == "sparse" else None,
        )
        hit, = store.recall(request, exact=True)
        assert hit.channel == channel
        assert hit.retrieval_identity == document.retrieval_identity
        assert hit.video_chunk_id == document.video_chunk_id
        assert (hit.start_seconds, hit.end_seconds) == (10, 20)
        assert bool(hit.lexical_matches) == (channel == "lexical")
        if channel == "sparse":
            assert hit.score == pytest.approx(0.4, abs=0.005)
        assert store.recall(replace(request, index_generation=2), exact=True) == ()
    state = store.inspect([(document.parse_task_id, document.video_chunk_id)])[
        (document.parse_task_id, document.video_chunk_id)
    ]
    assert state.sparse_token_weights == (("utterance", 0.5),)
    source = store._client.get(index=store.index_name, id=_id(document))["_source"]
    assert source["sparse_vector"] == {"utterance": 0.5}
    assert "source_revision" not in source and "semantic_encoder_id" not in source
    inactive = replace(document, content_version=2, sparse_token_weights=())
    store.bulk_upsert([inactive], refresh="wait_for")
    assert store.verify_ready([inactive])
    assert store.recall(request, exact=True) == ()
    state = store.inspect([(inactive.parse_task_id, inactive.video_chunk_id)])[
        (inactive.parse_task_id, inactive.video_chunk_id)
    ]
    assert state.sparse_token_weights == ()


def test_real_es_bulk_partial_failure_keeps_successful_item(isolated_store, monkeypatch):
    store = isolated_store
    series_id = str(uuid4())
    video_id = str(uuid4())
    parse_task_id = str(uuid4())
    good = _document(
        series_id=series_id, video_id=video_id, chunk_id=str(uuid4()),
        parse_task_id=parse_task_id, question="成功写入的检索问题",
    )
    bad = _document(
        series_id=series_id, video_id=video_id, chunk_id=str(uuid4()),
        parse_task_id=parse_task_id, question="映射拒绝的检索问题",
    )
    original_source = store._document_source

    def with_unmapped_field(document):
        source = original_source(document)
        if document.video_chunk_id == bad.video_chunk_id:
            source["unmapped_field"] = "reject this one item"
        return source

    monkeypatch.setattr(store, "_document_source", with_unmapped_field)
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([good, bad], refresh="wait_for")
    assert set(caught.value.failures) == {_id(bad)}
    assert store.inspect([(parse_task_id, good.video_chunk_id)])[(parse_task_id, good.video_chunk_id)].present
    assert not store.inspect([(parse_task_id, bad.video_chunk_id)])[(parse_task_id, bad.video_chunk_id)].present


def test_real_es_strict_generation_and_exact_replay(isolated_store):
    store = isolated_store
    document = replace(
        _document(
            series_id=str(uuid4()),
            video_id=str(uuid4()),
            chunk_id=str(uuid4()),
            parse_task_id=str(uuid4()),
            question="generation two",
        ),
        index_generation=2,
        content_version=2,
    )
    store.bulk_upsert([document], refresh="wait_for")

    with pytest.raises(ElasticsearchBulkWriteError) as replay_error:
        store.bulk_upsert([document], refresh="wait_for")
    assert replay_error.value.status_codes == {_id(document): 409}
    assert store.inspect_exact([document]) == {_id(document): True}

    lower_generation = replace(
        document,
        index_generation=1,
        content_version=(1 << 31) - 1,
    )
    with pytest.raises(ElasticsearchBulkWriteError) as stale_error:
        store.bulk_upsert([lower_generation], refresh="wait_for")
    assert stale_error.value.status_codes == {_id(document): 409}

    conflicting = replace(document, semantic_vector=[0.5, 0.5])
    assert store.inspect_exact([conflicting]) == {_id(document): True}
    assert store.verify_ready([conflicting])

    for mismatched in (
        lower_generation,
        replace(document, index_generation=3),
        replace(document, content_version=1),
        replace(document, content_version=3),
        replace(document, tenant_id="another-workspace"),
        replace(document, series_id=str(uuid4())),
        replace(document, video_id=str(uuid4())),
        replace(document, parse_task_id=str(uuid4())),
        replace(document, video_chunk_id=str(uuid4())),
        replace(document, chunk_start_seconds=11),
        replace(document, chunk_end_seconds=21),
    ):
        assert store.inspect_exact([mismatched]) == {_id(mismatched): False}
        assert not store.is_ready([mismatched])


def test_real_es_readiness_accepts_same_version_after_numeric_tail_change(isolated_store, monkeypatch):
    store = isolated_store
    original_value = -0.00010395050048828125
    stored_value = -0.00010395050048828124
    document = replace(_document(
        series_id=str(uuid4()), video_id=str(uuid4()), chunk_id=str(uuid4()),
        parse_task_id=str(uuid4()), question="数值尾差版本确认",
    ), semantic_vector=[1.0, original_value])
    original_source = store._document_source

    def with_numeric_tail(item):
        source = original_source(item)
        source["semantic_vector"] = [1.0, stored_value]
        return source

    assert not store.is_ready([document])
    monkeypatch.setattr(store, "_document_source", with_numeric_tail)
    store.bulk_upsert([document], refresh="wait_for")
    monkeypatch.setattr(store, "_document_source", original_source)
    source = store._client.get(index=store.index_name, id=_id(document))["_source"]
    assert source["semantic_vector"] == [1.0, stored_value]
    assert source["semantic_vector"] != document.semantic_vector
    assert store.inspect_exact([document]) == {_id(document): True}
    assert store.is_ready([document])
    assert store.verify_ready([document])


def test_real_es_rejects_late_content_in_the_same_generation(isolated_store):
    store = isolated_store
    old = _document(
        series_id=str(uuid4()), video_id=str(uuid4()), chunk_id=str(uuid4()),
        parse_task_id=str(uuid4()), question="迟到的旧正文蓝莓雨伞",
    )
    current_projection = old.projection.model_copy(update={
        "semantic_text": "当前正文星空布丁",
        "lexical_fields": [
            field.model_copy(update={"raw_text": "当前正文星空布丁"})
            if field.field == "questions" else field
            for field in old.projection.lexical_fields
        ],
    })
    current = replace(
        old, content_version=2,
        projection=current_projection,
    )
    store.bulk_upsert([old], refresh="wait_for")
    store.bulk_upsert([current], refresh="wait_for")
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([old], refresh="wait_for")
    assert caught.value.status_codes == {_id(old): 409}
    assert store.inspect_exact([current]) == {_id(current): True}
    assert store.verify_ready([current])
    assert not store.is_ready([old])
    scope = {
        "tenant_id": current.tenant_id, "series_ids": [current.series_id],
        "parse_tasks_by_video_id": {current.video_id: current.parse_task_id},
        "index_generation": current.index_generation,
    }
    assert not store.search_lexical("蓝莓雨伞", **scope, limit=10)
    hits = store.search_lexical("星空布丁", **scope, limit=10)
    assert [hit.content_version for hit in hits] == [2]


def test_real_es_coordinator_retries_only_the_failed_chunk_without_reparsing(indexing_case, monkeypatch):
    case = indexing_case
    store = case.store
    good = case.repository.load_chunk_indexing_snapshot(case.index_lease).chunk
    failed_lease = case.queue.claim_chunk_index("index-second", lease_duration=timedelta(minutes=10))
    assert failed_lease is not None
    bad = case.repository.load_chunk_indexing_snapshot(failed_lease).chunk
    original_source, original_bulk = store._document_source, store.bulk_upsert
    bulk_calls = []

    def reject_second(document):
        source = original_source(document)
        if document.video_chunk_id == bad.video_chunk_id:
            source["unmapped_field"] = "deterministic item failure"
        return source

    def record_bulk(documents, *, refresh=False):
        bulk_calls.append(tuple(documents))
        return original_bulk(documents, refresh=refresh)

    monkeypatch.setattr(store, "_document_source", reject_second)
    monkeypatch.setattr(store, "bulk_upsert", record_bulk)
    case.coordinator.index_chunk(case.index_lease)
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        case.coordinator.index_chunk(failed_lease)
    assert set(caught.value.failures) == {document_id(case.task_id, bad.video_chunk_id)}
    keys = [(case.task_id, chunk.video_chunk_id) for chunk in (good, bad)]
    state = store.inspect(keys)
    assert state[keys[0]].present and not state[keys[1]].present
    with case.factory() as session:
        assert session.get(VideoParseTask, case.task_id).status != "COMPLETE"
        interval_id = case.prepared.interval_parse_task_ids[0]
        assert session.get(IntervalParseTask, interval_id).status == "COMPLETE"
        assert session.get(ChunkParseTask, good.chunk_parse_task_id).status == "COMPLETE"
        assert session.get(ChunkParseTask, bad.chunk_parse_task_id).status == "RUNNING"
        before = [
            (item.id, item.version, item.content, item.detail["scope"])
            for item in session.scalars(
                select(ChunkParseContent).order_by(ChunkParseContent.id)
            )
        ]
        refinement = session.get(IntervalParseContent, interval_id).content
    with pytest.raises(SeriesNotReadyError):
        case.retrieval.search(
            "松饼行动", tenant_id="es-test-workspace", series_ids=[case.series_id],
        )
    assert case.embedder.query_calls == 0

    monkeypatch.setattr(store, "_document_source", original_source)
    assert case.queue.retry_chunk(
        failed_lease, error="one ES item rejected", retry_at=datetime.now(UTC),
    )
    recovered = case.queue.claim_chunk_index("recovery", lease_duration=timedelta(minutes=10))
    assert recovered is not None and recovered.lease_version > failed_lease.lease_version
    assert recovered.phase == 3 and recovered.chunk_parse_task_id == bad.chunk_parse_task_id
    case.coordinator.index_chunk(recovered)
    assert [[item.video_chunk_id for item in batch] for batch in bulk_calls] == [
        [good.video_chunk_id], [bad.video_chunk_id], [bad.video_chunk_id],
    ]
    assert case.embedder.text_calls == case.embedder.video_calls == 2
    assert store.verify_ready([bulk_calls[0][0], bulk_calls[2][0]])
    assert case.repository.aggregate_task(case.video_lease).status == "COMPLETE"
    with case.factory() as session:
        assert session.get(IntervalParseTask, interval_id).status == "COMPLETE"
        after = [
            (item.id, item.version, item.content, item.detail["scope"])
            for item in session.scalars(
                select(ChunkParseContent).order_by(ChunkParseContent.id)
            )
        ]
        assert session.get(IntervalParseContent, interval_id).content == refinement
    assert after == before
    result = case.retrieval.search(
        "松饼行动", tenant_id="es-test-workspace", series_ids=[case.series_id],
    )
    assert {hit.candidate.video_chunk_id for hit in result.hits} == {
        good.video_chunk_id, bad.video_chunk_id,
    }
    assert {hit.evidence.video_id for hit in result.hits} == {case.video_id}
    assert not result.partial


def test_real_es_delete_reconcile_fences_an_immediately_late_writer(indexing_case, tmp_path, monkeypatch):
    case = indexing_case
    store = case.store
    original_bulk, original_delete = store.bulk_upsert, store.bulk_delete
    documents = []

    def record_documents(items, *, refresh=False):
        documents.extend(items)
        return original_bulk(items, refresh=refresh)

    monkeypatch.setattr(store, "bulk_upsert", record_documents)
    case.coordinator.index_chunk(case.index_lease)
    other_chunk = case.queue.claim_chunk_index("index-second", lease_duration=timedelta(minutes=10))
    assert other_chunk is not None
    case.coordinator.index_chunk(other_chunk)
    assert case.repository.aggregate_task(case.video_lease).status == "COMPLETE"
    other = _document(
        series_id=case.series_id, video_id=str(uuid4()), chunk_id=str(uuid4()),
        parse_task_id=str(uuid4()), question="不应删除的其他任务",
    )
    original_bulk([other], refresh="wait_for")
    keys = [(document.parse_task_id, document.video_chunk_id) for document in documents]
    deletion_calls = []

    def deleted_but_response_lost(items, *, refresh=False):
        deletion_calls.append(tuple(items))
        original_delete(items, refresh=refresh)
        # Check the immediate retained ES delete version, not unbounded gc_deletes.
        with pytest.raises(ElasticsearchBulkWriteError) as caught:
            original_bulk([documents[0]], refresh="wait_for")
        assert caught.value.status_codes == {_id(documents[0]): 409}
        assert store.verify_absent(keys)
        raise TimeoutError("delete response was lost after the ES write")

    uploads = UploadService(
        UploadRepository(case.factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"),
        video_parse_index_store=store,
        parse_defaults=ParseConfig.from_defaults(
            runtime=VideoParseRuntimeConfig(), gateway=load_config(),
            upload=UploadServiceConfig(), images=ImageUploadConfig(),
            embedding_model=None, embedding_dimensions=case.encoder.dimensions,
            embedding_fps=None,
        ),
        video_parse_query_encoder=case.encoder,
        video_parse_query_encoder_source=case.config["models"]["embedding"]["source"],
        # Legacy cleanup is a local boundary, never a Qdrant client or query fallback.
    )
    monkeypatch.setattr(store, "bulk_delete", deleted_but_response_lost)
    with pytest.raises(TimeoutError, match="delete response was lost"):
        uploads.delete_session(case.upload_id, tenant_id="es-test-workspace")
    with case.factory() as session:
        tombstone = session.get(UploadSession, case.upload_id)
        assert tombstone.last_error_code == UPLOAD_DELETION_PENDING_ERROR_CODE
        assert tombstone.video_id == case.video_id
        assert session.get(VideoParseTask, case.task_id).is_deleted
    with pytest.raises(SeriesNotReadyError):
        case.retrieval.search(
            "松饼行动", tenant_id="es-test-workspace", series_ids=[case.series_id],
        )

    monkeypatch.setattr(store, "bulk_delete", original_delete)
    assert uploads.reconcile_deletions() == (1, 0)
    assert set(deletion_calls[0]) == set(keys)
    assert store.verify_absent(keys)
    other_key = (other.parse_task_id, other.video_chunk_id)
    assert store.inspect([other_key])[other_key].present
    with case.factory() as session:
        tombstone = session.get(UploadSession, case.upload_id)
        assert tombstone.last_error_code is None and tombstone.video_id is None
        assert session.get(Video, case.video_id) is None
        assert session.get(VideoParseTask, case.task_id) is None
