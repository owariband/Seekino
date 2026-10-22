"""One-shot commands assemble only their required database, index and file dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gateway.config import load_config

from .caller.elasticsearch import open_elasticsearch_storage, build_event_elasticsearch_storage
from .caller.mysql import open_database_storage
from .common.retrieval import build_retrieval_service
from .constants.tenant import DEFAULT_LOCAL_TENANT_ID
from .config.elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
from .config.parse_profile import resolve_embedding_config, resolve_event_indexing_contract
from .config.retrieval import RetrievalConfig, resolve_retrieval_model_limits
from .config.settings import resolve_agent_request_timeout_seconds, resolve_database_pool_recycle_seconds, resolve_database_url, resolve_environment, resolve_execution_mode, resolve_parse_max_height
from .database import DatabaseEnvironment, create_schema
from .service.video_parse_persistence import VideoParsePersistenceService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anivag-server",
        description="Manage anivag persistence and formal video retrieval.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_es_index = subparsers.add_parser(
        "init-es-index",
        help="Create or validate the explicitly configured Elasticsearch index.",
    )
    _add_environment_argument(init_es_index)
    _add_elasticsearch_arguments(init_es_index)

    init_events = subparsers.add_parser("init-event-index", help="Create or validate the fixed Seekino Event index contract.")
    _add_environment_argument(init_events)
    _add_elasticsearch_arguments(init_events)
    init_events.add_argument("--config", type=Path)
    init_events.add_argument("--embedding-model")
    init_events.add_argument("--max-height", type=_positive_int)

    event_status = subparsers.add_parser("event-index-status", help="Read persisted Event coverage; no model or ES writes.")
    _add_database_arguments(event_status)
    event_status.add_argument("--video-parse-task-id", required=True)

    init_database = subparsers.add_parser(
        "init-db",
        help="Create the initial anivag persistence schema.",
    )
    _add_database_arguments(init_database)

    request_es_rebuild = subparsers.add_parser(
        "request-es-index-rebuild",
        help=(
            "Schedule all refined Chunks in one current Parse V2 task "
            "for Elasticsearch reconstruction."
        ),
    )
    _add_database_arguments(request_es_rebuild)
    request_es_rebuild.add_argument("--video-parse-task-id", required=True)
    request_es_rebuild.add_argument("--with-events", action="store_true", help="Explicitly accept the configured Event target for INDEX backfill.")
    request_es_rebuild.add_argument("--config", type=Path)
    _add_elasticsearch_arguments(request_es_rebuild)

    hydrate = subparsers.add_parser(
        "hydrate",
        help="Batch hydrate formal videoChunkId values from MySQL.",
    )
    _add_database_arguments(hydrate)
    hydrate.add_argument("video_chunk_ids", nargs="+")
    hydrate.add_argument("--tenant-id", required=True)

    rebuild_index = subparsers.add_parser(
        "rebuild-index",
        help="Request an Elasticsearch index rebuild for a Video Parse V2 task.",
    )
    _add_database_arguments(rebuild_index)
    _add_execution_mode_argument(rebuild_index)
    rebuild_index.add_argument(
        "--video-parse-task-id",
        help="Required in V2 mode; the current VideoParseTask to rebuild.",
    )

    search = subparsers.add_parser(
        "search",
        help="Run scoped Video Parse V2 retrieval through Elasticsearch.",
    )
    _add_database_arguments(search)
    _add_execution_mode_argument(search)
    _add_elasticsearch_arguments(search)
    search.add_argument("--query", required=True)
    search.add_argument("--tenant-id", "--workspace-id", dest="tenant_id", default=DEFAULT_LOCAL_TENANT_ID)
    search.add_argument(
        "--series-id",
        dest="series_ids",
        action="append",
        required=True,
        help="Formal Series UUID; repeat to search more than one Series.",
    )
    search.add_argument("--top-k", type=_positive_int, default=10)
    search.add_argument("--channel-top-k", type=_positive_int)
    search.add_argument("--rrf-k", type=_positive_int)
    search.add_argument("--text-weight", type=float)
    search.add_argument("--vision-weight", type=float)
    search.add_argument(
        "--exact",
        action="store_true",
        help="Use exact vector search; intended for deterministic diagnostics.",
    )
    search.add_argument(
        "--config",
        type=Path,
        help="Gateway model config; defaults to ANIVAG_MODEL_CONFIG/config.yaml.",
    )
    search.add_argument(
        "--embedding-model",
        help="Query embedding model; defaults to environment/model config.",
    )
    return parser


def _add_database_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy MySQL URL; defaults to ANIVAG_DATABASE_URL.",
    )
    _add_environment_argument(parser)


def _add_environment_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--environment",
        choices=tuple(item.value for item in DatabaseEnvironment),
    )


def _add_execution_mode_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execution-mode",
        choices=("v2",),
        type=_execution_mode,
        help="Formal execution supports only v2; defaults to ANIVAG_PARSE_EXECUTION_MODE.",
    )


def _add_elasticsearch_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_vector_dimensions: bool = True,
) -> None:
    parser.add_argument(
        "--elasticsearch-url",
    )
    parser.add_argument(
        "--elasticsearch-index",
    )
    parser.add_argument("--event-index", help="Independent Seekino Event index name.")
    parser.add_argument(
        "--elasticsearch-api-key",
    )
    parser.add_argument(
        "--elasticsearch-index-generation",
        type=_positive_int,
    )
    if include_vector_dimensions:
        parser.add_argument(
            "--text-vector-dimensions", type=_positive_int,
        )
        parser.add_argument(
            "--vision-vector-dimensions", type=_positive_int,
        )


def main() -> None:
    args = _build_parser().parse_args()
    environment = resolve_environment(args.environment)
    if args.command in {"search", "rebuild-index"}:
        resolve_execution_mode(args.execution_mode)
    if args.command in {"request-es-index-rebuild", "rebuild-index"}:
        if not args.video_parse_task_id:
            raise ValueError("--video-parse-task-id is required in V2 mode")

    if args.command in {"init-es-index", "init-event-index", "search"}:
        elasticsearch = ElasticsearchConfig.from_environment(
            environment,
            url=args.elasticsearch_url,
            index_name=args.elasticsearch_index,
            api_key=args.elasticsearch_api_key,
            index_generation=args.elasticsearch_index_generation,
            event_index_name=args.event_index,
        )
        if elasticsearch is None:
            raise ValueError(
                "Elasticsearch URL and index are required via command options or "
                "ANIVAG_ELASTICSEARCH_URL and ANIVAG_ELASTICSEARCH_INDEX"
            )
        text_dimensions, vision_dimensions = resolve_vector_dimensions(
            text_dimensions=args.text_vector_dimensions,
            vision_dimensions=args.vision_vector_dimensions,
            require_matching=args.command == "search",
        )

    if args.command in {"init-es-index", "init-event-index"}:
        with open_elasticsearch_storage(
            elasticsearch, environment=environment,
            semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
        ) as (_client, store):
            event_contract = None
            if args.command == "init-event-index":
                if text_dimensions != vision_dimensions:
                    raise ValueError("the configured shared Event encoder requires matching dimensions")
                gateway = load_config(args.config)
                embedding = resolve_embedding_config(gateway, model=args.embedding_model, dimensions=text_dimensions)
                event_contract = _event_contract(gateway, embedding, max_height=resolve_parse_max_height(args.max_height))
                store = build_event_elasticsearch_storage(
                    _client, elasticsearch, environment=environment,
                    semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
                )
                store.ensure_index(event_contract)
            else:
                store.ensure_index()
        result = {
            "schemaReady": True,
            "index": store.index_name if event_contract is not None else elasticsearch.index_name,
            **({"contractId": event_contract.contract_id} if event_contract is not None
               else {"indexGeneration": elasticsearch.index_generation}),
        }
        print(json.dumps(result))
        return

    if args.command == "search":
        gateway = load_config(args.config)
        embedding = resolve_embedding_config(
            gateway, model=args.embedding_model, dimensions=text_dimensions,
        )
        retrieval = RetrievalConfig.from_environment(
            channel_top_k=args.channel_top_k, rrf_k=args.rrf_k,
            text_weight=args.text_weight, vision_weight=args.vision_weight,
        )
        request_timeout, max_output_tokens = resolve_retrieval_model_limits(
            args.config,
            request_timeout_seconds=resolve_agent_request_timeout_seconds(),
        )

    with open_database_storage(
        resolve_database_url(args.database_url),
        environment=environment,
        pool_recycle_seconds=resolve_database_pool_recycle_seconds(),
    ) as (engine, sessions):
        if args.command == "init-db":
            create_schema(engine)
            result = {"schemaCreated": True, "environment": environment.value}
        elif args.command in {"request-es-index-rebuild", "rebuild-index"}:
            repository = VideoParsePersistenceService(sessions)
            event_options = {}
            if getattr(args, "with_events", False):
                config = ElasticsearchConfig.from_environment(
                    environment, url=args.elasticsearch_url, index_name=args.elasticsearch_index,
                    api_key=args.elasticsearch_api_key, index_generation=args.elasticsearch_index_generation,
                    event_index_name=args.event_index,
                )
                if config is None:
                    raise ValueError("Event backfill requires an explicit Elasticsearch configuration")
                from agent.video_parse_v2.input import ParseConfig as TaskParseConfig
                task_input = repository.load_task_input(args.video_parse_task_id)
                frozen = TaskParseConfig.model_validate(task_input.task.parse_config)
                contract = _event_contract(load_config(args.config), frozen.models.embedding,
                                           max_height=resolve_parse_max_height())
                event_options["event_target"] = {"index_name": config.event_index_for_environment(environment),
                    "contract_id": contract.contract_id, "contract": contract.as_dict()}
            interval_ids = repository.request_index_rebuild(args.video_parse_task_id, **event_options)
            result = {
                "videoParseTaskId": args.video_parse_task_id,
                "scheduledChunkTaskIds": list(interval_ids),
            }
        elif args.command == "event-index-status":
            result = VideoParsePersistenceService(sessions).event_index_status(args.video_parse_task_id)
        elif args.command == "search":
            with open_elasticsearch_storage(
                elasticsearch, environment=environment,
                semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
            ) as (_client, store):
                service, _encoder = build_retrieval_service(
                    VideoParsePersistenceService(sessions), store,
                    gateway_config=gateway, embedding_config=embedding,
                    retrieval_config=retrieval,
                    index_generation=elasticsearch.index_generation,
                    request_timeout_seconds=request_timeout,
                    max_output_tokens=max_output_tokens,
                    exact=args.exact,
                )
                result = service.search(
                    args.query, tenant_id=args.tenant_id,
                    series_ids=args.series_ids, top_k=args.top_k,
                ).model_dump(mode="json", by_alias=True)
        elif args.command == "hydrate":
            result = [
                item.model_dump(mode="json", by_alias=True)
                for item in (
                    VideoParsePersistenceService(sessions).get_chunk_evidence(
                        chunk_id, tenant_id=args.tenant_id,
                    ) for chunk_id in args.video_chunk_ids
                )
            ]
    print(json.dumps(result, ensure_ascii=False))


def _event_contract(gateway, embedding, *, max_height):
    from gateway.config import EmbeddingModelBinding
    from gateway.embedding import describe_embedding_model
    from engine.rag.encoding import EncoderContract
    binding = EmbeddingModelBinding.model_validate(embedding.model_dump(exclude_none=True))
    metadata = describe_embedding_model(binding, gateway.source(binding.source))
    return resolve_event_indexing_contract(EncoderContract(
        provider=metadata.provider_name, model=metadata.encoder_model,
        encoder_revision=metadata.encoder_revision, dimensions=metadata.dimensions,
    ), max_height=max_height)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _execution_mode(value: str) -> str:
    try:
        return resolve_execution_mode(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
