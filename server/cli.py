from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from .database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from .repository.index_sync import IndexSyncRepository
from .repository.video import VideoRepository
from .service.index_sync import IndexSyncService
from .service.parse_import import prepare_video_parse_import
from .storage.qdrant import QdrantVideoSegmentStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anivag-server",
        description="Manage the AniVAG MySQL fact source.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_database = subparsers.add_parser(
        "init-db",
        help="Create the initial AniVAG persistence schema.",
    )
    _add_database_arguments(init_database)

    import_parse = subparsers.add_parser(
        "import-parse",
        help="Import one completed VideoParseResult as formal business records.",
    )
    _add_database_arguments(import_parse)
    import_parse.add_argument("--parse-result", type=Path, required=True)
    import_parse.add_argument(
        "--canonical-segment-dir",
        type=Path,
        required=True,
        help="Directory containing one non-overlapping canonical MP4 per Segment.",
    )
    import_parse.add_argument(
        "--embedding-cache-dir",
        type=Path,
        help="Optional directory containing segment-NNNN.text/video.json files.",
    )
    import_parse.add_argument("--series-name", required=True)
    import_parse.add_argument("--series-description")
    import_parse.add_argument("--series-id")
    import_parse.add_argument("--video-id")
    import_parse.add_argument("--workspace-id", default="local-default")
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
        help="Batch hydrate formal videoSegmentId values from MySQL.",
    )
    _add_database_arguments(hydrate)
    hydrate.add_argument("video_segment_ids", nargs="+")

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
            args.canonical_segment_dir,
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

    if args.command in {"sync-index", "reconcile-index", "rebuild-index"}:
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
            store = QdrantVideoSegmentStore(
                client,
                collection_name=args.qdrant_collection,
                environment=environment,
                text_dimensions=args.text_vector_dimensions,
                vision_dimensions=args.vision_vector_dimensions,
            )
            service = IndexSyncService(
                IndexSyncRepository(create_session_factory(engine)),
                store,
            )
            if args.command == "sync-index":
                report = service.sync_due(limit=args.limit)
            elif args.command == "reconcile-index":
                report = service.reconcile(
                    inspection_batch_size=args.inspection_batch_size
                )
            else:
                report = service.rebuild()
        finally:
            client.close()
        print(
            json.dumps(
                report.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            )
        )
        return

    repository = VideoRepository(create_session_factory(engine))
    evidence = repository.hydrate_video_segments(args.video_segment_ids)
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
