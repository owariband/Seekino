from types import SimpleNamespace

import pytest

from scripts import migrate_tenant_index as migration
from server.database import DatabaseEnvironment


class Client:
    def __init__(self):
        self.indices = self
        self.created = []
        self.copies = []
        self.bad_copy = False
        self.mapping = {
            "dynamic": "strict",
            "_meta": {
                "index_contract_version": "anivag-video-chunk-es-5",
                "projection_version": "parse-v2-chunk-index-v1",
                "analysis_contract_version": "cjk-1",
                "media_time_unit": "milliseconds",
            },
            "properties": {
                **{name: {"type": "keyword"} for name in (
                    "workspace_id", "series_id", "video_id", "video_chunk_id", "parse_task_id",
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
                    **{name: {"type": "keyword"} for name in ("language", "role", "speaker")},
                    **{name: {"type": "long"} for name in ("start_seconds", "end_seconds")},
                    **{name: {"type": "integer"} for name in ("timeline_index", "enrichment_index")},
                }},
                **{name: {
                    "type": "dense_vector", "dims": 2, "index": True, "similarity": "cosine",
                    "index_options": {"type": "hnsw"},
                } for name in ("semantic_vector", "vision_vector")},
            },
        }

    def get_mapping(self, *, index):
        return {index: {"mappings": self.mapping}}

    def exists(self, *, index):
        return index in self.created

    def count(self, *, index, query=None):
        return {"count": 0 if query else 3}

    def options(self, **_kwargs):
        return self

    def reindex(self, **kwargs):
        self.copies.append(kwargs)
        return {"failures": [{"reason": "invalid document"}] if self.bad_copy else [], "version_conflicts": 0}


def run(client, **kwargs):
    return migration.migrate(client, source="legacy_test", destination="tenant_test",
                             environment=DatabaseEnvironment.TEST,
                             text_dimensions=2, vision_dimensions=2, **kwargs)


def test_dry_run_does_not_create_or_copy():
    client = Client()
    assert run(client)["documentCount"] == 3
    assert client.created == client.copies == []


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("source_setting", [
    {"enabled": False},
    {"mode": "synthetic"},
    {"includes": ["workspace_id", "semantic_vector"]},
    {"excludes": ["vision_vector"]},
    False,
])
def test_incomplete_source_blocks_before_write(source_setting, apply):
    client = Client()
    client.mapping["_source"] = source_setting
    with pytest.raises(ValueError, match="complete stored _source"):
        run(client, apply=apply)
    assert client.created == client.copies == []


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("field", ["semantic_vector", "vision_vector"])
@pytest.mark.parametrize("vector", [None, {"type": "keyword"}, {"type": "dense_vector", "dims": 3}])
def test_incompatible_vector_blocks_before_write(field, vector, apply):
    client = Client()
    if vector is None:
        client.mapping["properties"].pop(field)
    else:
        client.mapping["properties"][field] = vector
    with pytest.raises(ValueError, match=f"source {field}"):
        run(client, apply=apply)
    assert client.created == client.copies == []


def test_copy_preserves_external_versions_and_old_index(monkeypatch):
    client = Client()
    def store(_client, *, index_name, **_kwargs):
        return SimpleNamespace(ensure_index=lambda: client.created.append(index_name))
    monkeypatch.setattr(migration, "ElasticsearchVideoChunkStore", store)
    assert run(client, apply=True)["validated"] is True
    assert set(client.created) == {"tenant_test"}
    assert client.copies[0]["dest"] == {"index": "tenant_test", "version_type": "external"}
    assert "ctx._source.remove('workspace_id')" in client.copies[0]["script"]["source"]
    assert "ctx._version" not in client.copies[0]["script"]["source"]


def test_existing_destination_blocks_before_write():
    client = Client()
    client.created.append("tenant_test")
    with pytest.raises(ValueError, match="already exists"):
        run(client, apply=True)
    assert client.copies == []


def test_failed_copy_is_not_reported_as_success(monkeypatch):
    client = Client()
    client.bad_copy = True
    monkeypatch.setattr(migration, "ElasticsearchVideoChunkStore",
                        lambda *_args, **_kwargs: SimpleNamespace(ensure_index=lambda: None))
    with pytest.raises(RuntimeError, match="keep the source configured"):
        run(client, apply=True)


def test_wrong_environment_is_rejected():
    client = Client()
    with pytest.raises(ValueError, match="test component"):
        migration.migrate(client, source="production", destination="tenant_test",
                          environment=DatabaseEnvironment.TEST,
                          text_dimensions=2, vision_dimensions=2, apply=True)
    assert client.created == client.copies == []
