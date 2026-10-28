from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from elasticsearch import Elasticsearch
from redis import Redis
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from . import elasticsearch, mysql, redis

if TYPE_CHECKING:
    from ..config import ServerSettings


@dataclass(slots=True)
class CallerResources:
    engine: Engine | None = None
    session_factory: sessionmaker[Session] | None = None
    elasticsearch_client: Elasticsearch | None = None
    elasticsearch_store: ElasticsearchVideoChunkStore | None = None
    elasticsearch_event_store: ElasticsearchVideoEventStore | None = None
    redis_client: Redis | None = None
    _cleanup: ExitStack | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cleanup is not None:
            self._cleanup.close()
            return
        # Explicitly injected resources follow the same shutdown order.
        with ExitStack() as cleanup:
            if self.engine is not None:
                cleanup.callback(mysql.close_database_engine, self.engine)
            cleanup.callback(redis.close_redis_client, self.redis_client)
            if self.elasticsearch_client is not None:
                cleanup.callback(elasticsearch.close_elasticsearch_client, self.elasticsearch_client)


def init(
    settings: ServerSettings, *, semantic_dimensions: int, vision_dimensions: int,
) -> CallerResources:
    if settings.elasticsearch is None:
        raise ValueError(
            "Video Parse V2 requires SEEKINO_ELASTICSEARCH_URL and "
            "SEEKINO_ELASTICSEARCH_INDEX"
        )
    with ExitStack() as construction_cleanup:
        engine, session_factory = mysql.build_database_storage(
            settings.database_url, environment=settings.environment,
            pool_recycle_seconds=settings.database_pool_recycle_seconds,
        )
        construction_cleanup.callback(mysql.close_database_engine, engine)
        redis_client = redis.create_redis_client(settings.auth)
        construction_cleanup.callback(redis.close_redis_client, redis_client)
        elasticsearch_client, elasticsearch_store = elasticsearch.build_elasticsearch_storage(
            settings.elasticsearch, environment=settings.environment,
            semantic_dimensions=semantic_dimensions, vision_dimensions=vision_dimensions,
        )
        construction_cleanup.callback(elasticsearch.close_elasticsearch_client, elasticsearch_client)
        event_store = elasticsearch.build_event_elasticsearch_storage(
            elasticsearch_client, settings.elasticsearch, environment=settings.environment,
            semantic_dimensions=semantic_dimensions, vision_dimensions=vision_dimensions,
        )
        resources = CallerResources(
            engine=engine, session_factory=session_factory,
            elasticsearch_client=elasticsearch_client,
            elasticsearch_store=elasticsearch_store, redis_client=redis_client,
            elasticsearch_event_store=event_store,
        )
        resources._cleanup = construction_cleanup.pop_all()
        return resources
