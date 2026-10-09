from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from elasticsearch import Elasticsearch
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config.elasticsearch import ElasticsearchConfig
from ..database import DatabaseEnvironment, create_database_engine, create_session_factory
from ..storage.artifact import LocalArtifactStore
from ..storage.elasticsearch import ElasticsearchVideoChunkStore


def build_database_storage(
    database_url: str, *, environment: DatabaseEnvironment,
    pool_recycle_seconds: int,
) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_database_engine(
        database_url, environment=environment,
        pool_recycle_seconds=pool_recycle_seconds,
    )
    try:
        return engine, create_session_factory(engine)
    except BaseException:
        engine.dispose()
        raise


@contextmanager
def open_database_storage(
    database_url: str, *, environment: DatabaseEnvironment,
    pool_recycle_seconds: int,
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine, session_factory = build_database_storage(
        database_url, environment=environment,
        pool_recycle_seconds=pool_recycle_seconds,
    )
    try:
        yield engine, session_factory
    finally:
        engine.dispose()


def build_elasticsearch_storage(
    config: ElasticsearchConfig, *, environment: DatabaseEnvironment,
    semantic_dimensions: int, vision_dimensions: int,
) -> tuple[Elasticsearch, ElasticsearchVideoChunkStore]:
    config.validate_environment(environment)
    client = Elasticsearch(config.url, api_key=config.api_key)
    try:
        store = ElasticsearchVideoChunkStore(
            client, index_name=config.index_name, environment=environment,
            semantic_dimensions=semantic_dimensions,
            vision_dimensions=vision_dimensions,
        )
        return client, store
    except BaseException:
        client.close()
        raise


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
        client.close()


def build_artifact_storage(
    root: Path, *, environment: DatabaseEnvironment,
    create_base_directory: bool = True,
) -> LocalArtifactStore:
    return LocalArtifactStore(
        root, environment=environment, create_base_directory=create_base_directory,
    )
