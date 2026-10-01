"""Optional Elasticsearch 8.19 checks against a disposable test-only index."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch

from engine.retrieval.index_projection import ChunkIndexProjection, LexicalField
from server.storage.elasticsearch import (
    ElasticsearchBulkWriteError,
    ElasticsearchVideoChunkStore,
    VideoChunkIndexDocument,
)


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


def _document(*, series_id, video_id, chunk_id, parse_task_id, question):
    return VideoChunkIndexDocument(
        workspace_id="es-test-workspace", series_id=series_id,
        video_id=video_id, video_chunk_id=chunk_id,
        parse_task_id=parse_task_id,
        content_version=1, index_generation=1, source_revision=question,
        retrieval_contract_id="integration-retrieval-v1",
        semantic_encoder_id="integration-semantic-v1",
        vision_encoder_id="integration-vision-v1",
        semantic_input_sha256="a" * 64,
        vision_input_sha256="b" * 64,
        chunk_start_seconds=10, chunk_end_seconds=20,
        projection=ChunkIndexProjection(
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
        ),
        semantic_vector=[1.0, 0.0], vision_vector=[0.0, 1.0],
    )


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
        "workspace_id": "es-test-workspace",
        "series_ids": [series_id],
        "parse_tasks_by_video_id": {video_id: current.parse_task_id, other_video: other_parse_task},
        "retrieval_contract_id": "integration-retrieval-v1",
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
        [1, 0], **scope, encoder_id="integration-semantic-v1", limit=10,
    )
    vision = store.search_vision(
        [0, 1], **scope, encoder_id="integration-vision-v1", limit=10, exact=True,
    )
    assert {hit.video_chunk_id for hit in semantic} == {chunk_id, other.video_chunk_id}
    assert {hit.video_chunk_id for hit in vision} == {chunk_id, other.video_chunk_id}
    assert not store.search_semantic(
        [1, 0], **scope, encoder_id="different-vector-space", limit=10,
    )

    updated = replace(
        _document(
            series_id=series_id, video_id=video_id, chunk_id=chunk_id,
            parse_task_id=current.parse_task_id, question="更新后的独特问法星空布丁",
        ),
        content_version=2,
    )
    assert updated.document_id == current.document_id
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
    assert set(caught.value.failures) == {bad.document_id}
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
    )
    store.bulk_upsert([document], refresh="wait_for")

    with pytest.raises(ElasticsearchBulkWriteError) as replay_error:
        store.bulk_upsert([document], refresh="wait_for")
    assert replay_error.value.status_codes == {document.document_id: 409}
    assert store.inspect_exact([document]) == {document.document_id: True}

    lower_generation = replace(
        document,
        index_generation=1,
        content_version=(1 << 31) - 1,
    )
    with pytest.raises(ElasticsearchBulkWriteError) as stale_error:
        store.bulk_upsert([lower_generation], refresh="wait_for")
    assert stale_error.value.status_codes == {document.document_id: 409}

    conflicting = replace(document, source_revision="same-version-different-source")
    assert store.inspect_exact([conflicting]) == {document.document_id: False}
