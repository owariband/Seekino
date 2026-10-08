from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine.rag import (
    ChunkIndexProjection,
    LexicalField,
    OrderedFrame,
    OrderedFramesManifest,
    RecallRequest,
    RetrievalScope,
    SelectedVideoTask,
    VideoChunkIndexDocument,
)
from server.storage.elasticsearch import (
    ElasticsearchBulkWriteError,
    ElasticsearchDocumentConsistencyError,
    ElasticsearchIndexSchemaError,
    ElasticsearchVideoChunkStore,
    _external_version,
    document_id,
)


class _Indices:
    def __init__(self) -> None:
        self.mapping = None
        self.created = 0

    def exists(self, *, index):
        return self.mapping is not None

    def create(self, *, index, mappings):
        self.mapping = deepcopy(mappings)
        self.created += 1

    def get_mapping(self, *, index):
        return {index: {"mappings": self.mapping}}


class _Client:
    def __init__(self) -> None:
        self.indices = _Indices()
        self.version = "8.19.22"
        self.bulk_response = None
        self.search_response = {"hits": {"hits": []}}
        self.mget_response = {"docs": []}
        self.last_bulk = None
        self.last_search = None
        self.deleted = []

    def info(self):
        return {"version": {"number": self.version}}

    def bulk(self, **kwargs):
        self.last_bulk = kwargs
        return self.bulk_response

    def search(self, *, index, query=None, knn=None, size=None, source=None, source_includes=None):
        self.last_search = {
            "index": index, "query": query, "knn": knn, "size": size,
            "source": source, "source_includes": source_includes,
        }
        return self.search_response

    def mget(self, **kwargs):
        return self.mget_response

    def delete(self, **kwargs):
        self.deleted.append(kwargs)


@pytest.fixture
def IDs():
    return SimpleNamespace(
        series=str(uuid4()),
        video=str(uuid4()),
        chunk=str(uuid4()),
        parse_task=str(uuid4()),
        other_parse_task=str(uuid4()),
    )


@pytest.fixture
def client():
    return _Client()


@pytest.fixture
def store(client):
    return ElasticsearchVideoChunkStore(
        client, index_name="anivag_test_storage", environment="test",
        semantic_dimensions=2, vision_dimensions=2,
    )


def _document(ids, *, question: str = "银河蓝松饼行动"):
    projection = ChunkIndexProjection(
        semantic_text="千早爱音说我没事",
        lexical_fields=[
            LexicalField(
                field="dialogue", raw_text="我没事。", language="zh-Hans",
                role="ORIGINAL", speaker="千早爱音", start_seconds=12,
                end_seconds=15, timeline_index=3,
            ),
            LexicalField(
                field="questions", raw_text=question, language="zh-Hans",
                start_seconds=10, end_seconds=20, enrichment_index=0,
            ),
        ],
    )
    return VideoChunkIndexDocument(
        workspace_id="local-default", series_id=ids.series,
        video_id=ids.video, video_chunk_id=ids.chunk,
        parse_task_id=ids.parse_task,
        content_version=2, index_generation=1,
        chunk_start_seconds=10, chunk_end_seconds=20,
        projection=projection,
        semantic_vector=[1.0, 0.0], vision_vector=[0.0, 1.0],
    )


def _id(document: VideoChunkIndexDocument) -> str:
    return document_id(document.parse_task_id, document.video_chunk_id)


def _version(document: VideoChunkIndexDocument) -> int:
    return _external_version(document.content_version, document.index_generation)


def _scope(ids):
    return {
        "workspace_id": "local-default", "series_ids": [ids.series],
        "parse_tasks_by_video_id": {ids.video: ids.parse_task},
        "index_generation": 1,
    }


def _hit(store, document, *, inner=None):
    hit = {
        "_id": _id(document),
        "_score": 1.5,
        "_source": {key: value for key, value in store._document_source(document).items()
                    if key not in {"lexical_fields", "semantic_text", "semantic_vector", "vision_vector"}},
    }
    if inner is not None:
        hit["inner_hits"] = {"matched_lexical_fields": {"hits": {"hits": [
            {"_source": item.model_dump(mode="json")} for item in inner
        ]}}}
    return hit


def test_mapping_contract_and_version_gate(store, client):
    store.ensure_index()
    assert client.indices.created == 1
    mapping = client.indices.mapping
    assert mapping["properties"]["lexical_fields"]["type"] == "nested"
    assert mapping["properties"]["lexical_fields"]["properties"]["raw_text"]["analyzer"] == "cjk"
    assert mapping["properties"]["semantic_vector"]["similarity"] == "cosine"
    assert mapping["properties"]["vision_vector"]["dims"] == 2
    assert mapping["properties"]["parse_task_id"]["type"] == "keyword"
    assert "index_build_id" not in mapping["properties"]
    store.ensure_index()
    assert client.indices.created == 1
    client.indices.mapping["properties"]["lexical_fields"]["properties"]["raw_text"]["analyzer"] = "standard"
    with pytest.raises(ElasticsearchIndexSchemaError, match="lexical_fields"):
        store.ensure_index()
    client.version = "8.18.9"
    with pytest.raises(ElasticsearchIndexSchemaError, match="8.19"):
        store.ensure_index()


@pytest.mark.parametrize("explicit_search_analyzer", [False, True])
def test_mapping_accepts_equivalent_search_analyzer_without_mutating_response(
    store, client, explicit_search_analyzer,
):
    store.ensure_index()
    raw_text = client.indices.mapping["properties"]["lexical_fields"]["properties"]["raw_text"]
    if not explicit_search_analyzer:
        raw_text.pop("search_analyzer")
    before = deepcopy(client.indices.mapping)

    store.ensure_index()

    assert client.indices.created == 1
    assert client.indices.mapping == before


@pytest.mark.parametrize("raw_text", [
    {"type": "text", "analyzer": "cjk", "search_analyzer": "standard"},
    {"type": "text", "analyzer": "cjk", "search_analyzer": None},
    {"type": "text", "analyzer": "standard"},
    {"type": "text", "search_analyzer": "cjk"},
    {"type": "text"},
    {"type": "keyword", "analyzer": "cjk"},
])
def test_mapping_rejects_incompatible_text_analyzers_and_type(store, client, raw_text):
    store.ensure_index()
    client.indices.mapping["properties"]["lexical_fields"]["properties"]["raw_text"] = deepcopy(raw_text)
    before = deepcopy(client.indices.mapping)

    with pytest.raises(ElasticsearchIndexSchemaError, match="lexical_fields"):
        store.ensure_index()

    assert client.indices.created == 1
    assert client.indices.mapping == before


@pytest.mark.parametrize("field, key, value", [
    ("lexical_fields", "type", "object"),
    ("lexical_fields", "dynamic", True),
    ("semantic_vector", "dims", 3),
    ("vision_vector", "similarity", "dot_product"),
])
def test_mapping_default_search_analyzer_does_not_relax_other_fields(
    store, client, field, key, value,
):
    store.ensure_index()
    properties = client.indices.mapping["properties"]
    properties["lexical_fields"]["properties"]["raw_text"].pop("search_analyzer")
    properties[field][key] = value

    with pytest.raises(ElasticsearchIndexSchemaError, match=field):
        store.ensure_index()


def test_existing_build_mapping_requires_explicit_replacement(store, client):
    store.ensure_index()
    client.indices.mapping["_meta"]["index_contract_version"] = "anivag-video-chunk-es-1"

    with pytest.raises(ElasticsearchIndexSchemaError, match="metadata differs"):
        store.ensure_index()
    assert client.indices.created == 1


@pytest.mark.parametrize("source_mapping", [
    {"enabled": False},
    {"excludes": ["semantic_vector"]},
    {"includes": ["video_id"]},
    {"mode": "synthetic"},
])
def test_existing_index_must_retain_complete_stored_source(store, client, source_mapping):
    store.ensure_index()
    client.indices.mapping["_source"] = source_mapping

    with pytest.raises(ElasticsearchIndexSchemaError, match="complete stored _source"):
        store.ensure_index()


def test_delete_waits_for_refresh_and_verifies_storage_and_search_absence(
    store, client, IDs,
):
    document = _document(IDs)
    store.ensure_index()
    store.delete(
        parse_task_id=IDs.parse_task, video_chunk_id=IDs.chunk,
        refresh="wait_for",
    )
    assert client.deleted[-1] == {
        "index": store.index_name,
        "id": _id(document),
        "refresh": "wait_for",
    }
    client.mget_response = {"docs": [{"_id": _id(document), "found": False}]}
    client.search_response = {"hits": {"hits": []}}
    assert store.verify_absent([(IDs.parse_task, IDs.chunk)])
    assert client.last_search["query"] == {"ids": {"values": [_id(document)]}}

    client.search_response = {"hits": {"hits": [{"_id": _id(document)}]}}
    assert not store.verify_absent([(IDs.parse_task, IDs.chunk)])
    client.search_response = {"hits": {"hits": []}}
    client.mget_response = {"docs": [{
        "_id": _id(document), "found": True,
        "_source": store._document_source(document),
    }]}
    assert not store.verify_absent([(IDs.parse_task, IDs.chunk)])


def test_bulk_delete_accepts_absent_documents_and_reports_item_failures(
    store, client, IDs,
):
    store.ensure_index()
    second_chunk = str(uuid4())
    first_id = _id(_document(IDs))
    second_id = f"{IDs.parse_task}:{second_chunk}"
    client.bulk_response = {"errors": False, "items": [
        {"delete": {"_id": first_id, "status": 200}},
        {"delete": {"_id": second_id, "status": 404, "result": "not_found"}},
    ]}

    assert store.bulk_delete(
        [(IDs.parse_task, IDs.chunk), (IDs.parse_task, second_chunk)],
        refresh="wait_for",
    ) == [first_id, second_id]
    assert client.last_bulk == {
        "operations": [
            {"delete": {"_index": store.index_name, "_id": first_id}},
            {"delete": {"_index": store.index_name, "_id": second_id}},
        ],
        "refresh": "wait_for",
    }

    client.bulk_response = {"errors": True, "items": [
        {"delete": {
            "_id": first_id,
            "status": 429,
            "error": {"type": "rejected"},
        }},
    ]}
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_delete([(IDs.parse_task, IDs.chunk)])
    assert caught.value.failures == {first_id: "{'type': 'rejected'}"}


def test_bulk_checks_every_item_and_does_not_assume_query_visibility(store, client, IDs):
    document = _document(IDs)
    conflict = _document(SimpleNamespace(**{**vars(IDs), "chunk": str(uuid4())}))
    rejected = _document(SimpleNamespace(**{**vars(IDs), "chunk": str(uuid4())}))
    client.bulk_response = {"errors": True, "items": [
        {"index": {"_id": _id(document), "status": 201}},
        {"index": {
            "_id": _id(conflict), "status": 409,
            "error": {"type": "version_conflict_engine_exception"},
        }},
        {"index": {
            "_id": _id(rejected), "status": 429,
            "error": {"type": "rejected"},
        }},
    ]}
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([document, conflict, rejected])
    assert caught.value.failures == {
        _id(conflict): "{'type': 'version_conflict_engine_exception'}",
        _id(rejected): "{'type': 'rejected'}",
    }
    assert caught.value.status_codes == {
        _id(conflict): 409,
        _id(rejected): 429,
    }
    assert caught.value.version_conflict_ids == {_id(conflict)}
    assert client.last_bulk["refresh"] is False
    assert client.last_bulk["operations"][0]["index"]["_id"] == _id(document)
    assert client.last_bulk["operations"][0]["index"]["version"] == (1 << 31) | 2
    assert client.last_bulk["operations"][0]["index"]["version_type"] == "external"
    assert store.verify_ready([document]) is False

    client.bulk_response = {"errors": False, "items": [
        {"index": {"_id": _id(document), "status": 200}},
    ]}
    assert store.bulk_upsert([document], refresh="wait_for") == [_id(document)]
    assert client.last_bulk["refresh"] == "wait_for"
    client.search_response = {"hits": {"hits": [{
        "_id": _id(document), "_source": store._document_source(document)
    }]}}
    assert store.verify_ready([document]) is True


def test_bulk_rejects_zero_vectors_and_incomplete_response(store, client, IDs):
    document = _document(IDs)
    invalid = replace(document, vision_vector=[0.0, 0.0])
    with pytest.raises(ValueError, match="nonzero"):
        store.bulk_upsert([invalid])
    client.bulk_response = {"errors": False, "items": []}
    with pytest.raises(ElasticsearchBulkWriteError, match="1 document"):
        store.bulk_upsert([document])


def test_bulk_rejects_content_version_outside_external_version_range(store, IDs):
    with pytest.raises(ValueError, match="content_version"):
        store.bulk_upsert([replace(_document(IDs), content_version=2**63)])


def test_strict_external_version_rejects_a_lower_generation(store, client, IDs):
    older_generation = replace(
        _document(IDs), index_generation=1, content_version=(1 << 31) - 1
    )
    newer_generation = replace(
        _document(IDs), index_generation=2, content_version=1
    )
    client.bulk_response = {"errors": False, "items": [
        {"index": {"_id": _id(newer_generation), "status": 200}},
    ]}

    store.bulk_upsert([newer_generation])
    new_version = client.last_bulk["operations"][0]["index"]["version"]
    client.bulk_response = {"errors": True, "items": [{"index": {
        "_id": _id(older_generation),
        "status": 409,
        "error": {"type": "version_conflict_engine_exception"},
    }}]}
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([older_generation])
    old_version = client.last_bulk["operations"][0]["index"]["version"]

    assert new_version > old_version
    assert caught.value.status_codes == {_id(older_generation): 409}
    assert caught.value.version_conflict_ids == {_id(older_generation)}


def test_equal_external_version_conflict_supports_exact_reconciliation(store, client, IDs):
    document = _document(IDs)
    client.bulk_response = {"errors": True, "items": [{"index": {
        "_id": _id(document),
        "status": 409,
        "error": {"type": "version_conflict_engine_exception"},
    }}]}

    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([document], refresh="wait_for")
    assert caught.value.version_conflict_ids == {_id(document)}
    assert client.last_bulk["operations"][0]["index"]["version_type"] == "external"

    stored_source = store._document_source(document)
    client.mget_response = {"docs": [{
        "_id": _id(document), "found": True, "_source": stored_source,
    }]}
    assert store.inspect_exact([document]) == {_id(document): True}

    conflicting = _document(IDs, question="同版本的不同投影")
    assert _version(conflicting) == _version(document)
    assert store.inspect_exact([conflicting]) == {_id(document): False}


def test_document_and_mapping_keep_only_outer_versions_and_real_search_content(store, IDs):
    document = _document(IDs)
    source = store._document_source(document)
    removed = {"source_revision", "projection_revision", "retrieval_contract_id",
               "semantic_encoder_id", "vision_encoder_id", "semantic_input_sha256", "vision_input_sha256"}
    assert removed.isdisjoint(source)
    assert removed.isdisjoint(store._mapping()["properties"])
    assert (source["content_version"], source["index_generation"]) == (2, 1)
    assert source["lexical_fields"][0]["raw_text"] == "我没事。"


def test_bulk_rejects_semantic_vector_presence_inconsistent_with_formal_text(store, IDs):
    with pytest.raises(ValueError, match="presence must match semantic text"):
        store.bulk_upsert([replace(_document(IDs), semantic_vector=None)])


def test_content_updates_overwrite_the_same_task_chunk_document(store, client, IDs):
    original = _document(IDs)
    updated = replace(
        _document(IDs, question="更新后的检索问题"),
        content_version=3,
    )
    assert _id(original) == _id(updated) == f"{IDs.parse_task}:{IDs.chunk}"
    client.bulk_response = {"errors": False, "items": [
        {"index": {"_id": _id(original), "status": 200}},
    ]}
    store.bulk_upsert([original])
    old_source = client.last_bulk["operations"][1]
    store.bulk_upsert([updated])
    operations = client.last_bulk["operations"]
    assert operations[0]["index"]["_id"] == _id(original)
    assert operations[0]["index"]["version"] == (1 << 31) | 3
    assert operations[0]["index"]["version_type"] == "external"
    assert operations[1]["content_version"] == 3
    assert operations[1]["lexical_fields"] != old_source["lexical_fields"]
    assert "index_build_id" not in operations[1]

    another_task = replace(updated, parse_task_id=IDs.other_parse_task)
    assert _id(another_task) != _id(updated)


@pytest.mark.parametrize("field,value", [
    ("parse_task_id", "another-task"),
    ("content_version", 1),
    ("index_generation", 2),
    ("vision_vector", None),
])
def test_verify_ready_checks_exact_task_version_generation_and_required_source(
    store, client, IDs, field, value,
):
    document = _document(IDs)
    source = store._document_source(document)
    source[field] = value
    client.search_response = {"hits": {"hits": [{"_id": _id(document), "_source": source}]}}
    assert store.is_ready([document]) is False
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="differs"):
        store.verify_ready([document])
    filters = client.last_search["query"]["bool"]["filter"]
    assert {"term": {"parse_task_id": IDs.parse_task}} in filters
    assert {"term": {"content_version": 2}} in filters
    assert {"term": {"index_generation": 1}} in filters


def test_lexical_query_preserves_nested_source_and_authorized_task_pair(store, client, IDs):
    document = _document(IDs)
    question = document.projection.lexical_fields[1]
    client.search_response = {"hits": {"hits": [_hit(store, document, inner=[question])]}}
    matches = store.search_lexical("银河蓝松饼行动", **_scope(IDs), limit=10)
    assert matches[0].matched_fields == (question,)
    assert matches[0].matched_fields[0].field == "questions"
    assert matches[0].index_generation == 1
    nested_query = client.last_search["query"]["bool"]["must"][0]["nested"]
    assert nested_query["path"] == "lexical_fields"
    assert nested_query["inner_hits"]["name"] == "matched_lexical_fields"
    scope = client.last_search["query"]["bool"]["filter"][0]
    pairs = scope["bool"]["filter"][-1]["bool"]["should"]
    assert pairs == [{"bool": {"filter": [
        {"term": {"video_id": IDs.video}}, {"term": {"parse_task_id": IDs.parse_task}}
    ]}}]
    assert "content_version" in client.last_search["source_includes"]

    for name, value in (
        ("parse_task_id", IDs.other_parse_task),
        ("workspace_id", "another-workspace"),
        ("series_id", str(uuid4())),
        ("index_generation", 2),
    ):
        wrong = _hit(store, document, inner=[question])
        wrong["_source"][name] = value
        client.search_response = {"hits": {"hits": [wrong]}}
        with pytest.raises(ElasticsearchDocumentConsistencyError):
            store.search_lexical("银河蓝松饼行动", **_scope(IDs), limit=10)

    other_video = str(uuid4())
    crossed = replace(document, video_id=other_video, parse_task_id=IDs.parse_task)
    client.search_response = {"hits": {"hits": [_hit(store, crossed, inner=[question])]}}
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="video/task scope"):
        store.search_lexical(
            "银河蓝松饼行动", workspace_id="local-default", series_ids=[IDs.series],
            parse_tasks_by_video_id={IDs.video: IDs.parse_task, other_video: IDs.other_parse_task},
            index_generation=1, limit=10,
        )
    with pytest.raises(ValueError, match="parse_tasks_by_video_id must not be empty"):
        store.search_lexical(
            "银河蓝松饼行动", **{**_scope(IDs), "parse_tasks_by_video_id": {}}, limit=10,
        )


def test_vector_search_prefilters_scope_and_generation(store, client, IDs):
    document = _document(IDs)
    client.search_response = {"hits": {"hits": [_hit(store, document)]}}
    matches = store.search_semantic(
        [1, 0], **_scope(IDs), limit=5,
    )
    assert matches[0].video_chunk_id == IDs.chunk
    knn = client.last_search["knn"]
    assert knn["field"] == "semantic_vector"
    assert {"term": {"index_generation": 1}} in knn["filter"]["bool"]["filter"][0]["bool"]["filter"]
    store.search_vision(
        [0, 1], **_scope(IDs), limit=5, exact=True,
    )
    assert client.last_search["query"]["script_score"]["script"]["source"].startswith("cosineSimilarity")
    with pytest.raises(ValueError, match="nonzero"):
        store.search_semantic(
            [0, 0], **_scope(IDs), limit=5,
        )


def test_inspect_is_separate_from_query_readiness_and_delete_is_task_scoped(store, client, IDs):
    document = _document(IDs)
    source = store._document_source(document)
    client.indices.mapping = store._mapping()
    client.mget_response = {"docs": [{
        "_id": _id(document), "found": True, "_source": source,
    }]}
    state = store.inspect([(IDs.parse_task, IDs.chunk)])[(IDs.parse_task, IDs.chunk)]
    assert state.present and state.semantic_vector_present and state.vision_vector_present
    assert state.lexical_fields_present and state.semantic_text_present
    assert state.content_version == document.content_version
    assert state.semantic_vector == (1.0, 0.0)
    assert state.vision_vector == (0.0, 1.0)
    assert store.verify_ready([document]) is False
    store.delete(parse_task_id=IDs.parse_task, video_chunk_id=IDs.chunk)
    assert client.deleted[0]["id"] == _id(document)


def test_inspect_does_not_treat_per_document_error_as_absence(store, client, IDs):
    client.indices.mapping = store._mapping()
    document = _document(IDs)
    client.mget_response = {"docs": [{
        "_id": _id(document), "found": False,
        "error": {"type": "unavailable_shards_exception"},
    }]}
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="read failed"):
        store.inspect([(IDs.parse_task, IDs.chunk)])


@pytest.mark.parametrize("channel", ["lexical", "semantic", "vision", "sparse"])
def test_recall_executes_engine_channel_scope_budget_and_returns_formal_identity(store, client, IDs, channel):
    document = replace(_document(IDs), sparse_token_weights=(("utterance", 0.5),))
    lexical = (document.projection.lexical_fields[0],) if channel == "lexical" else ()
    client.search_response = {"hits": {"hits": [_hit(store, document, inner=lexical or None)]}}
    scope = RetrievalScope(workspace_id="local-default", tasks=(SelectedVideoTask(
        series_id=IDs.series, video_id=IDs.video, parse_task_id=IDs.parse_task, status="COMPLETE",
    ),))
    request = RecallRequest(
        channel=channel, scope=scope, index_generation=1, limit=7,
        text="我没事" if channel == "lexical" else None,
        vector=(1.0, 0.0) if channel in ("semantic", "vision") else None,
        token_weights=(("utterance", 0.8),) if channel == "sparse" else None,
    )
    hit, = store.recall(request, exact=True)
    assert (hit.channel, hit.video_chunk_id, hit.rank, hit.score) == (channel, IDs.chunk, 1, 1.5)
    assert hit.retrieval_identity == document.retrieval_identity
    assert (hit.start_seconds, hit.end_seconds) == (10, 20)
    assert hit.lexical_matches == lexical
    assert client.last_search["size"] == 7
    if channel == "sparse":
        assert client.last_search["query"]["bool"]["must"] == [{"sparse_vector": {
            "field": "sparse_vector", "query_vector": {"utterance": 0.8}, "prune": False,
        }}]
        assert store._document_source(document)["sparse_vector"] == {"utterance": 0.5}


def test_recall_empty_scope_and_inactive_sparse_query_need_no_storage_call(store, client, IDs):
    empty = RecallRequest(channel="vision", scope=RetrievalScope(workspace_id="local-default", tasks=()),
                          index_generation=1, limit=1, vector=(1.0, 0.0))
    assert store.recall(empty) == ()
    scope = RetrievalScope(workspace_id="local-default", tasks=(SelectedVideoTask(
        series_id=IDs.series, video_id=IDs.video, parse_task_id=IDs.parse_task, status="COMPLETE",
    ),))
    inactive = RecallRequest(channel="sparse", scope=scope, index_generation=1, limit=1, token_weights=())
    assert store.recall(inactive) == ()
    assert client.last_search is None


def test_recall_checks_each_selected_video_series_pair(store, client, IDs):
    other_series = str(uuid4())
    document = replace(_document(IDs), series_id=other_series)
    client.search_response = {"hits": {"hits": [_hit(store, document)]}}
    scope = RetrievalScope(workspace_id="local-default", tasks=(
        SelectedVideoTask(series_id=IDs.series, video_id=IDs.video, parse_task_id=IDs.parse_task, status="COMPLETE"),
        SelectedVideoTask(series_id=other_series, video_id=str(uuid4()), parse_task_id=str(uuid4()), status="COMPLETE"),
    ))
    request = RecallRequest(channel="semantic", scope=scope, index_generation=1, limit=3, vector=(1.0, 0.0))
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="selected video's series"):
        store.recall(request)


def test_sparse_mapping_storage_inspection_and_invalid_weights(store, client, IDs):
    document = replace(_document(IDs), sparse_token_weights=(("utterance", 0.8),))
    source = store._document_source(document)
    assert "sparse_vector" not in store._document_source(replace(document, sparse_token_weights=None))
    assert store._document_source(replace(document, sparse_token_weights=()))["sparse_vector"] == {}
    assert store._mapping()["properties"]["sparse_vector"] == {"type": "sparse_vector"}
    client.indices.mapping = store._mapping()
    client.mget_response = {"docs": [{"_id": _id(document), "found": True, "_source": source}]}
    state = store.inspect([(IDs.parse_task, IDs.chunk)])[(IDs.parse_task, IDs.chunk)]
    assert state.sparse_token_weights == (("utterance", 0.8),)
    with pytest.raises(ValueError, match="nonnegative"):
        store.search_sparse({"utterance": -0.5}, **_scope(IDs), limit=2)
    with pytest.raises(ValueError, match="nonnegative"):
        store._document_source(replace(document, sparse_token_weights=(("utterance", -0.5),)))


def test_old_physical_contract_is_rejected_without_mutating_the_index(store, client):
    store.ensure_index()
    client.indices.mapping["_meta"]["index_contract_version"] = "anivag-video-chunk-es-4"
    before = deepcopy(client.indices.mapping)
    with pytest.raises(ElasticsearchIndexSchemaError, match="metadata differs"):
        store.ensure_index()
    assert client.indices.mapping == before
    assert client.indices.created == 1


def test_prepared_ordered_frames_retain_absolute_times_and_actual_manifest(store, IDs):
    manifest = OrderedFramesManifest(
        frames=(OrderedFrame(ordinal=0, at_seconds=12.0, input_sha256="a" * 64),
                OrderedFrame(ordinal=1, at_seconds=18.0, input_sha256="b" * 64)),
        sampling_contract_id="prepared-sampling", mime_type="image/png", preprocess_revision="v1",
    )
    source = store._document_source(replace(_document(IDs), ordered_frames_manifest=manifest))
    assert source["ordered_frames_manifest"] == manifest.as_dict()
    assert [frame["at_seconds"] for frame in source["ordered_frames_manifest"]["frames"]] == [12.0, 18.0]
    assert store._mapping()["properties"]["ordered_frames_manifest"] == {"type": "object", "enabled": False}
