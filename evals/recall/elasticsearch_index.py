from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from elasticsearch import Elasticsearch

from engine.rag.retrieval.fusion import RankedVideoChunk
from engine.parsev1.retrieval import VideoChunkRetrievalDocument

_EVAL_INDEX_PATTERN = re.compile(r"^anivag_recall_eval_[a-z0-9][a-z0-9_-]*$")
_INDEX_PURPOSE = {"anivag_recall_eval": 1}
_TEXT_VECTOR_FIELD = "text_vector"
_VISION_VECTOR_FIELD = "vision_vector"


class ElasticsearchIndexSchemaError(RuntimeError):
    """The selected index does not belong to this evaluation contract."""


class ElasticsearchDatasetConflictError(RuntimeError):
    """A dataset ID already identifies another evaluation snapshot."""


class ElasticsearchEvaluationWriteError(RuntimeError):
    """One or more evaluation documents were not made searchable."""


class ElasticsearchVideoChunkIndex:
    """Store one isolated V1 dual-vector evaluation dataset in Elasticsearch."""

    def __init__(
        self,
        client: Elasticsearch,
        *,
        index_name: str,
        dataset_id: str,
    ) -> None:
        if not _EVAL_INDEX_PATTERN.fullmatch(index_name):
            raise ValueError(
                "evaluation index must start with anivag_recall_eval_ "
                "and contain only lowercase letters, digits, _ or -"
            )
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must not be empty")
        self._client = client
        self.index_name = index_name
        self.dataset_id = dataset_id.strip()
        self._text_dimensions: int | None = None
        self._vision_dimensions: int | None = None

    def ensure_index(
        self,
        *,
        text_dimensions: int,
        vision_dimensions: int,
    ) -> None:
        if text_dimensions <= 0 or vision_dimensions <= 0:
            raise ValueError("vector dimensions must be greater than zero")
        mapping = {
            "_meta": _INDEX_PURPOSE,
            "dynamic": "strict",
            "properties": {
                "dataset_id": {"type": "keyword"},
                "dataset_digest": {"type": "keyword"},
                "chunk_sequence_no": {"type": "integer"},
                "start_seconds": {"type": "long"},
                "end_seconds": {"type": "long"},
                "video_compact": {"type": "text"},
                "is_fallback": {"type": "boolean"},
                _TEXT_VECTOR_FIELD: {
                    "type": "dense_vector",
                    "dims": text_dimensions,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "hnsw"},
                },
                _VISION_VECTOR_FIELD: {
                    "type": "dense_vector",
                    "dims": vision_dimensions,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "hnsw"},
                },
            },
        }
        if not self._client.indices.exists(index=self.index_name):
            self._client.indices.create(index=self.index_name, mappings=mapping)

        info = _body(self._client.indices.get(index=self.index_name))
        # An alias could point at a business index despite having an eval-like name.
        if set(info) != {self.index_name}:
            raise ElasticsearchIndexSchemaError(
                "evaluation index must be a concrete, dedicated index"
            )
        actual = info[self.index_name]["mappings"]
        if actual.get("_meta") != _INDEX_PURPOSE:
            raise ElasticsearchIndexSchemaError(
                "index is missing the AniVAG recall evaluation marker"
            )
        properties = actual.get("properties", {})
        for field, expected in mapping["properties"].items():
            found = properties.get(field, {})
            if any(
                found.get(key, {}).get("type") != value["type"]
                if key == "index_options"
                else found.get(key) != value
                for key, value in expected.items()
            ):
                raise ElasticsearchIndexSchemaError(
                    f"evaluation index field {field!r} has an incompatible mapping"
                )
        self._text_dimensions = text_dimensions
        self._vision_dimensions = vision_dimensions

    def upsert_chunks(
        self,
        documents: Sequence[VideoChunkRetrievalDocument],
        *,
        text_vectors: Mapping[int, list[float]],
        vision_vectors: Mapping[int, list[float]],
        vision_input_sha256: Mapping[int, str],
        embedding_model: str,
        embedding_fps: float,
    ) -> None:
        sequences = {document.sequence_no for document in documents}
        if not documents or len(sequences) != len(documents):
            raise ValueError("evaluation requires distinct VideoChunk documents")
        if sequences != set(text_vectors):
            raise ValueError("text vectors must contain exactly one vector per VideoChunk")
        if sequences != set(vision_vectors):
            raise ValueError("vision vectors must contain exactly one vector per VideoChunk")
        if self._text_dimensions is None or self._vision_dimensions is None:
            raise RuntimeError("ensure_index must run before upsert_chunks")
        for vector in text_vectors.values():
            _validate_vector(vector, self._text_dimensions)
        for vector in vision_vectors.values():
            _validate_vector(vector, self._vision_dimensions)

        self.assert_dataset_snapshot(
            documents,
            vision_input_sha256=vision_input_sha256,
            embedding_model=embedding_model,
            embedding_fps=embedding_fps,
            allow_incomplete=True,
        )
        digest = self._dataset_digest(
            documents,
            vision_input_sha256=vision_input_sha256,
            embedding_model=embedding_model,
            embedding_fps=embedding_fps,
        )
        operations: list[dict] = []
        expected_ids: list[str] = []
        for document in documents:
            point_id = self._point_id(document.sequence_no)
            expected_ids.append(point_id)
            operations.extend((
                {"index": {"_index": self.index_name, "_id": point_id}},
                {
                    "dataset_id": self.dataset_id,
                    "dataset_digest": digest,
                    "chunk_sequence_no": document.sequence_no,
                    "start_seconds": document.start_seconds,
                    "end_seconds": document.end_seconds,
                    "video_compact": document.video_compact,
                    "is_fallback": document.is_fallback,
                    _TEXT_VECTOR_FIELD: text_vectors[document.sequence_no],
                    _VISION_VECTOR_FIELD: vision_vectors[document.sequence_no],
                },
            ))
        response = _body(self._client.bulk(operations=operations, refresh="wait_for"))
        items = response.get("items", [])
        if len(items) != len(expected_ids):
            raise ElasticsearchEvaluationWriteError("bulk response has missing items")
        for point_id, item in zip(expected_ids, items, strict=True):
            indexed = item.get("index", {})
            if (
                indexed.get("_id") != point_id
                or indexed.get("status") not in (200, 201)
                or indexed.get("error")
            ):
                raise ElasticsearchEvaluationWriteError(
                    f"evaluation document {point_id} failed to index: "
                    f"{indexed.get('error', indexed.get('status'))}"
                )
        if response.get("errors"):
            raise ElasticsearchEvaluationWriteError("bulk response reported an error")
        # Count is a search operation. Together with refresh=wait_for this checks
        # that this exact snapshot is visible before evaluation queries start.
        try:
            self.assert_dataset_snapshot(
                documents,
                vision_input_sha256=vision_input_sha256,
                embedding_model=embedding_model,
                embedding_fps=embedding_fps,
                allow_incomplete=False,
            )
        except ElasticsearchDatasetConflictError as exc:
            raise ElasticsearchEvaluationWriteError(
                "evaluation documents are not query-visible after bulk indexing"
            ) from exc

    def search_text(
        self,
        query_vector: list[float],
        *,
        limit: int,
        mode: Literal["exact", "approximate"],
    ) -> list[RankedVideoChunk]:
        return self._search(query_vector, field=_TEXT_VECTOR_FIELD, limit=limit, mode=mode)

    def search_vision(
        self,
        query_vector: list[float],
        *,
        limit: int,
        mode: Literal["exact", "approximate"],
    ) -> list[RankedVideoChunk]:
        return self._search(query_vector, field=_VISION_VECTOR_FIELD, limit=limit, mode=mode)

    def count_chunks(self) -> int:
        return int(_body(self._client.count(
            index=self.index_name,
            query={"term": {"dataset_id": self.dataset_id}},
        ))["count"])

    def assert_dataset_snapshot(
        self,
        documents: Sequence[VideoChunkRetrievalDocument],
        *,
        vision_input_sha256: Mapping[int, str],
        embedding_model: str,
        embedding_fps: float,
        allow_incomplete: bool,
    ) -> None:
        if not documents:
            raise ValueError("at least one VideoChunk retrieval document is required")
        actual = self.count_chunks()
        if actual == 0 and allow_incomplete:
            return
        digest = self._dataset_digest(
            documents,
            vision_input_sha256=vision_input_sha256,
            embedding_model=embedding_model,
            embedding_fps=embedding_fps,
        )
        matching = int(_body(self._client.count(
            index=self.index_name,
            query={"bool": {"filter": [
                {"term": {"dataset_id": self.dataset_id}},
                {"term": {"dataset_digest": digest}},
            ]}},
        ))["count"])
        if matching != actual or actual > len(documents) or (
            not allow_incomplete and actual != len(documents)
        ):
            raise ElasticsearchDatasetConflictError(
                f"Elasticsearch dataset {self.dataset_id!r} does not match this "
                "VideoChunk snapshot, visual inputs, or embedding contract. "
                "Use a new dataset ID."
            )

    def _search(
        self,
        query_vector: list[float],
        *,
        field: str,
        limit: int,
        mode: Literal["exact", "approximate"],
    ) -> list[RankedVideoChunk]:
        if self._text_dimensions is None or self._vision_dimensions is None:
            raise RuntimeError("ensure_index must run before search")
        _validate_vector(
            query_vector,
            self._text_dimensions if field == _TEXT_VECTOR_FIELD else self._vision_dimensions,
        )
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        if mode == "exact":
            response = self._client.search(
                index=self.index_name,
                size=limit,
                query={"script_score": {
                    "query": {"term": {"dataset_id": self.dataset_id}},
                    "script": {
                        "source": f"cosineSimilarity(params.query_vector, '{field}') + 1.0",
                        "params": {"query_vector": query_vector},
                    },
                }},
                source_includes=["dataset_id", "chunk_sequence_no"],
            )
        elif mode == "approximate":
            response = self._client.search(
                index=self.index_name,
                size=limit,
                knn={
                    "field": field,
                    "query_vector": query_vector,
                    "k": limit,
                    "num_candidates": min(10_000, max(100, limit * 4)),
                    "filter": {"term": {"dataset_id": self.dataset_id}},
                },
                source_includes=["dataset_id", "chunk_sequence_no"],
            )
        else:
            raise ValueError("mode must be exact or approximate")

        scored: list[tuple[int, float]] = []
        seen: set[int] = set()
        for hit in _body(response)["hits"]["hits"]:
            source = hit.get("_source") or {}
            sequence_no = source.get("chunk_sequence_no")
            score = hit.get("_score")
            if (
                source.get("dataset_id") != self.dataset_id
                or isinstance(sequence_no, bool)
                or not isinstance(sequence_no, int)
                or sequence_no < 0
                or sequence_no in seen
                or hit.get("_id") != self._point_id(sequence_no)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise RuntimeError("Elasticsearch returned an invalid evaluation hit")
            seen.add(sequence_no)
            scored.append((sequence_no, float(score)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            RankedVideoChunk(rank=rank, chunk_sequence_no=sequence_no, score=score)
            for rank, (sequence_no, score) in enumerate(scored, start=1)
        ]

    def _dataset_digest(
        self,
        documents: Sequence[VideoChunkRetrievalDocument],
        *,
        vision_input_sha256: Mapping[int, str],
        embedding_model: str,
        embedding_fps: float,
    ) -> str:
        if self._text_dimensions is None or self._vision_dimensions is None:
            raise RuntimeError("ensure_index must run before checking a dataset")
        sequences = {document.sequence_no for document in documents}
        if set(vision_input_sha256) != sequences or any(
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in vision_input_sha256.values()
        ):
            raise ValueError(
                "vision input identities must contain one SHA-256 per VideoChunk"
            )
        ordered_documents = sorted(documents, key=lambda item: item.sequence_no)
        source = {
            "model": embedding_model,
            "fps": embedding_fps,
            "text_dimensions": self._text_dimensions,
            "vision_dimensions": self._vision_dimensions,
            "documents": [
                document.model_dump(mode="json")
                for document in ordered_documents
            ],
            "vision_inputs": [
                {
                    "sequence_no": document.sequence_no,
                    "input_sha256": vision_input_sha256[document.sequence_no],
                }
                for document in ordered_documents
            ],
        }
        canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _point_id(self, sequence_no: int) -> str:
        return str(uuid5(NAMESPACE_URL, f"anivag:{self.dataset_id}:segment:{sequence_no}"))


def _validate_vector(vector: list[float], dimensions: int) -> None:
    if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
        raise ValueError("vector dimensions or values do not match the index")
    if not any(value != 0 for value in vector):
        raise ValueError("cosine vectors must have non-zero norm")


def _body(response) -> Mapping:
    return response.body if hasattr(response, "body") else response
