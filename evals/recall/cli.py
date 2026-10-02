from __future__ import annotations

import argparse
import os
from pathlib import Path

from elasticsearch import Elasticsearch
from gateway.config import load_config
from gateway.ark.embedding import DoubaoMultimodalEmbedder

from engine.parsev1.analysis_windows import probe_video_duration_seconds
from .elasticsearch_index import ElasticsearchVideoChunkIndex
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
        choices=("local", "elasticsearch"),
        default="local",
        help="Ranking backend; defaults to the in-process exact cosine baseline.",
    )
    parser.add_argument(
        "--elasticsearch-url",
        help=(
            "Evaluation Elasticsearch URL; defaults to "
            "ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL."
        ),
    )
    parser.add_argument(
        "--elasticsearch-index",
        help=(
            "Dedicated anivag_recall_eval_* index; defaults to "
            "ANIVAG_RECALL_EVAL_INDEX."
        ),
    )
    parser.add_argument(
        "--dataset-id",
        help="Dataset isolation key required for Elasticsearch evaluation.",
    )
    parser.add_argument(
        "--elasticsearch-search-mode",
        choices=("exact", "approximate"),
        default="exact",
        help=(
            "Exact scans the filtered dataset with script_score; approximate "
            "uses filtered kNN."
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
    elasticsearch_options = _resolve_elasticsearch_options(args)
    parse_result_path = args.parse_result.expanduser().resolve()
    parse_result = load_parse_result(parse_result_path)
    fixture = load_recall_fixture(args.fixture)
    source_video = _resolve_source_video(
        parse_result.source_video,
        parse_result_path=parse_result_path,
        override=args.video,
    )
    probed_duration_seconds = probe_video_duration_seconds(source_video)
    if abs(probed_duration_seconds - parse_result.duration_seconds) > 1:
        raise RuntimeError(
            "source video duration does not match parse result: "
            f"source={probed_duration_seconds} seconds, parsed={parse_result.duration_seconds} seconds"
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
    client = None
    elasticsearch_index = None
    if elasticsearch_options is not None:
        elasticsearch_url, index_name, dataset_id = elasticsearch_options
        client = Elasticsearch(
            elasticsearch_url,
            api_key=os.getenv("ANIVAG_RECALL_EVAL_ELASTICSEARCH_API_KEY") or None,
            request_timeout=30,
        )
        elasticsearch_index = ElasticsearchVideoChunkIndex(
            client, index_name=index_name, dataset_id=dataset_id,
        )

    try:
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
            elasticsearch_index=elasticsearch_index,
            elasticsearch_search_mode=args.elasticsearch_search_mode,
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
    finally:
        if client is not None:
            client.close()
    print(
        f"recall evaluation complete; queries={result.query_count}; "
        f"chunks={result.chunk_count}; "
        f"backend={result.retrieval_backend}; "
        f"fused_hit@1={result.fused_metrics.hit_at[1]:.3f}; "
        f"fused_mrr={result.fused_metrics.mrr:.3f}; output={result_path}",
        flush=True,
    )


def _resolve_elasticsearch_options(
    args: argparse.Namespace,
) -> tuple[str, str, str] | None:
    if args.retrieval_backend == "local":
        if args.elasticsearch_url or args.elasticsearch_index or args.dataset_id:
            raise ValueError(
                "Elasticsearch connection arguments require --retrieval-backend elasticsearch"
            )
        if args.elasticsearch_search_mode != "exact":
            raise ValueError(
                "Elasticsearch search mode requires --retrieval-backend elasticsearch"
            )
        return None

    elasticsearch_url = args.elasticsearch_url or os.getenv(
        "ANIVAG_RECALL_EVAL_ELASTICSEARCH_URL"
    )
    index_name = args.elasticsearch_index or os.getenv("ANIVAG_RECALL_EVAL_INDEX")
    if not elasticsearch_url or not index_name or not args.dataset_id:
        raise ValueError(
            "Elasticsearch evaluation requires URL, dedicated index, and dataset ID"
        )
    return elasticsearch_url, index_name, args.dataset_id


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
