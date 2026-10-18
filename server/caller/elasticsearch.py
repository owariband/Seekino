from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from elasticsearch import Elasticsearch

from ..config.elasticsearch import ElasticsearchConfig
from ..database import DatabaseEnvironment
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore


def create_elasticsearch_client(
    config: ElasticsearchConfig, *, request_timeout_seconds: float | None = None,
) -> Elasticsearch:
    options = {"api_key": config.api_key}
    if request_timeout_seconds is not None:
        options["request_timeout"] = request_timeout_seconds
    return Elasticsearch(config.url, **options)


def close_elasticsearch_client(client: Elasticsearch) -> None:
    client.close()


@contextmanager
def open_elasticsearch_client(
    config: ElasticsearchConfig, *, request_timeout_seconds: float | None = None,
) -> Iterator[Elasticsearch]:
    client = create_elasticsearch_client(config, request_timeout_seconds=request_timeout_seconds)
    try:
        yield client
    finally:
        close_elasticsearch_client(client)


def build_elasticsearch_storage(
    config: ElasticsearchConfig, *, environment: DatabaseEnvironment,
    semantic_dimensions: int, vision_dimensions: int,
) -> tuple[Elasticsearch, ElasticsearchVideoChunkStore]:
    config.validate_environment(environment)
    client = create_elasticsearch_client(config)
    try:
        store = ElasticsearchVideoChunkStore(
            client, index_name=config.index_name, environment=environment,
            semantic_dimensions=semantic_dimensions,
            vision_dimensions=vision_dimensions,
        )
        return client, store
    except BaseException:
        close_elasticsearch_client(client)
        raise


def build_event_elasticsearch_storage(
    client: Elasticsearch, config: ElasticsearchConfig, *, environment: DatabaseEnvironment,
    semantic_dimensions: int, vision_dimensions: int,
) -> ElasticsearchVideoEventStore:
    """Borrow the existing client; index initialization is an explicit operation."""
    config.validate_environment(environment)
    return ElasticsearchVideoEventStore(
        client, index_name=config.event_index_for_environment(environment), environment=environment,
        semantic_dimensions=semantic_dimensions, vision_dimensions=vision_dimensions,
    )


@contextmanager
def open_elasticsearch_storage(
    config: ElasticsearchConfig, *, environment: DatabaseEnvironment,
    semantic_dimensions: int, vision_dimensions: int,
) -> Iterator[tuple[Elasticsearch, ElasticsearchVideoChunkStore]]:
    client, store = build_elasticsearch_storage(
        config, environment=environment,
        semantic_dimensions=semantic_dimensions, vision_dimensions=vision_dimensions,
    )
    try:
        yield client, store
    finally:
        close_elasticsearch_client(client)
