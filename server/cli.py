"""One-shot commands assemble only their required database, index and file dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gateway.config import load_config

from .caller.elasticsearch import open_elasticsearch_storage
from .caller.mysql import open_database_storage
from .common.retrieval import build_retrieval_service
from .common.storage import build_artifact_storage
from .constants.tenant import DEFAULT_LOCAL_TENANT_ID
from .config.elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
from .config.parse_profile import resolve_embedding_config
from .config.retrieval import RetrievalConfig, resolve_retrieval_model_limits
from .config.settings import (
    resolve_agent_request_timeout_seconds,
    resolve_artifact_root,
    resolve_database_pool_recycle_seconds,
    resolve_database_url,
    resolve_environment,
    resolve_execution_mode,
    resolve_parse_max_height,
)
from .database import DatabaseEnvironment, create_schema
from .repository.video import VideoRepository
from .repository.video_parse import VideoParseRepository
from .service.keyframe_backfill import KeyframeBackfillService


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

    import_parse = subparsers.add_parser(
        "import-parse",
        help="Import one completed VideoParseResult as formal business records.",
    )
    _add_database_arguments(import_parse)
    import_parse.add_argument("--parse-result", type=Path, required=True)
    import_parse.add_argument(
        "--canonical-chunk-dir",
        type=Path,
        required=True,
        help="Directory containing one non-overlapping canonical MP4 per VideoChunk.",
    )
    import_parse.add_argument(
        "--embedding-cache-dir",
        type=Path,
        help=(
            "Optional directory containing chunk-NNNN.text/video.json files; "
            "legacy segment-NNNN files are also accepted."
        ),
    )
    import_parse.add_argument("--series-name", required=True)
    import_parse.add_argument("--series-description")
    import_parse.add_argument("--series-id")
    import_parse.add_argument("--video-id")
    import_parse.add_argument("--tenant-id", "--workspace-id", dest="tenant_id", default=DEFAULT_LOCAL_TENANT_ID)
    import_parse.add_argument("--episode-order", type=int, required=True)
    import_parse.add_argument("--upload-sequence", type=int)
    import_parse.add_argument("--pipeline-version", required=True)
    import_parse.add_argument("--series-bible-file", type=Path)
    import_parse.add_argument(
        "--create-schema",
        action="store_true",
        help="Create missing tables before this transitional import.",
    )

    hydrate = subparsers.add_parser(
        "hydrate",
        help="Batch hydrate formal videoChunkId values from MySQL.",
    )
    _add_database_arguments(hydrate)
    hydrate.add_argument("video_chunk_ids", nargs="+")

    backfill_keyframes = subparsers.add_parser(
        "backfill-keyframes",
        help=(
            "Generate timed first-frame images for historical VideoChunks "
            "without rerunning model parsing."
        ),
    )
    _add_database_arguments(backfill_keyframes)
    backfill_keyframes.add_argument(
        "--artifact-root",
        type=Path,
        help="Managed Artifact Store root; defaults to ANIVAG_ARTIFACT_ROOT.",
    )
    backfill_keyframes.add_argument(
        "--tenant-id", "--workspace-id", dest="tenant_id",
        default=DEFAULT_LOCAL_TENANT_ID,
        help="Only inspect this Tenant; defaults to local-default.",
    )
    backfill_keyframes.add_argument(
        "--series-id",
        help="Only inspect this Series UUID.",
    )
    backfill_keyframes.add_argument(
        "--video-id",
        help="Only inspect this Video UUID.",
    )
    backfill_keyframes.add_argument(
        "--limit",
        type=_positive_int,
        default=100,
        help=(
            "Maximum successful writes for --apply, or missing candidates "
            "inspected for --dry-run."
        ),
    )
    backfill_keyframes.add_argument(
        "--max-height",
        type=_positive_int,
        help="Maximum generated JPEG height; defaults to ANIVAG_PARSE_MAX_HEIGHT.",
    )
    backfill_mode = backfill_keyframes.add_mutually_exclusive_group(
        required=True
    )
    backfill_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect candidates and managed media without writing images or MySQL.",
    )
    backfill_mode.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly publish first-frame images and conditionally update MySQL.",
    )

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

    if args.command in {"init-es-index", "search"}:
        elasticsearch = ElasticsearchConfig.from_environment(
            environment,
            url=args.elasticsearch_url,
            index_name=args.elasticsearch_index,
            api_key=args.elasticsearch_api_key,
            index_generation=args.elasticsearch_index_generation,
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

    if args.command == "init-es-index":
        with open_elasticsearch_storage(
            elasticsearch, environment=environment,
            semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
        ) as (_client, store):
            store.ensure_index()
        result = {
            "schemaReady": True,
            "index": elasticsearch.index_name,
            "indexGeneration": elasticsearch.index_generation,
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
            interval_ids = VideoParseRepository(sessions).request_index_rebuild(
                args.video_parse_task_id
            )
            result = {
                "videoParseTaskId": args.video_parse_task_id,
                "scheduledChunkTaskIds": list(interval_ids),
            }
        elif args.command == "import-parse":
            from .service.parsev1.parse_import import prepare_video_parse_import

            if args.create_schema:
                create_schema(engine)
            prepared = prepare_video_parse_import(
                args.parse_result,
                args.canonical_chunk_dir,
                series_name=args.series_name,
                series_description=args.series_description,
                series_id=args.series_id,
                video_id=args.video_id,
                tenant_id=args.tenant_id,
                episode_order=args.episode_order,
                upload_sequence=args.upload_sequence,
                pipeline_version=args.pipeline_version,
                series_bible_file=args.series_bible_file,
                embedding_cache_directory=args.embedding_cache_dir,
            )
            result = VideoRepository(sessions).import_video_parse(
                prepared
            ).model_dump(mode="json", by_alias=True)
        elif args.command == "backfill-keyframes":
            report = KeyframeBackfillService(
                VideoRepository(sessions),
                build_artifact_storage(
                    resolve_artifact_root(args.artifact_root),
                    environment=environment,
                    create_base_directory=args.apply,
                ),
                max_height=resolve_parse_max_height(args.max_height),
            ).backfill(
                limit=args.limit, dry_run=not args.apply,
                tenant_id=args.tenant_id, series_id=args.series_id,
                video_id=args.video_id,
            )
            result = report.model_dump(mode="json", by_alias=True)
        elif args.command == "search":
            with open_elasticsearch_storage(
                elasticsearch, environment=environment,
                semantic_dimensions=text_dimensions, vision_dimensions=vision_dimensions,
            ) as (_client, store):
                service, _encoder = build_retrieval_service(
                    VideoParseRepository(sessions), store,
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
                for item in VideoRepository(sessions).hydrate_video_chunks(
                    args.video_chunk_ids
                )
            ]
    print(json.dumps(result, ensure_ascii=False))


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
