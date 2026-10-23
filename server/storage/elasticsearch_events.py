"""Elasticsearch projection of formal Timeline Events; MySQL owns the facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from elasticsearch import BadRequestError, Elasticsearch

from engine.rag.indexing.events import EventIndexDocument, EventIndexingContract, compute_event_id
from engine.rag.retrieval.contracts import EventFilters, EventIndexNotReadyError, RawEventHit

from ..config.elasticsearch import validate_index_name
from ..database import DatabaseEnvironment
from ..utils.validation import canonical_uuid
from .elasticsearch import (
    ElasticsearchBulkWriteError, ElasticsearchDocumentConsistencyError,
    ElasticsearchIndexSchemaError, _mapping_contains, _positive_limit, _validated_vector,
)

EVENT_INDEX_CONTRACT_VERSION = "seekino-video-event-es-2"
EventSource = tuple[str, str, int]
_HIT_SOURCE_FIELDS = (
    "video_chunk_id", "parse_task_id", "version", "timeline_index", "event_type", "speaker", "lexical_fields",
)


class ElasticsearchVideoEventStore:
    """Borrow one Caller client; every operation retains exact SQL source tuples."""

    def __init__(
        self, client: Elasticsearch, *, index_name: str,
        environment: DatabaseEnvironment | str, semantic_dimensions: int,
        vision_dimensions: int,
    ) -> None:
        validate_index_name(index_name, environment)
        for value in (semantic_dimensions, vision_dimensions):
            if type(value) is not int or not 1 <= value <= 4096:
                raise ValueError("Event vector dimensions must be integers between 1 and 4096")
        self._client = client
        self.index_name = index_name
        self.semantic_dimensions = semantic_dimensions
        self.vision_dimensions = vision_dimensions
        self._contract: EventIndexingContract | None = None

    def bind_contract(self, contract: EventIndexingContract, *, create: bool = False) -> None:
        contract = EventIndexingContract.model_validate(contract)
        if (contract.semantic_encoder.dimensions != self.semantic_dimensions
                or contract.vision_encoder.dimensions != self.vision_dimensions):
            raise ElasticsearchIndexSchemaError("Event contract and store dimensions differ")
        if self._contract is not None and self._contract.contract_id != contract.contract_id:
            raise ElasticsearchIndexSchemaError("one Event store cannot change its fixed contract")
        expected = self._mapping(contract)
        if not self._client.indices.exists(index=self.index_name):
            if not create:
                raise EventIndexNotReadyError("Event index is not initialized; continue with Chunk retrieval")
            version = self._client.info().get("version", {}).get("number", "")
            if not isinstance(version, str) or not version.startswith("8.19."):
                raise ElasticsearchIndexSchemaError("Elasticsearch 8.19 is required")
            try:
                self._client.indices.create(index=self.index_name, mappings=expected)
            except BadRequestError as error:
                if error.error != "resource_already_exists_exception":
                    raise
        actual = self._client.indices.get_mapping(index=self.index_name)
        mapping = actual.get(self.index_name, {}).get("mappings", {})
        source = mapping.get("_source", {})
        if (not isinstance(source, Mapping)
                or mapping.get("dynamic") != "strict" or mapping.get("_meta") != expected["_meta"]
                or source.get("enabled") is False or source.get("includes") or source.get("excludes")
                or source.get("mode") not in (None, "stored")
                or set(mapping.get("properties", {})) != set(expected["properties"])):
            raise ElasticsearchIndexSchemaError("Event index contract metadata or source differs")
        for name, specification in expected["properties"].items():
            current = mapping.get("properties", {}).get(name)
            if (not isinstance(current, Mapping) or not _mapping_contains(current, specification)
                    or name == "semantic_vectors"
                    and set(current.get("properties", {})) != set(specification["properties"])):
                raise ElasticsearchIndexSchemaError(f"Event index field {name!r} differs")
        self._contract = contract

    def ensure_index(self, contract: EventIndexingContract | None = None) -> None:
        selected = contract if contract is not None else self._contract
        if selected is None:
            raise ValueError("Event index initialization requires a frozen contract")
        self.bind_contract(selected, create=True)

    def bulk_upsert(
        self, documents: Sequence[EventIndexDocument], *, write_version: int | None = None,
        refresh: Literal[False, "wait_for"] = False,
    ) -> list[str]:
        if refresh is not False and refresh != "wait_for":
            raise ValueError("refresh must be False or 'wait_for'")
        if write_version is not None and (type(write_version) is not int or write_version < 1):
            raise ValueError("write_version must be a positive lease version")
        if not documents:
            return []
        self.ensure_index()
        operations, ids = [], []
        for value in documents:
            document = self._validate_document(value)
            if document.event_id in ids:
                raise ValueError("duplicate Event ID in bulk request")
            ids.append(document.event_id)
            action: dict[str, Any] = {"_index": self.index_name, "_id": document.event_id}
            if write_version is not None:
                action.update(version=write_version, version_type="external_gte")
            operations.extend(({"index": action}, document.as_source()))
        response = self._client.bulk(operations=operations, refresh=refresh)
        items = response.get("items")
        if not isinstance(items, list) or len(items) != len(ids):
            raise ElasticsearchBulkWriteError({key: "missing or malformed bulk response" for key in ids})
        failures, statuses = {}, {}
        for key, item in zip(ids, items, strict=True):
            result = item.get("index") if isinstance(item, Mapping) else None
            status = result.get("status") if isinstance(result, Mapping) else None
            if (not isinstance(result, Mapping) or result.get("_id") != key
                    or type(status) is not int or not 200 <= status < 300 or result.get("error")):
                failures[key] = str(result.get("error") or status) if isinstance(result, Mapping) else "malformed bulk item"
                statuses[key] = status if type(status) is int else None
        if failures or response.get("errors"):
            raise ElasticsearchBulkWriteError(
                failures or {key: "bulk reported errors" for key in ids}, status_codes=statuses,
            )
        return ids

    def inspect(self, ids: Sequence[str]) -> dict[str, EventIndexDocument | None]:
        ordered = tuple(dict.fromkeys(ids))
        if not ordered:
            return {}
        if not self._client.indices.exists(index=self.index_name):
            return dict.fromkeys(ordered)
        response = self._client.mget(index=self.index_name, ids=list(ordered))
        items = response.get("docs")
        if not isinstance(items, list) or len(items) != len(ordered):
            raise ElasticsearchDocumentConsistencyError("Event mget response is incomplete")
        results = {}
        for key, item in zip(ordered, items, strict=True):
            if not isinstance(item, Mapping) or item.get("_id") != key or item.get("error"):
                raise ElasticsearchDocumentConsistencyError("Event mget identity or status differs")
            results[key] = self._parse_document(key, item.get("_source")) if item.get("found") else None
        return results

    def verify_ready(self, documents: Sequence[EventIndexDocument]) -> bool:
        expected = {document.event_id: self._validate_document(document) for document in documents}
        if len(expected) != len(documents):
            raise ValueError("duplicate expected Event IDs")
        if not expected:
            return True
        if not self._client.indices.exists(index=self.index_name):
            return False
        # Unlike mget, this proves that the exact sources are searchable.
        response = self._client.search(
            index=self.index_name, query={"ids": {"values": list(expected)}},
            size=len(expected), source=True,
        )
        _require_complete(response)
        hits = response.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            raise ElasticsearchDocumentConsistencyError("Event readiness search has malformed hits")
        found = {}
        for hit in hits:
            if not isinstance(hit, Mapping) or hit.get("_id") in found:
                raise ElasticsearchDocumentConsistencyError("Event readiness search has duplicate or malformed IDs")
            found[hit.get("_id")] = self._parse_document(hit.get("_id"), hit.get("_source"))
        return found == expected

    def verify_set(
        self, parse_task_id: str, video_chunk_id: str, version: int,
        expected_ids: Sequence[str],
    ) -> bool:
        source = _source_tuple((parse_task_id, video_chunk_id, version))
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("duplicate expected Event IDs")
        if not self._client.indices.exists(index=self.index_name):
            return False
        query = _source_scope((source,))
        count = self._count(query)
        if count != len(expected_ids):
            return False
        if not expected_ids:
            return True
        count = self._count({"bool": {"filter": [
            query, {"ids": {"values": list(expected_ids)}},
        ]}})
        return count == len(expected_ids)

    def delete_older(
        self, parse_task_id: str, video_chunk_id: str, version: int,
        *, refresh: bool = True,
    ) -> None:
        task, chunk, current = _source_tuple((parse_task_id, video_chunk_id, version))
        self._delete({"bool": {"filter": [
            {"term": {"parse_task_id": task}}, {"term": {"video_chunk_id": chunk}},
            {"range": {"version": {"lt": current}}},
        ]}}, refresh=refresh)

    def bulk_delete(
        self, keys: Sequence[tuple[str, str]], *, refresh: Literal[False, "wait_for"] = False,
    ) -> None:
        if refresh is not False and refresh != "wait_for":
            raise ValueError("refresh must be False or 'wait_for'")
        if keys:
            self._delete(_key_scope(keys), refresh=refresh == "wait_for")

    def verify_absent(self, keys: Sequence[tuple[str, str]]) -> bool:
        if not keys or not self._client.indices.exists(index=self.index_name):
            return True
        return self._count(_key_scope(keys)) == 0

    def _count(self, query: dict) -> int:
        response = self._client.count(index=self.index_name, query=query)
        _require_complete(response)
        count = response.get("count")
        if type(count) is not int or count < 0:
            raise ElasticsearchDocumentConsistencyError("Event count response has an invalid count")
        return count

    def _delete(self, query: dict, *, refresh: bool) -> None:
        if not self._client.indices.exists(index=self.index_name):
            return
        result = self._client.delete_by_query(
            index=self.index_name, query=query, refresh=refresh, conflicts="abort",
        )
        if result.get("failures") or result.get("timed_out") or result.get("version_conflicts"):
            raise ElasticsearchDocumentConsistencyError("Event deletion was incomplete")

    def search_lexical(
        self, query: str, *, sources: Sequence[EventSource], filters: EventFilters,
        limit: int,
    ) -> tuple[RawEventHit, ...]:
        scope = self._query_scope(sources, filters)
        if not query.strip():
            raise ValueError("query must be nonblank")
        response = self._client.search(
            index=self.index_name, query={"bool": {
                "filter": scope,
                "must": [{"nested": {"path": "lexical_fields", "score_mode": "max", "query": {
                    "bool": {"should": [
                        {"match": {"lexical_fields.raw_text": query}},
                        {"match_phrase": {"lexical_fields.raw_text": {"query": query, "boost": 2.0}}},
                    ], "minimum_should_match": 1},
                }, "inner_hits": {"name": "matched_lexical_fields", "size": 32}}}],
            }}, size=_positive_limit(limit), source_includes=list(_HIT_SOURCE_FIELDS),
        )
        return self._parse_hits(response, "lexical", sources, filters)

    def search_vector(
        self, query_vector: Sequence[float], *, channel: Literal["semantic", "vision"],
        sources: Sequence[EventSource], filters: EventFilters, limit: int,
        exact: bool = False,
    ) -> tuple[RawEventHit, ...]:
        if channel not in ("semantic", "vision"):
            raise ValueError("unknown Event vector channel")
        field = "semantic_vectors.vector" if channel == "semantic" else "vision_vector"
        dimensions = self.semantic_dimensions if channel == "semantic" else self.vision_dimensions
        vector = _validated_vector(query_vector, dimensions, "query_vector")
        scope = self._query_scope(sources, filters)
        if channel == "vision":
            scope.append({"exists": {"field": field}})
        limit = _positive_limit(limit)
        if exact:
            score = {"script_score": {
                "query": {"exists": {"field": field}} if channel == "semantic" else {"bool": {"filter": scope}},
                "script": {"source": f"cosineSimilarity(params.vector, '{field}') + 1.0", "params": {"vector": vector}},
            }}
            query = {"bool": {"filter": scope, "must": [{"nested": {
                "path": "semantic_vectors", "score_mode": "max", "query": score,
            }}]}} if channel == "semantic" else score
            response = self._client.search(
                index=self.index_name, query=query, size=limit, source_includes=list(_HIT_SOURCE_FIELDS),
            )
        else:
            if limit > 10_000:
                raise ValueError("approximate Event limit must not exceed 10000")
            response = self._client.search(index=self.index_name, knn={
                "field": field, "query_vector": vector, "k": limit,
                "num_candidates": min(10_000, max(100, limit * 4)),
                "filter": {"bool": {"filter": scope}},
            }, size=limit, source_includes=list(_HIT_SOURCE_FIELDS))
        return self._parse_hits(response, channel, sources, filters)

    def _query_scope(self, sources: Sequence[EventSource], filters: EventFilters) -> list[dict]:
        if self._contract is None:
            raise EventIndexNotReadyError("Event index requires its frozen query contract")
        self.bind_contract(self._contract)
        conditions = [_source_scope(sources)]
        if filters.event_types:
            conditions.append({"terms": {"event_type": list(filters.event_types)}})
        if filters.speaker is not None:
            conditions.append({"term": {"speaker": filters.speaker}})
        if filters.video_chunk_ids:
            conditions.append({"terms": {"video_chunk_id": list(filters.video_chunk_ids)}})
        return conditions

    def _parse_hits(self, response, channel, sources, filters) -> tuple[RawEventHit, ...]:
        _require_complete(response)
        allowed = set(map(_source_tuple, sources))
        hits = response.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise ElasticsearchDocumentConsistencyError("Event search has no hits")
        results, seen = [], set()
        for rank, hit in enumerate(hits, 1):
            if not isinstance(hit, Mapping):
                raise ElasticsearchDocumentConsistencyError("Event search hit is malformed")
            source = hit.get("_source")
            if not isinstance(source, Mapping):
                raise ElasticsearchDocumentConsistencyError("Event search hit has no source")
            lexical = ()
            if channel == "lexical":
                inner = hit.get("inner_hits", {}).get("matched_lexical_fields", {}).get("hits", {}).get("hits", [])
                lexical = tuple(item.get("_source") for item in inner)
                if not lexical:
                    raise ElasticsearchDocumentConsistencyError("Event lexical match is not in its document")
            try:
                candidate = RawEventHit(
                    **{name: source[name] for name in _HIT_SOURCE_FIELDS if name in source},
                    channel=channel, event_id=hit.get("_id"), rank=rank, score=hit.get("_score"),
                    matched_fields=lexical,
                )
            except (TypeError, ValueError) as error:
                raise ElasticsearchDocumentConsistencyError(str(error)) from error
            identity = (candidate.parse_task_id, candidate.video_chunk_id, candidate.version)
            if (candidate.event_id != compute_event_id(*identity, candidate.timeline_index)
                    or candidate.event_id in seen or identity not in allowed
                    or filters.event_types and candidate.event_type not in filters.event_types
                    or filters.speaker is not None and candidate.speaker != filters.speaker
                    or filters.video_chunk_ids and candidate.video_chunk_id not in filters.video_chunk_ids):
                raise ElasticsearchDocumentConsistencyError("Event hit is outside its trusted source or filter")
            seen.add(candidate.event_id)
            results.append(candidate)
        return tuple(results)

    def _validate_document(self, value) -> EventIndexDocument:
        source = value.model_dump(mode="python") if isinstance(value, EventIndexDocument) else value
        if not isinstance(source, Mapping):
            raise ValueError("Event document source must be an object")
        document = EventIndexDocument.model_validate(source)
        for semantic in document.semantic_vectors:
            _validated_vector(semantic.vector, self.semantic_dimensions, "semantic_vectors.vector")
        _validated_vector(document.vision_vector, self.vision_dimensions, "vision_vector")
        if (canonical_uuid(document.parse_task_id, "parse_task_id") != document.parse_task_id
                or canonical_uuid(document.video_chunk_id, "video_chunk_id") != document.video_chunk_id):
            raise ValueError("Event document IDs must use canonical UUIDs")
        return document

    def _parse_document(self, event_id, source) -> EventIndexDocument:
        try:
            document = self._validate_document(source)
            if event_id != document.event_id:
                raise ValueError("Event document ID differs from its source")
            return document
        except (TypeError, ValueError) as error:
            raise ElasticsearchDocumentConsistencyError(str(error)) from error

    def _mapping(self, contract: EventIndexingContract) -> dict[str, Any]:
        return {"dynamic": "strict", "_meta": {
            "index_contract_version": EVENT_INDEX_CONTRACT_VERSION,
            "event_contract_id": contract.contract_id, "event_contract": contract.as_dict(),
            "media_time_unit": "milliseconds", "analysis_contract_version": "cjk-1",
        }, "properties": {
            **{name: {"type": "keyword"} for name in (
                "video_chunk_id", "parse_task_id", "event_type", "speaker",
            )},
            "version": {"type": "integer"}, "timeline_index": {"type": "integer"},
            "lexical_fields": {"type": "nested", "dynamic": "strict", "properties": {
                "field": {"type": "keyword"},
                "raw_text": {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"},
                "language": {"type": "keyword"}, "role": {"type": "keyword"},
            }},
            "semantic_vectors": {"type": "nested", "dynamic": "strict", "properties": {
                "source": {"type": "keyword"}, "language": {"type": "keyword"}, "role": {"type": "keyword"},
                "vector": {
                    "type": "dense_vector", "dims": self.semantic_dimensions, "index": True,
                    "similarity": "cosine", "index_options": {"type": "hnsw"},
                },
            }},
            "vision_vector": {
                "type": "dense_vector", "dims": self.vision_dimensions, "index": True, "similarity": "cosine",
                "index_options": {"type": "hnsw"},
            },
            "detail": {"type": "object", "enabled": False},
        }}


def _source_tuple(value: EventSource) -> EventSource:
    task, chunk, version = value
    if type(version) is not int or version < 1:
        raise ValueError("Event source version must be a positive integer")
    return canonical_uuid(task, "parse_task_id"), canonical_uuid(chunk, "video_chunk_id"), version


def _require_complete(response: Mapping[str, Any]) -> None:
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise ElasticsearchDocumentConsistencyError("Event search/count response is partial")


def _source_scope(sources: Sequence[EventSource]) -> dict:
    tuples = tuple(dict.fromkeys(map(_source_tuple, sources)))
    if not tuples:
        return {"match_none": {}}
    # Keep tuple pairing while avoiding one Boolean clause per Chunk in a Series.
    grouped: dict[tuple[str, int], list[str]] = {}
    for task, chunk, version in tuples:
        grouped.setdefault((task, version), []).append(chunk)
    return {"bool": {"should": [{"bool": {"filter": [
        {"term": {"parse_task_id": task}},
        {"term": {"video_chunk_id": chunks[0]}} if len(chunks) == 1 else {"terms": {"video_chunk_id": chunks}},
        {"term": {"version": version}},
    ]}} for (task, version), chunks in grouped.items()], "minimum_should_match": 1}}


def _key_scope(keys: Sequence[tuple[str, str]]) -> dict:
    return {"bool": {"should": [{"bool": {"filter": [
        {"term": {"parse_task_id": canonical_uuid(task, "parse_task_id")}},
        {"term": {"video_chunk_id": canonical_uuid(chunk, "video_chunk_id")}},
    ]}} for task, chunk in dict.fromkeys(keys)], "minimum_should_match": 1}}
