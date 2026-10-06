from __future__ import annotations

import os
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from elasticsearch import Elasticsearch

from engine.parsev1.retrieval import VideoChunkRetrievalDocument
from evals.recall.elasticsearch_index import (
    ElasticsearchDatasetConflictError,
    ElasticsearchEvaluationWriteError,
    ElasticsearchIndexSchemaError,
    ElasticsearchVideoChunkIndex,
)


class FakeIndices:
    def __init__(self) -> None:
        self.mappings: dict[str, dict] = {}

    def exists(self, *, index: str) -> bool:
        return index in self.mappings

    def create(self, *, index: str, mappings: dict) -> None:
        self.mappings[index] = {"mappings": mappings}

    def get(self, *, index: str) -> dict:
        return {index: self.mappings[index]}


class FakeElasticsearch:
    def __init__(self) -> None:
        self.indices = FakeIndices()
        self.documents: dict[str, dict] = {}
        self.search_arguments: dict | None = None
        self.fail_bulk_item = False
        self.bulk_not_visible = False

    def bulk(self, *, operations: list[dict], refresh: str) -> dict:
        assert refresh == "wait_for"
        items = []
        for offset in range(0, len(operations), 2):
            point_id = operations[offset]["index"]["_id"]
            if self.fail_bulk_item and offset == 0:
                items.append({"index": {"_id": point_id, "status": 400, "error": "rejected"}})
            else:
                if not self.bulk_not_visible:
                    self.documents[point_id] = operations[offset + 1]
                items.append({"index": {"_id": point_id, "status": 201}})
        return {"errors": self.fail_bulk_item, "items": items}

    def count(self, *, index: str, query: dict) -> dict:
        assert index in self.indices.mappings
        filters = (
            query["bool"]["filter"] if "bool" in query else [query]
        )
        matched = sum(
            all(
                document.get(key) == value
                for clause in filters
                for key, value in clause["term"].items()
            )
            for document in self.documents.values()
        )
        return {"count": matched}

    def search(self, **kwargs) -> dict:
        self.search_arguments = kwargs
        return {"hits": {"hits": [
            {
                "_id": point_id,
                "_score": float(2 - document["chunk_sequence_no"]),
                "_source": {
                    "dataset_id": document["dataset_id"],
                    "chunk_sequence_no": document["chunk_sequence_no"],
                },
            }
            for point_id, document in self.documents.items()
            if document["dataset_id"] == "first"
        ]}}


def _chunk(sequence_no: int, *, compact: str = "chunk") -> VideoChunkRetrievalDocument:
    return VideoChunkRetrievalDocument(
        sequence_no=sequence_no,
        start_seconds=sequence_no * 60,
        end_seconds=(sequence_no + 1) * 60,
        video_compact=compact,
        text=compact,
        is_fallback=False,
    )


def _upsert(
    index: ElasticsearchVideoChunkIndex,
    chunks: list[VideoChunkRetrievalDocument],
    *,
    vision_identity: str = "0" * 64,
) -> None:
    index.upsert_chunks(
        chunks,
        text_vectors={chunk.sequence_no: [1.0, 0.0] for chunk in chunks},
        vision_vectors={chunk.sequence_no: [0.0, 1.0] for chunk in chunks},
        vision_input_sha256={
            chunk.sequence_no: vision_identity for chunk in chunks
        },
        embedding_model="eval-model",
        embedding_fps=1.0,
    )


def test_dedicated_index_and_dataset_are_required() -> None:
    client = FakeElasticsearch()
    with pytest.raises(ValueError, match="anivag_recall_eval_"):
        ElasticsearchVideoChunkIndex(
            client, index_name="anivag_video_chunks_production", dataset_id="first"
        )
    with pytest.raises(ValueError, match="dataset_id"):
        ElasticsearchVideoChunkIndex(
            client, index_name="anivag_recall_eval_test", dataset_id=" "
        )


def test_index_mapping_and_ownership_are_checked() -> None:
    client = FakeElasticsearch()
    index = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    mapping = client.indices.mappings[index.index_name]["mappings"]
    assert mapping["properties"]["dataset_id"] == {"type": "keyword"}
    assert mapping["properties"]["text_vector"]["dims"] == 2
    mapping["properties"]["text_vector"]["index_options"]["m"] = 16
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    with pytest.raises(ElasticsearchIndexSchemaError, match="incompatible mapping"):
        index.ensure_index(text_dimensions=3, vision_dimensions=2)
    mapping["_meta"] = {}
    with pytest.raises(ElasticsearchIndexSchemaError, match="marker"):
        index.ensure_index(text_dimensions=2, vision_dimensions=2)


def test_vectors_are_dataset_filtered_and_exact_and_approximate_are_distinct() -> None:
    client = FakeElasticsearch()
    first = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    second = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="second"
    )
    first.ensure_index(text_dimensions=2, vision_dimensions=2)
    second.ensure_index(text_dimensions=2, vision_dimensions=2)
    _upsert(first, [_chunk(0), _chunk(1)])
    _upsert(second, [_chunk(0)])

    assert first.count_chunks() == 2
    assert second.count_chunks() == 1
    assert set(client.documents) == {
        str(uuid5(NAMESPACE_URL, f"anivag:{dataset}:segment:{sequence}"))
        for dataset, sequence in (("first", 0), ("first", 1), ("second", 0))
    }

    assert [hit.chunk_sequence_no for hit in first.search_text(
        [1.0, 0.0], limit=2, mode="exact"
    )] == [0, 1]
    assert client.search_arguments["query"]["script_score"]["query"] == {
        "term": {"dataset_id": "first"}
    }
    assert "cosineSimilarity" in client.search_arguments["query"]["script_score"]["script"]["source"]

    first.search_vision([0.0, 1.0], limit=2, mode="approximate")
    assert client.search_arguments["knn"]["filter"] == {
        "term": {"dataset_id": "first"}
    }
    assert client.search_arguments["knn"]["field"] == "vision_vector"


def test_same_dataset_id_rejects_changed_snapshot_before_new_write() -> None:
    client = FakeElasticsearch()
    index = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    _upsert(index, [_chunk(0)])
    with pytest.raises(ElasticsearchDatasetConflictError, match="new dataset ID"):
        index.assert_dataset_snapshot(
            [_chunk(0, compact="changed")],
            vision_input_sha256={0: "0" * 64},
            embedding_model="eval-model",
            embedding_fps=1.0,
            allow_incomplete=True,
        )


@pytest.mark.parametrize("changed_identity", ["1" * 64, "2" * 64])
def test_same_dataset_id_rejects_changed_visual_input_identity(
    changed_identity: str,
) -> None:
    client = FakeElasticsearch()
    index = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    _upsert(index, [_chunk(0)])
    stored_document = dict(next(iter(client.documents.values())))

    with pytest.raises(ElasticsearchDatasetConflictError, match="new dataset ID"):
        _upsert(index, [_chunk(0)], vision_identity=changed_identity)
    assert next(iter(client.documents.values())) == stored_document


def test_partial_bulk_failure_is_not_query_ready() -> None:
    client = FakeElasticsearch()
    client.fail_bulk_item = True
    index = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    with pytest.raises(ElasticsearchEvaluationWriteError, match="failed to index"):
        _upsert(index, [_chunk(0), _chunk(1)])
    client.fail_bulk_item = False
    index.assert_dataset_snapshot(
        [_chunk(0), _chunk(1)],
        vision_input_sha256={0: "0" * 64, 1: "0" * 64},
        embedding_model="eval-model",
        embedding_fps=1.0,
        allow_incomplete=True,
    )
    _upsert(index, [_chunk(0), _chunk(1)])
    assert index.count_chunks() == 2


def test_successful_bulk_response_does_not_replace_query_visibility_check() -> None:
    client = FakeElasticsearch()
    client.bulk_not_visible = True
    index = ElasticsearchVideoChunkIndex(
        client, index_name="anivag_recall_eval_test", dataset_id="first"
    )
    index.ensure_index(text_dimensions=2, vision_dimensions=2)
    with pytest.raises(ElasticsearchEvaluationWriteError, match="query-visible"):
        _upsert(index, [_chunk(0)])


def test_real_elasticsearch_dataset_isolation_and_query_visibility() -> None:
    url = os.getenv("ANIVAG_TEST_ELASTICSEARCH_URL")
    if not url:
        pytest.skip("ANIVAG_TEST_ELASTICSEARCH_URL is not configured")
    if url == os.getenv("ANIVAG_ELASTICSEARCH_URL"):
        pytest.fail("test Elasticsearch URL must differ from the application URL")

    client = Elasticsearch(url, request_timeout=30)
    index_name = f"anivag_recall_eval_test_{uuid4().hex}"
    try:
        first = ElasticsearchVideoChunkIndex(
            client, index_name=index_name, dataset_id="first"
        )
        second = ElasticsearchVideoChunkIndex(
            client, index_name=index_name, dataset_id="second"
        )
        first.ensure_index(text_dimensions=2, vision_dimensions=2)
        second.ensure_index(text_dimensions=2, vision_dimensions=2)
        first.upsert_chunks(
            [_chunk(0), _chunk(1)],
            text_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
            vision_vectors={0: [0.0, 1.0], 1: [1.0, 0.0]},
            vision_input_sha256={0: "0" * 64, 1: "0" * 64},
            embedding_model="eval-model",
            embedding_fps=1.0,
        )
        _upsert(second, [_chunk(0)])
        assert first.count_chunks() == 2
        assert second.count_chunks() == 1
        assert [hit.chunk_sequence_no for hit in first.search_text(
            [1.0, 0.0], limit=2, mode="exact"
        )] == [0, 1]
        assert first.search_vision([1.0, 0.0], limit=2, mode="approximate")[0].chunk_sequence_no == 1
    finally:
        client.indices.delete(index=index_name, ignore_unavailable=True)
        client.close()
