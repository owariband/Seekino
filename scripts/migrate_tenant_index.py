"""Copy the legacy search index into a new tenant-scoped index; dry run by default.

Run with ``uv run python -m scripts.migrate_tenant_index --help``.
Stop API/Workers for the final copy and switch the configured index only after
successful validation. The source index and database remain unchanged.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os

from server.caller.elasticsearch import open_elasticsearch_client
from server.config.elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
from server.database import DatabaseEnvironment
from server.storage.elasticsearch import ElasticsearchVideoChunkStore


_RENAME_SCOPE = """
if (!ctx._source.containsKey('workspace_id')) {
    throw new IllegalArgumentException('missing legacy workspace_id');
}
if (ctx._source.containsKey('tenant_id') && ctx._source.tenant_id != ctx._source.workspace_id) {
    throw new IllegalArgumentException('conflicting scope identifiers');
}
ctx._source.tenant_id = ctx._source.remove('workspace_id');
"""


def migrate(client, *, source: str, destination: str, environment: DatabaseEnvironment,
            text_dimensions: int, vision_dimensions: int, apply: bool = False) -> dict:
    if source == destination:
        raise ValueError("source and destination must differ")
    for name in (source, destination):
        ElasticsearchConfig("http://127.0.0.1:9200", name).validate_environment(environment)
    # Require physical indices so an alias cannot accidentally name the source.
    mapping = client.indices.get_mapping(index=source)
    if set(mapping) != {source}:
        raise ValueError("source must name exactly one physical index")
    legacy = mapping[source]["mappings"]
    if (legacy.get("_meta", {}).get("index_contract_version") != "seekino-video-chunk-es-5"
            or legacy.get("properties", {}).get("workspace_id", {}).get("type") != "keyword"):
        raise ValueError("source must use the legacy v5 workspace contract")
    stored_source = legacy.get("_source", {})
    if (
        not isinstance(stored_source, Mapping)
        or stored_source.get("enabled") is False
        or stored_source.get("mode") not in (None, "stored")
        or stored_source.get("includes")
        or stored_source.get("excludes")
    ):
        raise ValueError("source must retain the complete stored _source")
    for field, dimensions in (("semantic_vector", text_dimensions), ("vision_vector", vision_dimensions)):
        vector = legacy.get("properties", {}).get(field)
        if (not isinstance(vector, Mapping) or vector.get("type") != "dense_vector"
                or vector.get("dims") != dimensions):
            raise ValueError(f"source {field} must be a dense_vector with {dimensions} dimensions")
    if client.indices.exists(index=destination):
        raise ValueError("destination already exists; choose a new physical index")
    source_count = client.count(index=source)["count"]
    plan = {"source": source, "destination": destination, "documentCount": source_count,
            "apply": apply, "preserveDocumentIds": True, "preserveExternalVersions": True}
    if not apply:
        return plan
    store = ElasticsearchVideoChunkStore(
        client, index_name=destination, environment=environment,
        semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
    )
    store.ensure_index()
    result = client.options(request_timeout=600).reindex(
        source={"index": source}, dest={"index": destination, "version_type": "external"},
        script={"lang": "painless", "source": _RENAME_SCOPE},
        wait_for_completion=True, refresh=True,
    )
    if result.get("failures") or result.get("timed_out") or result.get("version_conflicts"):
        raise RuntimeError("copy did not complete; keep the source configured and inspect the new index")
    destination_count = client.count(index=destination)["count"]
    missing_scope = client.count(index=destination, query={"bool": {
        "must_not": [{"exists": {"field": "tenant_id"}}],
    }})["count"]
    if destination_count != source_count or missing_scope:
        raise RuntimeError("copied document count or tenant scope validation failed")
    store.ensure_index()
    return {**plan, "copiedDocumentCount": destination_count, "validated": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--environment", choices=[item.value for item in DatabaseEnvironment], required=True)
    parser.add_argument("--elasticsearch-url", default=os.getenv("SEEKINO_ELASTICSEARCH_URL"))
    parser.add_argument("--text-vector-dimensions", type=int)
    parser.add_argument("--vision-vector-dimensions", type=int)
    parser.add_argument("--apply", action="store_true", help="create and copy into the new index")
    arguments = parser.parse_args()
    config = ElasticsearchConfig(arguments.elasticsearch_url or "", arguments.destination,
                                 api_key=os.getenv("SEEKINO_ELASTICSEARCH_API_KEY") or None)
    dimensions = resolve_vector_dimensions(text_dimensions=arguments.text_vector_dimensions,
                                          vision_dimensions=arguments.vision_vector_dimensions)
    with open_elasticsearch_client(config) as client:
        result = migrate(client, source=arguments.source, destination=arguments.destination,
                         environment=DatabaseEnvironment(arguments.environment),
                         text_dimensions=dimensions[0], vision_dimensions=dimensions[1], apply=arguments.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
