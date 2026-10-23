from copy import deepcopy
from uuid import uuid4

import pytest

from engine.rag import EncoderContract
from engine.rag.indexing.events import EventIndexDocument, EventIndexingContract
from engine.rag.retrieval.contracts import EventFilters, EventIndexNotReadyError
from engine.rag.vision.event_frames import EventFrameContract
from server.storage.elasticsearch import (
    ElasticsearchBulkWriteError, ElasticsearchDocumentConsistencyError, ElasticsearchIndexSchemaError,
)
from server.storage.elasticsearch_events import ElasticsearchVideoEventStore


class _Indices:
    def __init__(self):
        self.mapping = None

    def exists(self, *, index):
        return self.mapping is not None

    def create(self, *, index, mappings):
        self.mapping = deepcopy(mappings)

    def get_mapping(self, *, index):
        return {index: {"mappings": self.mapping}}


class _Client:
    def __init__(self):
        self.indices = _Indices()
        self.documents = {}
        self.requests = []
        self.hits = []
        self.counts = []
        self.bulk_response = None

    def info(self):
        return {"version": {"number": "8.19.22"}}

    def bulk(self, **kwargs):
        self.requests.append(kwargs)
        operations = kwargs["operations"]
        for action, source in zip(operations[::2], operations[1::2], strict=True):
            self.documents[action["index"]["_id"]] = deepcopy(source)
        return self.bulk_response or {"errors": False, "items": [
            {"index": {"_id": action["index"]["_id"], "status": 201}}
            for action in operations[::2]
        ]}

    def mget(self, *, index, ids):
        return {"docs": [{"_id": key, "found": key in self.documents,
                          "_source": self.documents.get(key)} for key in ids]}

    def search(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("query", {}).get("ids"):
            hits = [{"_id": key, "_source": self.documents[key]}
                    for key in kwargs["query"]["ids"]["values"] if key in self.documents]
        else:
            hits = self.hits
        return {"hits": {"hits": hits}}

    def count(self, **kwargs):
        self.requests.append(kwargs)
        return {"count": self.counts.pop(0)}

    def delete_by_query(self, **kwargs):
        self.requests.append(kwargs)
        return {"failures": [], "version_conflicts": 0, "timed_out": False}


def _contract():
    encoder = EncoderContract(provider="fixture", model="embedding", encoder_revision="v1", dimensions=2)
    return EventIndexingContract(
        semantic_encoder=encoder, vision_encoder=encoder, capture=EventFrameContract(max_height=512),
    )


def _document(*, task=None, chunk=None, version=2, ordinal=0, dialogue=False):
    return EventIndexDocument(
        parse_task_id=task or str(uuid4()), video_chunk_id=chunk or str(uuid4()),
        version=version, timeline_index=ordinal, event_type="DIALOGUE" if dialogue else "ACTION",
        speaker="祥子" if dialogue else None,
        lexical_fields=[{"field": "dialogue", "raw_text": "真是居高临下啊", "language": "zh-Hans", "role": "TRANSLATION"}]
        if dialogue else [{"field": "event_description", "raw_text": "角色从台阶跳下", "language": None, "role": None}],
        semantic_vectors=[{"source": "description", "vector": [1, 0]}, *(
            [{"source": "dialogue", "language": "zh-Hans", "role": "TRANSLATION", "vector": [0.6, 0.8]}]
            if dialogue else []
        )], vision_vector=[0, 1],
        detail={"capture": {"frames": [{"requested_at_ms": 150, "actual_at_ms": None, "image_sha256": "a" * 64}]}},
    )


@pytest.fixture
def storage():
    client = _Client()
    store = ElasticsearchVideoEventStore(
        client, index_name="seekino_event_test", environment="test",
        semantic_dimensions=2, vision_dimensions=2,
    )
    return client, store


def test_missing_index_is_recoverable_and_does_not_create_on_query(storage):
    client, store = storage
    assert client.indices.mapping is None
    with pytest.raises(EventIndexNotReadyError, match="not initialized"):
        store.bind_contract(_contract())
    assert client.indices.mapping is None
    assert store.inspect(["absent"]) == {"absent": None}


def test_event_schema_has_only_agreed_fields_and_freezes_complete_contract(storage):
    client, store = storage
    contract = _contract()
    store.ensure_index(contract)
    mapping = client.indices.mapping
    assert set(mapping["properties"]) == {
        "video_chunk_id", "parse_task_id", "version", "timeline_index", "event_type", "speaker",
        "lexical_fields", "semantic_vectors", "vision_vector", "detail",
    }
    assert set(mapping["properties"]["lexical_fields"]["properties"]) == {"field", "raw_text", "language", "role"}
    assert mapping["properties"]["detail"] == {"type": "object", "enabled": False}
    assert mapping["properties"]["semantic_vectors"] == {"type": "nested", "dynamic": "strict", "properties": {
        "source": {"type": "keyword"}, "language": {"type": "keyword"}, "role": {"type": "keyword"},
        "vector": {"type": "dense_vector", "dims": 2, "index": True, "similarity": "cosine",
                   "index_options": {"type": "hnsw"}},
    }}
    assert mapping["_meta"]["index_contract_version"] == "seekino-video-event-es-2"
    assert mapping["_meta"]["event_contract"] == contract.as_dict()
    changed = contract.model_copy(update={"capture": contract.capture.model_copy(update={"max_frames": 4})})
    with pytest.raises(ElasticsearchIndexSchemaError, match="fixed contract"):
        store.bind_contract(changed)
    other = ElasticsearchVideoEventStore(client, index_name=store.index_name, environment="test",
                                        semantic_dimensions=2, vision_dimensions=2)
    with pytest.raises(ElasticsearchIndexSchemaError, match="metadata"):
        other.bind_contract(changed)


@pytest.mark.parametrize("change", ["old_version", "scalar", "object", "dimensions", "extra_field"])
def test_event_schema_rejects_old_or_incompatible_semantic_mapping(storage, change):
    client, store = storage
    store.ensure_index(_contract())
    mapping = client.indices.mapping
    semantic = mapping["properties"]["semantic_vectors"]
    if change == "old_version":
        mapping["_meta"]["index_contract_version"] = "seekino-video-event-es-1"
    elif change == "scalar":
        mapping["properties"]["semantic_vector"] = semantic["properties"]["vector"]
        del mapping["properties"]["semantic_vectors"]
    elif change == "object":
        semantic["type"] = "object"
    elif change == "dimensions":
        semantic["properties"]["vector"]["dims"] = 3
    else:
        semantic["properties"]["weight"] = {"type": "float"}
    with pytest.raises(ElasticsearchIndexSchemaError):
        store.bind_contract(_contract())


@pytest.mark.parametrize("dialogue", [False, True])
def test_bulk_restore_verifies_content_and_search_visibility(storage, dialogue):
    client, store = storage
    store.ensure_index(_contract())
    document = _document(dialogue=dialogue)
    assert store.bulk_upsert([document], write_version=7, refresh="wait_for") == [document.event_id]
    action, source = client.requests[-1]["operations"]
    assert action["index"]["version"] == 7 and action["index"]["version_type"] == "external_gte"
    assert "event_id" not in source and ("speaker" in source) == dialogue
    assert len(source["semantic_vectors"]) == (2 if dialogue else 1)
    assert store.inspect([document.event_id])[document.event_id] == document
    assert store.verify_ready([document])
    client.documents[document.event_id]["version"] += 1
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="ID differs"):
        store.inspect([document.event_id])


def test_bulk_malformed_or_failed_item_is_not_success(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    client.bulk_response = {"items": [], "errors": False}
    with pytest.raises(ElasticsearchBulkWriteError):
        store.bulk_upsert([document])
    client.bulk_response = {"items": [{"index": {"_id": document.event_id, "status": 409, "error": "stale"}}], "errors": True}
    with pytest.raises(ElasticsearchBulkWriteError) as caught:
        store.bulk_upsert([document])
    assert caught.value.version_conflict_ids == {document.event_id}


def test_store_revalidates_copied_models_at_the_storage_boundary(storage):
    _, store = storage
    store.ensure_index(_contract())
    document = _document()
    for update in ({"event_type": "NOT_AN_EVENT"}, {"semantic_vectors": (
        document.semantic_vectors[0].model_copy(update={"vector": (True, 0.0)}),
    )}):
        with pytest.raises(ValueError):
            store.bulk_upsert([document.model_copy(update=update)])


@pytest.mark.parametrize("semantic", [
    [], [1.0, 0.0], [{"source": "description", "vector": [1.0, 0.0, 0.0]}],
    [{"source": "description", "vector": [0.0, 0.0]}],
    [{"source": "description", "vector": [True, 0.0]}],
    [{"source": "dialogue", "vector": [1.0, 0.0]}],
    [{"source": "description", "vector": [1.0, 0.0]}] * 2,
])
def test_malformed_semantic_collection_cannot_be_written_or_read(storage, semantic):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    source = {**document.as_source(), "semantic_vectors": semantic}
    with pytest.raises(ValueError):
        store.bulk_upsert([source])
    assert not client.documents
    client.documents[document.event_id] = source
    with pytest.raises(ElasticsearchDocumentConsistencyError):
        store.inspect([document.event_id])


@pytest.mark.parametrize("channel", ["lexical", "semantic", "vision"])
def test_every_channel_prefilters_paired_sources_type_and_root_speaker(storage, channel):
    client, store = storage
    store.ensure_index(_contract())
    first, second = _document(dialogue=True), _document(dialogue=True, version=9)
    sources = [(first.parse_task_id, first.video_chunk_id, first.version),
               (second.parse_task_id, second.video_chunk_id, second.version)]
    filters = EventFilters(speaker="祥子")
    if channel == "lexical":
        store.search_lexical("居高临下", sources=sources, filters=filters, limit=5)
        conditions = client.requests[-1]["query"]["bool"]["filter"]
    else:
        store.search_vector([1, 0], channel=channel, sources=sources, filters=filters, limit=5)
        conditions = client.requests[-1]["knn"]["filter"]["bool"]["filter"]
    assert conditions[0] == {"bool": {"should": [{"bool": {"filter": [
        {"term": {"parse_task_id": task}}, {"term": {"video_chunk_id": chunk}}, {"term": {"version": version}},
    ]}} for task, chunk, version in sources], "minimum_should_match": 1}}
    assert {"terms": {"event_type": ["DIALOGUE"]}} in conditions
    assert {"term": {"speaker": "祥子"}} in conditions


@pytest.mark.parametrize("exact", [False, True])
def test_semantic_search_scores_one_parent_event_with_max_child_and_parent_filters(storage, exact):
    client, store = storage
    store.ensure_index(_contract())
    document = _document(dialogue=True)
    sources = [(document.parse_task_id, document.video_chunk_id, document.version)]
    filters = EventFilters(speaker="祥子", video_chunk_ids=(document.video_chunk_id,))
    client.hits = [{"_id": document.event_id, "_score": 1.8 if exact else 0.9,
                    "_source": document.as_source()}]
    hit, = store.search_vector([1, 0], channel="semantic", sources=sources,
                               filters=filters, limit=20, exact=exact)
    assert hit.event_id == document.event_id and hit.rank == 1
    assert hit.score == (1.8 if exact else 0.9)
    request = client.requests[-1]
    assert request["size"] == 20
    assert "semantic_vectors" not in request["source_includes"]
    assert "vision_vector" not in request["source_includes"]
    if exact:
        query = request["query"]["bool"]
        conditions = query["filter"]
        nested = query["must"][0]["nested"]
        assert nested["path"] == "semantic_vectors" and nested["score_mode"] == "max"
        score = nested["query"]["script_score"]
        assert score["query"] == {"exists": {"field": "semantic_vectors.vector"}}
        assert score["script"] == {
            "source": "cosineSimilarity(params.vector, 'semantic_vectors.vector') + 1.0",
            "params": {"vector": [1.0, 0.0]},
        }
    else:
        knn = request["knn"]
        assert knn["field"] == "semantic_vectors.vector"
        assert knn["k"] == 20 and knn["num_candidates"] == 100
        conditions = knn["filter"]["bool"]["filter"]
    assert len(conditions) == 4
    assert all("exists" not in condition for condition in conditions)
    assert {"term": {"speaker": "祥子"}} in conditions
    assert {"terms": {"event_type": ["DIALOGUE"]}} in conditions
    assert {"terms": {"video_chunk_id": [document.video_chunk_id]}} in conditions


def test_exact_vision_search_keeps_parent_scalar_scoring(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document(dialogue=True)
    store.search_vector([1, 0], channel="vision", sources=[(
        document.parse_task_id, document.video_chunk_id, document.version,
    )], filters=EventFilters(speaker="祥子"), limit=4, exact=True)
    score = client.requests[-1]["query"]["script_score"]
    assert score["query"]["bool"]["filter"][-1] == {"exists": {"field": "vision_vector"}}
    assert {"term": {"speaker": "祥子"}} in score["query"]["bool"]["filter"]
    assert score["script"]["source"] == "cosineSimilarity(params.vector, 'vision_vector') + 1.0"


@pytest.mark.parametrize("exact", [False, True])
def test_semantic_search_rejects_duplicate_or_untrusted_parent_results(storage, exact):
    client, store = storage
    store.ensure_index(_contract())
    document = _document(dialogue=True)
    hit = {"_id": document.event_id, "_score": 0.9, "_source": document.as_source()}
    for hits, version in (([hit, hit], document.version), ([hit], document.version + 1)):
        client.hits = hits
        with pytest.raises(ElasticsearchDocumentConsistencyError, match="trusted source"):
            store.search_vector([1, 0], channel="semantic", sources=[(
                document.parse_task_id, document.video_chunk_id, version,
            )], filters=EventFilters(), limit=20, exact=exact)


def test_search_rejects_stale_source_and_retains_actual_lexical_fields(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document(dialogue=True)
    client.hits = [{"_id": document.event_id, "_score": 2.5, "_source": document.as_source(), "inner_hits": {
        "matched_lexical_fields": {"hits": {"hits": [{"_source": document.as_source()["lexical_fields"][0]}]}}
    }}]
    hit, = store.search_lexical("居高临下", sources=[(document.parse_task_id, document.video_chunk_id, 2)],
                               filters=EventFilters(speaker="祥子"), limit=3)
    assert hit.lexical_fields == document.lexical_fields and hit.matched_fields == document.lexical_fields
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="trusted source"):
        store.search_lexical("居高临下", sources=[(document.parse_task_id, document.video_chunk_id, 3)],
                             filters=EventFilters(speaker="祥子"), limit=3)


def test_large_scope_compacts_only_equal_task_and_version_pairs(storage):
    client, store = storage
    store.ensure_index(_contract())
    task = str(uuid4())
    chunks = [str(uuid4()) for _ in range(400)]
    other = str(uuid4())
    store.search_vector([1, 0], channel="semantic", sources=[
        *((task, chunk, 2) for chunk in chunks), (task, other, 3),
    ], filters=EventFilters(), limit=3)
    grouped = client.requests[-1]["knn"]["filter"]["bool"]["filter"][0]["bool"]["should"]
    assert len(grouped) == 2
    assert grouped[0]["bool"]["filter"] == [
        {"term": {"parse_task_id": task}}, {"terms": {"video_chunk_id": chunks}}, {"term": {"version": 2}},
    ]
    assert grouped[1]["bool"]["filter"][-1] == {"term": {"version": 3}}
    assert other not in grouped[0]["bool"]["filter"][1]["terms"]["video_chunk_id"]


def test_exact_set_verification_handles_empty_and_missing_events(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    args = document.parse_task_id, document.video_chunk_id, document.version
    client.counts = [0]
    assert store.verify_set(*args, [])
    client.counts = [1, 0]
    assert not store.verify_set(*args, [document.event_id])
    client.counts = [1, 1]
    assert store.verify_set(*args, [document.event_id])


def test_deletion_never_targets_newer_source_versions(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    store.delete_older(document.parse_task_id, document.video_chunk_id, 2)
    assert client.requests[-1]["query"]["bool"]["filter"][-1] == {"range": {"version": {"lt": 2}}}
    keys = [(document.parse_task_id, document.video_chunk_id)]
    store.bulk_delete(keys, refresh="wait_for")
    assert client.requests[-1]["refresh"] is True
    client.counts = [0]
    assert store.verify_absent(keys)


def test_partial_search_cannot_be_accepted_as_complete_recall_or_publication(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    client.search = lambda **kwargs: {"timed_out": True, "hits": {"hits": []}}
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="partial"):
        store.search_vector([1, 0], channel="semantic", sources=[(
            document.parse_task_id, document.video_chunk_id, document.version,
        )], filters=EventFilters(), limit=3)
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="partial"):
        store.verify_ready([document])
    client.count = lambda **kwargs: {"count": 0, "_shards": {"failed": 1}}
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="partial"):
        store.verify_set(document.parse_task_id, document.video_chunk_id, document.version, [])


def test_malformed_count_and_duplicate_hit_cannot_prove_readiness(storage):
    client, store = storage
    store.ensure_index(_contract())
    document = _document()
    client.counts = [False]
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="invalid count"):
        store.verify_set(document.parse_task_id, document.video_chunk_id, document.version, [])
    hit = {"_id": document.event_id, "_source": document.as_source()}
    client.search = lambda **kwargs: {"hits": {"hits": [hit, hit]}}
    with pytest.raises(ElasticsearchDocumentConsistencyError, match="duplicate"):
        store.verify_ready([document])
