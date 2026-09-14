from __future__ import annotations

import argparse
import os
from pathlib import Path

from gateway.config import load_config
from gateway.multimodal_embedder import DoubaoMultimodalEmbedder
from qdrant_client import QdrantClient

from engine.parse.analysis_windows import probe_video_duration_ms
from .qdrant_index import QdrantVideoSegmentIndex
from .recall_eval import (
    RecallEvaluator,
    load_parse_result,
    load_recall_fixture,
    materialize_eval_only_canonical_media,
    write_recall_result,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.recall.cli",
        description=(
            "Evaluate raw text/video embedding recall and RRF for one fully "
            "parsed episode. Canonical media generated here is evaluation-only."
        ),
    )
    parser.add_argument("--parse-result", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--video",
        type=Path,
        help=(
            "Optional local source override; defaults to source_video stored in "
            "the parse result."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Model config YAML; defaults to repository-root config.yaml.",
    )
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-dimensions", type=int)
    parser.add_argument("--embedding-fps", type=float)
    parser.add_argument("--max-height", type=int, default=720)
    parser.add_argument("--channel-top-k", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--text-weight", type=float, default=1.0)
    parser.add_argument("--vision-weight", type=float, default=1.0)
    parser.add_argument(
        "--retrieval-backend",
        choices=("local", "qdrant"),
        default="local",
        help="Ranking backend; defaults to the in-process exact cosine baseline.",
    )
    parser.add_argument(
        "--qdrant-url",
        help=(
            "Qdrant server URL; defaults to ANIVAG_QDRANT_URL when the Qdrant "
            "backend is selected."
        ),
    )
    parser.add_argument(
        "--qdrant-collection",
        help="Qdrant Collection; defaults to ANIVAG_QDRANT_COLLECTION.",
    )
    parser.add_argument(
        "--dataset-id",
        help="Dataset isolation key required for Qdrant evaluation.",
    )
    parser.add_argument(
        "--qdrant-search-mode",
        choices=("exact", "approximate"),
        default="exact",
        help=(
            "Exact requests a full-precision exact scan; approximate allows "
            "the server to use its configured ANN path."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Embedding requests in flight (1-4; defaults to the safest value 1).",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    qdrant_options = _resolve_qdrant_options(args)
    parse_result_path = args.parse_result.expanduser().resolve()
    parse_result = load_parse_result(parse_result_path)
    fixture = load_recall_fixture(args.fixture)
    source_video = _resolve_source_video(
        parse_result.source_video,
        parse_result_path=parse_result_path,
        override=args.video,
    )
    probed_duration_ms = probe_video_duration_ms(source_video)
    if abs(probed_duration_ms - parse_result.duration_ms) > 100:
        raise RuntimeError(
            "source video duration does not match parse result: "
            f"source={probed_duration_ms} ms, parsed={parse_result.duration_ms} ms"
        )

    config = load_config(args.config)
    embedding_config = config.multimodal_embedding
    model = (
        args.embedding_model
        or os.getenv("ANIVAG_VIDEO_EMBEDDING_MODEL")
        or embedding_config.model
    )
    dimensions = args.embedding_dimensions
    if dimensions is None:
        dimensions = int(
            os.getenv("ANIVAG_EMBEDDING_DIMENSIONS") or embedding_config.dimensions
        )
    fps = args.embedding_fps
    if fps is None:
        fps = float(
            os.getenv("ANIVAG_VIDEO_EMBEDDING_FPS") or embedding_config.fps
        )

    embedder = DoubaoMultimodalEmbedder(
        model,
        output_dimensionality=dimensions,
        fps=fps,
    )
    qdrant_index = None
    if qdrant_options is not None:
        qdrant_url, qdrant_collection, dataset_id = qdrant_options
        qdrant_index = QdrantVideoSegmentIndex(
            QdrantClient(
                url=qdrant_url,
                api_key=os.getenv("ANIVAG_QDRANT_API_KEY"),
                timeout=30,
            ),
            collection_name=qdrant_collection,
            dataset_id=dataset_id,
        )
        qdrant_index.ensure_collection(
            text_dimensions=dimensions,
            vision_dimensions=dimensions,
        )
        qdrant_index.assert_dataset_size(
            len(parse_result.segments),
            allow_empty=True,
        )

    output_dir = args.output_dir.expanduser().resolve()
    media_by_sequence = materialize_eval_only_canonical_media(
        parse_result,
        source_video=source_video,
        output_dir=output_dir,
        max_height=args.max_height,
    )
    evaluator = RecallEvaluator(
        embedder,
        cache_dir=output_dir / "embedding-cache",
        max_height=args.max_height,
        channel_top_k=args.channel_top_k,
        rrf_k=args.rrf_k,
        text_weight=args.text_weight,
        vision_weight=args.vision_weight,
        qdrant_index=qdrant_index,
        qdrant_exact=args.qdrant_search_mode == "exact",
        concurrency=args.concurrency,
    )
    result = evaluator.evaluate(
        parse_result,
        fixture,
        source_video=source_video,
        media_by_sequence=media_by_sequence,
        parse_result_path=parse_result_path,
    )
    result_path = output_dir / "recall-eval.json"
    write_recall_result(result, result_path)
    print(
        f"recall evaluation complete; queries={result.query_count}; "
        f"segments={result.segment_count}; "
        f"backend={result.retrieval_backend}; "
        f"fused_hit@1={result.fused_metrics.hit_at[1]:.3f}; "
        f"fused_mrr={result.fused_metrics.mrr:.3f}; output={result_path}",
        flush=True,
    )


def _resolve_qdrant_options(
    args: argparse.Namespace,
) -> tuple[str, str, str] | None:
    if args.retrieval_backend == "local":
        if args.qdrant_url or args.qdrant_collection or args.dataset_id:
            raise ValueError(
                "Qdrant connection arguments require --retrieval-backend qdrant"
            )
        if args.qdrant_search_mode != "exact":
            raise ValueError(
                "Qdrant search mode requires --retrieval-backend qdrant"
            )
        return None

    qdrant_url = args.qdrant_url or os.getenv("ANIVAG_QDRANT_URL")
    qdrant_collection = args.qdrant_collection or os.getenv(
        "ANIVAG_QDRANT_COLLECTION"
    )
    if not qdrant_url or not qdrant_collection or not args.dataset_id:
        raise ValueError(
            "Qdrant evaluation requires URL, Collection, and dataset ID"
        )
    return qdrant_url, qdrant_collection, args.dataset_id


def _resolve_source_video(
    stored_source: Path,
    *,
    parse_result_path: Path,
    override: Path | None,
) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    expanded = stored_source.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (parse_result_path.parent / expanded).resolve()


if __name__ == "__main__":
    main()
