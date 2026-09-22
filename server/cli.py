from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from gateway.config import load_config
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from qdrant_client import QdrantClient

from .constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from .database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from .repository.index_sync import IndexSyncRepository
from .repository.video import VideoRepository
from .service.index_sync import IndexSyncService
from .service.keyframe_backfill import KeyframeBackfillService
from .service.parse_import import prepare_video_parse_import
from .service.qdrant_mutation_lock import QdrantMutationLock
from .service.video_retrieval import VideoRetrievalService
from .storage.artifact import LocalArtifactStore
from .storage.qdrant import QdrantVideoChunkStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anivag-server",
        description="Manage anivag persistence and formal video retrieval.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_database = subparsers.add_parser(
        "init-db",
        help="Create the initial anivag persistence schema.",
    )
    _add_database_arguments(init_database)

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
    import_parse.add_argument("--workspace-id", default=DEFAULT_LOCAL_WORKSPACE_ID)
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
        default=Path(
            os.getenv("ANIVAG_ARTIFACT_ROOT", "outputs/server/artifacts")
        ),
        help="Managed Artifact Store root; defaults to ANIVAG_ARTIFACT_ROOT.",
    )
    backfill_keyframes.add_argument(
        "--workspace-id",
        default=DEFAULT_LOCAL_WORKSPACE_ID,
        help="Only inspect this workspace; defaults to local-default.",
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
        default=os.getenv("ANIVAG_PARSE_MAX_HEIGHT", "720"),
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

    sync_index = subparsers.add_parser(
        "sync-index",
        help="Synchronize due MySQL Index Sync intents to Qdrant.",
    )
    _add_database_arguments(sync_index)
    _add_qdrant_arguments(sync_index)
    sync_index.add_argument("--limit", type=_positive_int, default=100)

    reconcile_index = subparsers.add_parser(
        "reconcile-index",
        help="Inspect and repair missing or stale Qdrant points.",
    )
    _add_database_arguments(reconcile_index)
    _add_qdrant_arguments(reconcile_index)
    reconcile_index.add_argument(
        "--inspection-batch-size",
        type=_positive_int,
        default=256,
    )

    rebuild_index = subparsers.add_parser(
        "rebuild-index",
        help="Replay every current MySQL index intent after Qdrant data loss.",
    )
    _add_database_arguments(rebuild_index)
    _add_qdrant_arguments(rebuild_index)

    search = subparsers.add_parser(
        "search",
        help="Run scoped Text/Vision retrieval and batch-hydrate its evidence.",
    )
    _add_database_arguments(search)
    _add_qdrant_arguments(search)
    search.add_argument("--query", required=True)
    search.add_argument("--workspace-id", default=DEFAULT_LOCAL_WORKSPACE_ID)
    search.add_argument(
        "--series-id",
        dest="series_ids",
        action="append",
        required=True,
        help="Formal Series UUID; repeat to search more than one Series.",
    )
    search.add_argument("--top-k", type=_positive_int, default=10)
    search.add_argument("--channel-top-k", type=_positive_int, default=20)
    search.add_argument("--rrf-k", type=_positive_int, default=60)
    search.add_argument("--text-weight", type=float, default=1.0)
    search.add_argument("--vision-weight", type=float, default=1.0)
    search.add_argument(
        "--exact",
        action="store_true",
        help="Use exact Qdrant search; intended for deterministic diagnostics.",
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
        default=os.getenv("ANIVAG_DATABASE_URL"),
        help="SQLAlchemy MySQL URL; defaults to ANIVAG_DATABASE_URL.",
    )
    parser.add_argument(
        "--environment",
        choices=tuple(item.value for item in DatabaseEnvironment),
        default=os.getenv(
            "ANIVAG_ENVIRONMENT",
            DatabaseEnvironment.DEVELOPMENT.value,
        ),
    )


def _add_qdrant_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--qdrant-url",
        default=os.getenv("ANIVAG_QDRANT_URL"),
        help="Qdrant HTTP URL; defaults to ANIVAG_QDRANT_URL.",
    )
    parser.add_argument(
        "--qdrant-api-key",
        default=os.getenv("ANIVAG_QDRANT_API_KEY"),
        help="Optional Qdrant API key; defaults to ANIVAG_QDRANT_API_KEY.",
    )
    parser.add_argument(
        "--qdrant-collection",
        default=os.getenv("ANIVAG_QDRANT_COLLECTION"),
        help="Environment-isolated collection; defaults to ANIVAG_QDRANT_COLLECTION.",
    )
    parser.add_argument(
        "--text-vector-dimensions",
        type=_positive_int,
        default=os.getenv(
            "ANIVAG_TEXT_VECTOR_DIMENSIONS",
            os.getenv("ANIVAG_EMBEDDING_DIMENSIONS", "1024"),
        ),
        help="Formal text-vector schema; defaults to 1024.",
    )
    parser.add_argument(
        "--vision-vector-dimensions",
        type=_positive_int,
        default=os.getenv(
            "ANIVAG_VISION_VECTOR_DIMENSIONS",
            os.getenv("ANIVAG_EMBEDDING_DIMENSIONS", "1024"),
        ),
        help="Formal vision-vector schema; defaults to 1024.",
    )


def main() -> None:
    args = _build_parser().parse_args()
    if not args.database_url:
        raise ValueError(
            "database URL is required via --database-url or ANIVAG_DATABASE_URL"
        )
    environment = DatabaseEnvironment(args.environment)
    engine = create_database_engine(args.database_url, environment=environment)

    if args.command == "init-db":
        create_schema(engine)
        print(json.dumps({"schemaCreated": True, "environment": environment.value}))
        return

    if args.command == "import-parse":
        if args.create_schema:
            create_schema(engine)
        important_extra_data = _load_series_bible(args.series_bible_file)
        prepared = prepare_video_parse_import(
            args.parse_result,
            args.canonical_chunk_dir,
            series_name=args.series_name,
            series_description=args.series_description,
            series_id=args.series_id,
            video_id=args.video_id,
            workspace_id=args.workspace_id,
            episode_order=args.episode_order,
            upload_sequence=args.upload_sequence,
            pipeline_version=args.pipeline_version,
            important_extra_data=important_extra_data,
            embedding_cache_directory=args.embedding_cache_dir,
        )
        repository = VideoRepository(create_session_factory(engine))
        report = repository.import_video_parse(prepared)
        print(
            json.dumps(
                report.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            )
        )
        return

    if args.command == "backfill-keyframes":
        report = KeyframeBackfillService(
            VideoRepository(create_session_factory(engine)),
            LocalArtifactStore(
                args.artifact_root,
                environment=environment,
                create_base_directory=args.apply,
            ),
            max_height=args.max_height,
        ).backfill(
            limit=args.limit,
            dry_run=not args.apply,
            workspace_id=args.workspace_id,
            series_id=args.series_id,
            video_id=args.video_id,
        )
        print(json.dumps(
            report.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        ))
        return

    if args.command in {
        "sync-index",
        "reconcile-index",
        "rebuild-index",
        "search",
    }:
        if not args.qdrant_url:
            raise ValueError(
                "Qdrant URL is required via --qdrant-url or ANIVAG_QDRANT_URL"
            )
        if not args.qdrant_collection:
            raise ValueError(
                "Qdrant collection is required via --qdrant-collection or "
                "ANIVAG_QDRANT_COLLECTION"
            )
        client = QdrantClient(
            url=args.qdrant_url,
            api_key=args.qdrant_api_key,
        )
        try:
            store = QdrantVideoChunkStore(
                client,
                collection_name=args.qdrant_collection,
                environment=environment,
                text_dimensions=args.text_vector_dimensions,
                vision_dimensions=args.vision_vector_dimensions,
            )
            session_factory = create_session_factory(engine)
            if args.command == "search":
                embedding_config = load_config(args.config).multimodal_embedding
                embedder = DoubaoMultimodalEmbedder(
                    args.embedding_model
                    or os.getenv("ANIVAG_VIDEO_EMBEDDING_MODEL")
                    or embedding_config.model,
                    output_dimensionality=args.text_vector_dimensions,
                    fps=embedding_config.fps,
                )
                result = VideoRetrievalService(
                    VideoRepository(session_factory),
                    store,
                    embedder,
                    channel_top_k=args.channel_top_k,
                    rrf_k=args.rrf_k,
                    text_weight=args.text_weight,
                    vision_weight=args.vision_weight,
                    exact=args.exact,
                ).search(
                    args.query,
                    workspace_id=args.workspace_id,
                    series_ids=args.series_ids,
                    top_k=args.top_k,
                )
            else:
                service = IndexSyncService(
                    IndexSyncRepository(session_factory),
                    store,
                    mutation_lock=QdrantMutationLock(
                        engine,
                        environment=environment,
                        collection_name=args.qdrant_collection,
                    ),
                )
                if args.command == "sync-index":
                    result = service.sync_due(limit=args.limit)
                elif args.command == "reconcile-index":
                    result = service.reconcile(
                        inspection_batch_size=args.inspection_batch_size
                    )
                else:
                    result = service.rebuild()
        finally:
            client.close()
        print(
            json.dumps(
                result.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            )
        )
        return

    repository = VideoRepository(create_session_factory(engine))
    evidence = repository.hydrate_video_chunks(args.video_chunk_ids)
    print(
        json.dumps(
            [item.model_dump(mode="json", by_alias=True) for item in evidence],
            ensure_ascii=False,
        )
    )


def _load_series_bible(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=True)
    return {
        "seriesBible": resolved.read_text(encoding="utf-8"),
        "sourceUri": resolved.as_uri(),
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


if __name__ == "__main__":
    main()
