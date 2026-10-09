"""Elasticsearch 8.19 storage for rebuildable, versioned VideoChunk search views."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from elasticsearch import BadRequestError, Elasticsearch, NotFoundError

from engine.rag.indexing.projection import (
    INDEX_PROJECTION_VERSION,
    LEXICAL_FIELD_WEIGHTS,
    LEXICAL_PHRASE_FIELDS,
    ChunkIndexProjection,
    LexicalField,
)
from engine.rag import (
    RawChunkHit, RecallRequest, VideoChunkIndexDocument,
)
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import validate_sparse_weights

from ..database import DatabaseEnvironment
from ..utils.validation import canonical_uuid, require_nonempty_text

INDEX_CONTRACT_VERSION = "anivag-video-chunk-es-5"
ANALYSIS_CONTRACT_VERSION = "cjk-1"
_MAX_EXTERNAL_VERSION = 9_223_372_036_854_775_807
_EXTERNAL_VERSION_SHIFT = 31
_MAX_CONTENT_VERSION = (1 << _EXTERNAL_VERSION_SHIFT) - 1
_MAX_INDEX_GENERATION = (1 << _EXTERNAL_VERSION_SHIFT) - 1
_SOURCE_FIELDS = (
    "workspace_id",
    "series_id",
    "video_id",
    "video_chunk_id",
    "parse_task_id",
    "content_version",
    "index_generation",
    "chunk_start_seconds",
    "chunk_end_seconds",
)


class ElasticsearchIndexSchemaError(RuntimeError):
    """The configured physical index has an incompatible search contract."""


class ElasticsearchDocumentConsistencyError(RuntimeError):
    """Elasticsearch returned a document outside the authorized task scope."""


class ElasticsearchBulkWriteError(RuntimeError):
    """At least one action in an acknowledged bulk request failed."""

    def __init__(
        self,
        failures: Mapping[str, str],
        *,
        status_codes: Mapping[str, int | None] | None = None,
    ) -> None:
        self.failures = dict(failures)
        provided_statuses = status_codes or {}
        self.status_codes: dict[str, int | None] = {}
        for document_id in self.failures:
            status = provided_statuses.get(document_id)
            self.status_codes[document_id] = (
                status
                if isinstance(status, int) and not isinstance(status, bool)
                else None
            )
        self.version_conflict_ids = frozenset(
            document_id
            for document_id, status in self.status_codes.items()
            if status == 409
        )
        super().__init__(f"Elasticsearch bulk failed for {len(failures)} document(s)")


@dataclass(frozen=True, slots=True)
class StoredDocumentState:
    present: bool
    workspace_id: str | None = None
    series_id: str | None = None
    video_id: str | None = None
    parse_task_id: str | None = None
    video_chunk_id: str | None = None
    content_version: int | None = None
    index_generation: int | None = None
    lexical_fields_present: bool = False
    semantic_text_present: bool = False
    semantic_vector_present: bool = False
    vision_vector_present: bool = False
    semantic_vector: tuple[float, ...] | None = None
    vision_vector: tuple[float, ...] | None = None
    sparse_token_weights: tuple[tuple[str, float], ...] | None = None


@dataclass(frozen=True, slots=True)
class VideoChunkIndexMatch:
    rank: int
    score: float
    workspace_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    index_generation: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    matched_fields: tuple[LexicalField, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _TrustedScope:
    query: dict[str, Any]
    workspace_id: str
    series_ids: frozenset[str]
    parse_tasks_by_video_id: Mapping[str, str]
    index_generation: int


def document_id(parse_task_id: str, video_chunk_id: str) -> str:
    """Keep one stable document per parse task and Chunk across content updates."""

    return (
        f"{canonical_uuid(parse_task_id, 'parse_task_id')}:"
        f"{canonical_uuid(video_chunk_id, 'video_chunk_id')}"
    )


def _external_version(content_version: int, index_generation: int) -> int:
    for name, value, maximum in (
        ("content_version", content_version, _MAX_CONTENT_VERSION),
        ("index_generation", index_generation, _MAX_INDEX_GENERATION),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} is outside the Elasticsearch version contract")
    # Generation is the deployment/index-contract epoch. A newer epoch must
    # dominate every content version written by an older Worker; content stays
    # monotonic within one epoch.
    version = (index_generation << _EXTERNAL_VERSION_SHIFT) | content_version
    if version > _MAX_EXTERNAL_VERSION:  # Defensive if the packing changes.
        raise ValueError("composite Elasticsearch version is too large")
    return version


class ElasticsearchVideoChunkStore:
    """Translate Engine search views into one physical Elasticsearch index.

    Callers authorize each video/task pair and enforce task readiness in MySQL.
    This adapter applies that scope; it does not select tasks or read task status.
    """

    def __init__(
        self,
        client: Elasticsearch,
        *,
        index_name: str,
        environment: DatabaseEnvironment | str,
        semantic_dimensions: int,
        vision_dimensions: int,
    ) -> None:
        self.index_name = require_nonempty_text(index_name, "index_name")
        if self.index_name != self.index_name.lower():
            raise ValueError("index_name must be lowercase")
        self.environment = DatabaseEnvironment(environment)
        parts = set(self.index_name.replace("-", "_").split("_"))
        if self.environment is DatabaseEnvironment.TEST and "test" not in parts:
            raise ValueError("test index_name must contain a test component")
        if self.environment is DatabaseEnvironment.DEVELOPMENT and not parts.intersection(
            {"dev", "development"}
        ):
            raise ValueError("development index_name must contain a dev component")
        if self.environment is DatabaseEnvironment.PRODUCTION and parts.intersection(
            {"test", "dev", "development"}
        ):
            raise ValueError("production index_name cannot name a test or dev index")
        for name, value in (
            ("semantic_dimensions", semantic_dimensions),
            ("vision_dimensions", vision_dimensions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096:
                raise ValueError(f"{name} must be an integer between 1 and 4096")
        self._client = client
        self.semantic_dimensions = semantic_dimensions
        self.vision_dimensions = vision_dimensions

    def ensure_index(self) -> None:
        """Create only an absent index, then verify its actual mapping contract."""

        version = self._client.info().get("version", {}).get("number", "")
        if not isinstance(version, str) or not version.startswith("8.19."):
            raise ElasticsearchIndexSchemaError("Elasticsearch 8.19 is required")
        expected = self._mapping()
        if not self._client.indices.exists(index=self.index_name):
            try:
                self._client.indices.create(index=self.index_name, mappings=expected)
            except BadRequestError as exc:
                if exc.error != "resource_already_exists_exception":
                    raise
        actual = self._client.indices.get_mapping(index=self.index_name)
        mapping = actual.get(self.index_name, {}).get("mappings", {})
        source_mapping = mapping.get("_source", {})
        if (
            not isinstance(source_mapping, Mapping)
            or source_mapping.get("enabled") is False
            or source_mapping.get("mode") not in (None, "stored")
            or source_mapping.get("includes")
            or source_mapping.get("excludes")
        ):
            raise ElasticsearchIndexSchemaError("index must retain the complete stored _source")
        if mapping.get("_meta") != expected["_meta"]:
            raise ElasticsearchIndexSchemaError("index contract metadata differs")
        for name, specification in expected["properties"].items():
            current = mapping.get("properties", {}).get(name)
            if not isinstance(current, Mapping) or not _mapping_contains(current, specification):
                raise ElasticsearchIndexSchemaError(f"index field {name!r} differs")

    def bulk_upsert(
        self,
        documents: Sequence[VideoChunkIndexDocument],
        *,
        refresh: Literal[False, "wait_for"] = False,
    ) -> list[str]:
        """Write deterministic IDs; report every failed item, including malformed replies."""

        if refresh is not False and refresh != "wait_for":
            raise ValueError("refresh must be False or 'wait_for'")
        if not documents:
            return []
        self.ensure_index()
        operations: list[dict[str, Any]] = []
        expected_ids: list[str] = []
        for document in documents:
            source = self._document_source(document)
            physical_id = document_id(document.parse_task_id, document.video_chunk_id)
            if physical_id in expected_ids:
                raise ValueError("bulk documents contain duplicate task/Chunk identity")
            expected_ids.append(physical_id)
            operations.extend(
                ({"index": {
                    "_index": self.index_name,
                    "_id": physical_id,
                    "version": _external_version(
                        document.content_version,
                        document.index_generation,
                    ),
                    "version_type": "external",
                }}, source)
            )
        response = self._client.bulk(operations=operations, refresh=refresh)
        items = response.get("items")
        if not isinstance(items, list) or len(items) != len(expected_ids):
            raise ElasticsearchBulkWriteError(
                {item: "missing or malformed bulk item response" for item in expected_ids}
            )
        failures: dict[str, str] = {}
        status_codes: dict[str, int | None] = {}
        for expected_id, item in zip(expected_ids, items, strict=True):
            action = item.get("index") if isinstance(item, Mapping) else None
            if not isinstance(action, Mapping) or action.get("_id") != expected_id:
                failures[expected_id] = "bulk item identity differs"
                status_codes[expected_id] = None
                continue
            status = action.get("status")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 200 <= status < 300
                or action.get("error")
            ):
                failures[expected_id] = str(action.get("error") or status)
                status_codes[expected_id] = (
                    status
                    if isinstance(status, int) and not isinstance(status, bool)
                    else None
                )
        if failures or response.get("errors"):
            if not failures:
                failures = {
                    item: "bulk reported errors without failed items"
                    for item in expected_ids
                }
            raise ElasticsearchBulkWriteError(
                failures,
                status_codes=status_codes,
            )
        return expected_ids

    def inspect(
        self, keys: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], StoredDocumentState]:
        """Read current storage state; this real-time read does not prove search visibility."""

        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return {}
        ids = [document_id(parse_task_id, chunk_id) for parse_task_id, chunk_id in ordered]
        states = {key: StoredDocumentState(present=False) for key in ordered}
        if not self._client.indices.exists(index=self.index_name):
            return states
        response = self._client.mget(index=self.index_name, ids=ids)
        docs = response.get("docs")
        if not isinstance(docs, list) or len(docs) != len(ids):
            raise ElasticsearchDocumentConsistencyError("mget response is incomplete")
        for key, expected_id, item in zip(ordered, ids, docs, strict=True):
            if not isinstance(item, Mapping) or item.get("_id") != expected_id:
                raise ElasticsearchDocumentConsistencyError("mget document identity differs")
            if item.get("error"):
                raise ElasticsearchDocumentConsistencyError("mget document read failed")
            if not item.get("found"):
                continue
            source = item.get("_source")
            if not isinstance(source, Mapping):
                raise ElasticsearchDocumentConsistencyError("mget document has no source")
            self._validate_identity(expected_id, source)
            semantic_vector = (
                tuple(float(value) for value in source["semantic_vector"])
                if _valid_stored_vector(
                    source.get("semantic_vector"), self.semantic_dimensions
                )
                else None
            )
            vision_vector = (
                tuple(float(value) for value in source["vision_vector"])
                if _valid_stored_vector(
                    source.get("vision_vector"), self.vision_dimensions
                )
                else None
            )
            states[key] = StoredDocumentState(
                present=True,
                workspace_id=source.get("workspace_id"),
                series_id=source.get("series_id"),
                video_id=source.get("video_id"),
                parse_task_id=source.get("parse_task_id"),
                video_chunk_id=source.get("video_chunk_id"),
                content_version=source.get("content_version"),
                index_generation=source.get("index_generation"),
                lexical_fields_present=isinstance(source.get("lexical_fields"), list),
                semantic_text_present=isinstance(source.get("semantic_text"), str),
                semantic_vector_present=semantic_vector is not None,
                vision_vector_present=vision_vector is not None,
                semantic_vector=semantic_vector,
                vision_vector=vision_vector,
                sparse_token_weights=_stored_sparse_weights(source.get("sparse_vector")),
            )
        return states

    def inspect_exact(
        self, documents: Sequence[VideoChunkIndexDocument],
    ) -> dict[str, bool]:
        """Real-time read whether each stored source exactly matches its document."""

        expected: dict[str, dict[str, Any]] = {}
        for document in documents:
            physical_id = document_id(document.parse_task_id, document.video_chunk_id)
            if physical_id in expected:
                raise ValueError("documents contain duplicate task/Chunk identity")
            expected[physical_id] = self._document_source(document)
        matches = {document_id: False for document_id in expected}
        if not expected or not self._client.indices.exists(index=self.index_name):
            return matches
        response = self._client.mget(index=self.index_name, ids=list(expected))
        docs = response.get("docs")
        if not isinstance(docs, list) or len(docs) != len(expected):
            raise ElasticsearchDocumentConsistencyError("mget response is incomplete")
        for expected_id, item in zip(expected, docs, strict=True):
            if not isinstance(item, Mapping) or item.get("_id") != expected_id:
                raise ElasticsearchDocumentConsistencyError("mget document identity differs")
            if item.get("error"):
                raise ElasticsearchDocumentConsistencyError("mget document read failed")
            if not item.get("found"):
                continue
            source = item.get("_source")
            if not isinstance(source, Mapping):
                raise ElasticsearchDocumentConsistencyError("mget document has no source")
            self._validate_identity(expected_id, source)
            matches[expected_id] = dict(source) == expected[expected_id]
        return matches

    def is_ready(self, documents: Sequence[VideoChunkIndexDocument]) -> bool:
        """Return whether the exact documents are visible before an upsert.

        A visible document with the same stable ID but a different source needs
        an idempotent overwrite.  That is a normal preflight miss, whereas the
        same mismatch after ``refresh=wait_for`` is a consistency failure.
        """

        return self._query_ready(documents, mismatch_is_error=False)

    def verify_ready(self, documents: Sequence[VideoChunkIndexDocument]) -> bool:
        """Strictly verify exact query-visible sources after an upsert."""

        return self._query_ready(documents, mismatch_is_error=True)

    def _query_ready(
        self,
        documents: Sequence[VideoChunkIndexDocument],
        *,
        mismatch_is_error: bool,
    ) -> bool:

        if not documents:
            raise ValueError("verify_ready requires the complete expected Chunk set")
        for document in documents:
            expected = self._document_source(document)
            physical_id = document_id(
                document.parse_task_id,
                document.video_chunk_id,
            )
            try:
                response = self._client.search(
                    index=self.index_name,
                    query={"bool": {"filter": [
                        {"ids": {"values": [physical_id]}},
                        {"term": {"workspace_id": document.workspace_id}},
                        {"term": {"video_id": document.video_id}},
                        {"term": {"parse_task_id": document.parse_task_id}},
                        {"term": {"content_version": document.content_version}},
                        {"term": {"index_generation": document.index_generation}},
                    ]}},
                    size=1,
                    source=True,
                )
            except NotFoundError:
                return False
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                return False
            hit = hits[0]
            if hit.get("_id") != physical_id or hit.get("_source") != expected:
                if not mismatch_is_error:
                    return False
                raise ElasticsearchDocumentConsistencyError(
                    f"search-visible document differs: {physical_id}"
                )
        return True

    def delete(
        self, *, parse_task_id: str, video_chunk_id: str,
        refresh: Literal[False, "wait_for"] = False,
    ) -> None:
        """Delete one parse task's Chunk document."""

        if refresh is not False and refresh != "wait_for":
            raise ValueError("refresh must be False or 'wait_for'")
        try:
            self._client.delete(
                index=self.index_name,
                id=document_id(parse_task_id, video_chunk_id),
                refresh=refresh,
            )
        except NotFoundError:
            pass

    def bulk_delete(
        self,
        keys: Sequence[tuple[str, str]],
        *,
        refresh: Literal[False, "wait_for"] = False,
    ) -> list[str]:
        """Delete exact Parse-task/Chunk documents in one refresh boundary."""

        if refresh is not False and refresh != "wait_for":
            raise ValueError("refresh must be False or 'wait_for'")
        ordered = list(dict.fromkeys(keys))
        expected_ids = [
            document_id(parse_task_id, chunk_id)
            for parse_task_id, chunk_id in ordered
        ]
        if not expected_ids or not self._client.indices.exists(index=self.index_name):
            return expected_ids
        operations = [
            {"delete": {"_index": self.index_name, "_id": expected_id}}
            for expected_id in expected_ids
        ]
        response = self._client.bulk(operations=operations, refresh=refresh)
        items = response.get("items")
        if not isinstance(items, list) or len(items) != len(expected_ids):
            raise ElasticsearchBulkWriteError({
                item: "missing or malformed bulk item response"
                for item in expected_ids
            })
        failures: dict[str, str] = {}
        for expected_id, item in zip(expected_ids, items, strict=True):
            action = item.get("delete") if isinstance(item, Mapping) else None
            if not isinstance(action, Mapping) or action.get("_id") != expected_id:
                failures[expected_id] = "bulk item identity differs"
                continue
            status = action.get("status")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or (status != 404 and not 200 <= status < 300)
                or (action.get("error") and status != 404)
            ):
                failures[expected_id] = str(action.get("error") or status)
        if failures:
            raise ElasticsearchBulkWriteError(failures)
        return expected_ids

    def verify_absent(self, keys: Sequence[tuple[str, str]]) -> bool:
        """Check both real-time storage and search visibility after cleanup."""

        ordered = list(dict.fromkeys(keys))
        if not ordered or not self._client.indices.exists(index=self.index_name):
            return True
        if any(state.present for state in self.inspect(ordered).values()):
            return False
        response = self._client.search(
            index=self.index_name,
            query={"ids": {"values": [document_id(parse_task_id, chunk_id)
                                      for parse_task_id, chunk_id in ordered]}},
            size=1,
            source=False,
        )
        hits = response.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise ElasticsearchDocumentConsistencyError("delete verification has no search hits")
        return not hits

    def recall(self, request: RecallRequest, *, exact: bool = False) -> tuple[RawChunkHit, ...]:
        """Execute one Engine request without changing its scope, channel or budget."""

        if not isinstance(request, RecallRequest):
            raise ValueError("recall requires an Engine RecallRequest")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        if not request.scope.tasks:
            return ()
        scope = {
            "workspace_id": request.scope.workspace_id,
            "series_ids": tuple(task.series_id for task in request.scope.tasks),
            "parse_tasks_by_video_id": {task.video_id: task.parse_task_id for task in request.scope.tasks},
            "index_generation": request.index_generation,
            "limit": request.limit,
        }
        if request.channel == "lexical":
            if request.vector is not None or request.token_weights is not None:
                raise ValueError("lexical request must contain only text")
            matches = self.search_lexical(request.text, **scope)
        elif request.channel in ("semantic", "vision"):
            if request.text is not None or request.token_weights is not None:
                raise ValueError("dense request must contain only a vector")
            search = self.search_semantic if request.channel == "semantic" else self.search_vision
            matches = search(request.vector, **scope, exact=exact)
        elif request.channel == "sparse":
            if request.text is not None or request.vector is not None or request.token_weights is None:
                raise ValueError("sparse request must contain only token weights")
            matches = self.search_sparse(dict(request.token_weights), **scope)
        else:
            raise ValueError("unsupported recall channel")
        tasks = {task.video_id: task for task in request.scope.tasks}
        hits = []
        for match in matches:
            if match.series_id != tasks[match.video_id].series_id:
                raise ElasticsearchDocumentConsistencyError("search hit differs from the selected video's series")
            hits.append(RawChunkHit(
                channel=request.channel, video_chunk_id=match.video_chunk_id,
                retrieval_identity=VideoChunkRetrievalIdentity(
                    workspace_id=match.workspace_id, series_id=match.series_id,
                    video_id=match.video_id, parse_task_id=match.parse_task_id,
                    content_version=match.content_version, index_generation=match.index_generation,
                ), rank=match.rank, score=match.score,
                start_seconds=match.chunk_start_seconds, end_seconds=match.chunk_end_seconds,
                lexical_matches=match.matched_fields,
            ))
        return tuple(hits)

    def search_lexical(
        self,
        query: str,
        *,
        workspace_id: str,
        series_ids: Sequence[str],
        parse_tasks_by_video_id: Mapping[str, str],
        index_generation: int,
        field_weights: Mapping[str, float] | None = None,
        phrase_fields: Sequence[str] | None = None,
        limit: int,
    ) -> list[VideoChunkIndexMatch]:
        text = require_nonempty_text(query, "query")
        weights = LEXICAL_FIELD_WEIGHTS if field_weights is None else field_weights
        if not weights:
            raise ValueError("field_weights must not be empty")
        phrase_names = set(LEXICAL_PHRASE_FIELDS if phrase_fields is None else phrase_fields)
        unknown = (set(weights) | phrase_names) - set(LEXICAL_FIELD_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown lexical field(s): {sorted(unknown)}")
        clauses = []
        for field_name, weight in weights.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
                raise ValueError("field weights must be positive finite numbers")
            for match_kind, boost in (("match", float(weight)), ("match_phrase", float(weight) * 2.0)):
                if match_kind == "match_phrase" and field_name not in phrase_names:
                    continue
                clauses.append({"bool": {"filter": {"term": {"lexical_fields.field": field_name}}, "must": {
                    match_kind: {"lexical_fields.raw_text": {"query": text, "boost": boost}}
                }}})
        scope = self._scope_query(
            workspace_id, series_ids, parse_tasks_by_video_id, index_generation
        )
        response = self._client.search(
            index=self.index_name,
            query={"bool": {"filter": [scope.query], "must": [{"nested": {
                "path": "lexical_fields",
                "query": {"bool": {"should": clauses, "minimum_should_match": 1}},
                "score_mode": "max",
                "inner_hits": {"name": "matched_lexical_fields", "size": 32},
            }}]}},
            size=_positive_limit(limit),
            source_includes=list(_SOURCE_FIELDS),
        )
        return self._parse_hits(response, scope, lexical=True)

    def search_semantic(
        self, query_vector: Sequence[float], *, workspace_id: str,
        series_ids: Sequence[str], parse_tasks_by_video_id: Mapping[str, str],
        index_generation: int, limit: int,
        exact: bool = False,
    ) -> list[VideoChunkIndexMatch]:
        return self._search_vector(
            query_vector, "semantic_vector", self.semantic_dimensions,
            workspace_id=workspace_id, series_ids=series_ids,
            parse_tasks_by_video_id=parse_tasks_by_video_id,
            index_generation=index_generation, limit=limit, exact=exact,
        )

    def search_vision(
        self, query_vector: Sequence[float], *, workspace_id: str,
        series_ids: Sequence[str], parse_tasks_by_video_id: Mapping[str, str],
        index_generation: int, limit: int,
        exact: bool = False,
    ) -> list[VideoChunkIndexMatch]:
        return self._search_vector(
            query_vector, "vision_vector", self.vision_dimensions,
            workspace_id=workspace_id, series_ids=series_ids,
            parse_tasks_by_video_id=parse_tasks_by_video_id,
            index_generation=index_generation, limit=limit, exact=exact,
        )

    def _search_vector(
        self, query_vector: Sequence[float], vector_field: str, dimensions: int,
        *, workspace_id: str, series_ids: Sequence[str],
        parse_tasks_by_video_id: Mapping[str, str], index_generation: int,
        limit: int, exact: bool,
    ) -> list[VideoChunkIndexMatch]:
        vector = _validated_vector(query_vector, dimensions, "query_vector")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        scope = self._scope_query(
            workspace_id, series_ids, parse_tasks_by_video_id, index_generation
        )
        filters = [scope.query, {"exists": {"field": vector_field}}]
        k = _positive_limit(limit)
        if not exact and k > 10_000:
            raise ValueError("approximate vector limit must not exceed 10000")
        if exact:
            response = self._client.search(
                index=self.index_name,
                query={"script_score": {
                    "query": {"bool": {"filter": filters}},
                    "script": {
                        "source": f"cosineSimilarity(params.vector, '{vector_field}') + 1.0",
                        "params": {"vector": vector},
                    },
                }},
                size=k,
                source_includes=list(_SOURCE_FIELDS),
            )
        else:
            response = self._client.search(
                index=self.index_name,
                knn={
                    "field": vector_field,
                    "query_vector": vector,
                    "k": k,
                    "num_candidates": min(10_000, max(100, k * 4)),
                    "filter": {"bool": {"filter": filters}},
                },
                size=k,
                source_includes=list(_SOURCE_FIELDS),
            )
        return self._parse_hits(response, scope, lexical=False)

    def search_sparse(
        self, query_weights: Mapping[str, float], *, workspace_id: str,
        series_ids: Sequence[str], parse_tasks_by_video_id: Mapping[str, str],
        index_generation: int, limit: int,
    ) -> list[VideoChunkIndexMatch]:
        weights = dict(validate_sparse_weights(query_weights))
        scope = self._scope_query(workspace_id, series_ids, parse_tasks_by_video_id, index_generation)
        size = _positive_limit(limit)
        if not weights:
            return []
        response = self._client.search(
            index=self.index_name, query={"bool": {
                "filter": [scope.query],
                "must": [{"sparse_vector": {
                    "field": "sparse_vector", "query_vector": weights, "prune": False,
                }}],
            }}, size=size, source_includes=list(_SOURCE_FIELDS),
        )
        return self._parse_hits(response, scope, lexical=False)

    def _scope_query(
        self, workspace_id: str, series_ids: Sequence[str],
        parse_tasks_by_video_id: Mapping[str, str], index_generation: int,
    ) -> _TrustedScope:
        workspace = require_nonempty_text(workspace_id, "workspace_id")
        series = list(dict.fromkeys(canonical_uuid(item, "series_id") for item in series_ids))
        if not series:
            raise ValueError("series_ids must not be empty")
        tasks = {
            canonical_uuid(video_id, "video_id"): canonical_uuid(parse_task_id, "parse_task_id")
            for video_id, parse_task_id in parse_tasks_by_video_id.items()
        }
        if not tasks:
            raise ValueError("parse_tasks_by_video_id must not be empty")
        _external_version(1, index_generation)
        pairs = [
            {"bool": {"filter": [
                {"term": {"video_id": video_id}},
                {"term": {"parse_task_id": parse_task_id}},
            ]}}
            for video_id, parse_task_id in tasks.items()
        ]
        query = {
            "bool": {"filter": [
                {"term": {"workspace_id": workspace}},
                {"terms": {"series_id": series}},
                {"term": {"index_generation": index_generation}},
                {"bool": {"should": pairs, "minimum_should_match": 1}},
            ]}
        }
        return _TrustedScope(
            query=query,
            workspace_id=workspace,
            series_ids=frozenset(series),
            parse_tasks_by_video_id=tasks,
            index_generation=index_generation,
        )

    def _parse_hits(
        self, response: Mapping[str, Any], scope: _TrustedScope, *, lexical: bool,
    ) -> list[VideoChunkIndexMatch]:
        hits = response.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise ElasticsearchDocumentConsistencyError("search response has no hits")
        parsed = []
        seen_ids: set[str] = set()
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, Mapping) else None
            if not isinstance(source, Mapping):
                raise ElasticsearchDocumentConsistencyError("search hit has no source")
            hit_id = hit.get("_id")
            self._validate_identity(hit_id, source)
            video_id = source["video_id"]
            if (
                source.get("workspace_id") != scope.workspace_id
                or source.get("series_id") not in scope.series_ids
                or source.get("index_generation") != scope.index_generation
                or scope.parse_tasks_by_video_id.get(video_id) != source["parse_task_id"]
            ):
                raise ElasticsearchDocumentConsistencyError("search hit is outside the authorized video/task scope")
            if hit_id in seen_ids:
                raise ElasticsearchDocumentConsistencyError("duplicate search hit")
            score = hit.get("_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ElasticsearchDocumentConsistencyError("search hit score is invalid")
            matched_fields: tuple[LexicalField, ...] = ()
            if lexical:
                inner = hit.get("inner_hits", {}).get("matched_lexical_fields", {}).get("hits", {}).get("hits")
                if not isinstance(inner, list) or not inner:
                    raise ElasticsearchDocumentConsistencyError("lexical hit lacks matching field sources")
                try:
                    matched_fields = tuple(
                        LexicalField.model_validate(item["_source"]) for item in inner
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ElasticsearchDocumentConsistencyError("lexical field source is invalid") from exc
            seen_ids.add(hit_id)
            parsed.append((hit_id, float(score), source, matched_fields))
        parsed.sort(key=lambda item: (-item[1], item[0]))
        return [
            VideoChunkIndexMatch(
                rank=rank, score=score,
                workspace_id=source["workspace_id"],
                series_id=source["series_id"], video_id=source["video_id"],
                video_chunk_id=source["video_chunk_id"],
                parse_task_id=source["parse_task_id"],
                content_version=source["content_version"],
                index_generation=source["index_generation"],
                chunk_start_seconds=source["chunk_start_seconds"],
                chunk_end_seconds=source["chunk_end_seconds"],
                matched_fields=matched_fields,
            )
            for rank, (_, score, source, matched_fields) in enumerate(parsed, start=1)
        ]

    def _validate_identity(self, hit_id: str, source: Mapping[str, Any]) -> None:
        try:
            if hit_id != document_id(source["parse_task_id"], source["video_chunk_id"]):
                raise ValueError("document ID differs")
            require_nonempty_text(source["workspace_id"], "workspace_id")
            for name in ("series_id", "video_id"):
                canonical_uuid(source[name], name)
            version = source["content_version"]
            generation = source["index_generation"]
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or not 1 <= version <= _MAX_CONTENT_VERSION
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or not 1 <= generation <= _MAX_INDEX_GENERATION
            ):
                raise ValueError("invalid content_version")
            start = source["chunk_start_seconds"]
            end = source["chunk_end_seconds"]
            if (
                isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or not 0 <= start < end
            ):
                raise ValueError("invalid Chunk time range")
        except (KeyError, TypeError, ValueError) as exc:
            raise ElasticsearchDocumentConsistencyError("stored document identity is invalid") from exc

    def _document_source(self, document: VideoChunkIndexDocument) -> dict[str, Any]:
        require_nonempty_text(document.workspace_id, "workspace_id")
        canonical_uuid(document.series_id, "series_id")
        canonical_uuid(document.video_id, "video_id")
        document_id(document.parse_task_id, document.video_chunk_id)
        if (
            isinstance(document.content_version, bool)
            or not isinstance(document.content_version, int)
            or not 1 <= document.content_version <= _MAX_CONTENT_VERSION
        ):
            raise ValueError("content_version must fit a positive SQL integer")
        _external_version(document.content_version, document.index_generation)
        if (
            isinstance(document.chunk_start_seconds, bool)
            or isinstance(document.chunk_end_seconds, bool)
            or not isinstance(document.chunk_start_seconds, int)
            or not isinstance(document.chunk_end_seconds, int)
            or not 0 <= document.chunk_start_seconds < document.chunk_end_seconds
        ):
            raise ValueError("Chunk interval is invalid")
        projection = ChunkIndexProjection.model_validate(document.projection)
        if any(
            item.start_seconds >= document.chunk_end_seconds
            or item.end_seconds <= document.chunk_start_seconds
            for item in projection.lexical_fields
        ):
            raise ValueError("lexical field does not overlap its Chunk")
        if bool(projection.semantic_text.strip()) != (document.semantic_vector is not None):
            raise ValueError("semantic vector presence must match semantic text")
        semantic_vector = (
            _validated_vector(document.semantic_vector, self.semantic_dimensions, "semantic_vector")
            if document.semantic_vector is not None else None
        )
        vision_vector = _validated_vector(
            document.vision_vector, self.vision_dimensions, "vision_vector"
        )
        source = {
            "workspace_id": document.workspace_id,
            "series_id": document.series_id,
            "video_id": document.video_id,
            "video_chunk_id": document.video_chunk_id,
            "parse_task_id": document.parse_task_id,
            "content_version": document.content_version,
            "index_generation": document.index_generation,
            "chunk_start_seconds": document.chunk_start_seconds,
            "chunk_end_seconds": document.chunk_end_seconds,
            "lexical_fields": [item.model_dump(mode="json") for item in projection.lexical_fields],
            "semantic_text": projection.semantic_text,
            "semantic_vector": semantic_vector,
            "vision_vector": vision_vector,
            "ordered_frames_manifest": (
                document.ordered_frames_manifest.as_dict()
                if document.ordered_frames_manifest is not None else None
            ),
        }
        if document.sparse_token_weights is not None:
            source["sparse_vector"] = dict(validate_sparse_weights(dict(document.sparse_token_weights)))
        return source

    def _mapping(self) -> dict[str, Any]:
        return {
            "dynamic": "strict",
            "_meta": {
                "index_contract_version": INDEX_CONTRACT_VERSION,
                "projection_version": INDEX_PROJECTION_VERSION,
                "analysis_contract_version": ANALYSIS_CONTRACT_VERSION,
                "media_time_unit": "milliseconds",
            },
            "properties": {
                **{name: {"type": "keyword"} for name in (
                    "workspace_id", "series_id", "video_id", "video_chunk_id",
                    "parse_task_id",
                )},
                "content_version": {"type": "integer"},
                "index_generation": {"type": "integer"},
                "chunk_start_seconds": {"type": "long"},
                "chunk_end_seconds": {"type": "long"},
                "semantic_text": {"type": "text", "index": False},
                "sparse_vector": {"type": "sparse_vector"},
                "ordered_frames_manifest": {"type": "object", "enabled": False},
                "lexical_fields": {"type": "nested", "dynamic": "strict", "properties": {
                    "field": {"type": "keyword"},
                    "raw_text": {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"},
                    "language": {"type": "keyword"},
                    "role": {"type": "keyword"},
                    "speaker": {"type": "keyword"},
                    "start_seconds": {"type": "long"},
                    "end_seconds": {"type": "long"},
                    "timeline_index": {"type": "integer"},
                    "enrichment_index": {"type": "integer"},
                }},
                "semantic_vector": {"type": "dense_vector", "dims": self.semantic_dimensions,
                                    "index": True, "similarity": "cosine",
                                    "index_options": {"type": "hnsw"}},
                "vision_vector": {"type": "dense_vector", "dims": self.vision_dimensions,
                                  "index": True, "similarity": "cosine",
                                  "index_options": {"type": "hnsw"}},
            },
        }


def _validated_vector(value: Sequence[float], dimensions: int, name: str) -> list[float]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a numeric vector")
    try:
        vector = list(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a numeric vector") from exc
    if len(vector) != dimensions or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in vector
    ) or not any(item != 0 for item in vector):
        raise ValueError(f"{name} must contain {dimensions} finite, nonzero dimensions")
    return [float(item) for item in vector]


def _stored_sparse_weights(value: object) -> tuple[tuple[str, float], ...] | None:
    try:
        return validate_sparse_weights(value)
    except ValueError:
        return None


def _valid_stored_vector(value: object, dimensions: int) -> bool:
    try:
        _validated_vector(value, dimensions, "stored vector")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _positive_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("limit must be a positive integer")
    return value


def _mapping_contains(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    # ES omits search_analyzer when it inherits the field's indexing analyzer.
    if (
        actual.get("type") == expected.get("type") == "text"
        and "search_analyzer" in expected
        and "search_analyzer" not in actual
        and "analyzer" in actual
    ):
        actual = {**actual, "search_analyzer": actual["analyzer"]}
    return all(
        key in actual and (
            _mapping_contains(actual[key], value)
            if isinstance(value, Mapping) and isinstance(actual[key], Mapping)
            else actual[key] == value
        )
        for key, value in expected.items()
    )
