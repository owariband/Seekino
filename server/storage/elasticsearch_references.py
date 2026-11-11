"""Lexical editing-technique projection, partitioned from content and Event documents."""

from __future__ import annotations

import hashlib
import json

from elasticsearch import BadRequestError, Elasticsearch

from engine.parsev2.edit_reference import ReferenceChunk
from ..config.elasticsearch import validate_index_name


class ElasticsearchReferenceStore:
    def __init__(self, client: Elasticsearch, *, index_name, environment):
        validate_index_name(index_name, environment)
        self.client = client
        self.index_name = index_name

    def search(self, query, *, tenant_id, task_ids, size):
        if not task_ids or not self.client.indices.exists(index=self.index_name):
            return []
        result = self.client.search(index=self.index_name, size=size, source_excludes=["text"],
                                    query={"bool": {"must": [{"match": {"text": query}}],
                                           "filter": [{"term": {"tenant_id": tenant_id}}, {"terms": {"task_id": list(task_ids)}}]}},
                                    sort=["_score", {"segment_id": "asc"}, {"task_id": "asc"}])
        return [hit["_source"] for hit in result["hits"]["hits"]]

    def publish(self, snapshot):
        if not self.client.indices.exists(index=self.index_name):
            properties = {key: {"type": "keyword"} for key in
                          ("tenant_id", "series_id", "video_id", "task_id", "chunk_id", "segment_id", "fingerprint")}
            properties.update({"version": {"type": "integer"}, "generation": {"type": "integer"},
                               "text": {"type": "text", "analyzer": "cjk"}})
            try:
                self.client.indices.create(index=self.index_name, mappings={"dynamic": "strict", "properties": properties})
            except BadRequestError as error:
                if error.error != "resource_already_exists_exception":
                    raise
        chunk = snapshot.chunk
        documents = {}
        for obs in ReferenceChunk.model_validate(chunk.content).observations:
            identity = {"tenant_id": chunk.tenant_id, "series_id": chunk.series_id, "video_id": chunk.video_id,
                        "task_id": snapshot.video_parse_task_id, "chunk_id": chunk.video_chunk_id,
                        "version": chunk.content_version, "generation": snapshot.target_index_generation,
                        "segment_id": obs.segment.segment_id}
            doc_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            note = obs.note
            words = [obs.technique_key, note.name, note.summary, note.role, *note.signals,
                     *note.look.model_dump().values(), note.motion.easing, note.motion.camera,
                     *(f"{s.framing} {s.subject} {s.enter}" for s in note.motion.steps)]
            documents[doc_id] = {**identity, "fingerprint": observation_fingerprint(obs),
                                 "text": " ".join(w for w in words if w)}
        if documents:
            operations = []
            for doc_id, document in documents.items():
                operations.extend(({"index": {"_id": doc_id}}, document))
            response = self.client.bulk(index=self.index_name, operations=operations, refresh="wait_for")
            if response["errors"]:
                raise RuntimeError("reference index did not publish every segment")
            visible = self.client.search(index=self.index_name, query={"ids": {"values": list(documents)}},
                                         size=len(documents), source=True)
            if {hit["_id"]: hit["_source"] for hit in visible["hits"]["hits"]} != documents:
                raise RuntimeError("reference projection differs from the current source")
        return {"index_name": self.index_name, "segments": sorted(d["segment_id"] for d in documents.values())}


def observation_fingerprint(observation):
    return hashlib.sha256(json.dumps(observation.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()
