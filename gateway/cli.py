from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import load_config
from .multimodal_embedder import DoubaoMultimodalEmbedder


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.cli",
        description="Smoke-test anivag model gateway capabilities.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Model config YAML; defaults to ANIVAG_MODEL_CONFIG or the "
            "repository-root config.yaml."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    embed_video = subparsers.add_parser(
        "embed-video",
        help="Embed a video into Doubao's cross-modal vector space.",
    )
    embed_video.add_argument("--video", type=Path, required=True)
    _add_embedding_arguments(embed_video, include_fps=True)

    embed_query = subparsers.add_parser(
        "embed-query",
        help="Embed a natural-language query into the same vector space.",
    )
    embed_query.add_argument("--query", required=True)
    _add_embedding_arguments(embed_query)

    return parser


def _add_embedding_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_fps: bool = False,
) -> None:
    parser.add_argument("--model")
    parser.add_argument("--dimensions", type=int)
    if include_fps:
        parser.add_argument("--fps", type=float)
    parser.add_argument("--output", type=Path)


def _write_json(payload: BaseModel | dict[str, Any], output: Path | None) -> None:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    rendered = json.dumps(data, ensure_ascii=False, indent=2)

    if output is None:
        print(rendered)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(f"wrote {output.resolve()}")


def main() -> None:
    args = _build_parser().parse_args()
    config = load_config(args.config)

    embedding_config = config.multimodal_embedding
    model = (
        args.model
        or os.getenv("ANIVAG_VIDEO_EMBEDDING_MODEL")
        or embedding_config.model
    )
    dimensions = args.dimensions
    if dimensions is None:
        dimensions = int(
            os.getenv(
                "ANIVAG_EMBEDDING_DIMENSIONS",
                str(embedding_config.dimensions),
            )
        )
    fps = getattr(args, "fps", None)
    if fps is None:
        fps = float(
            os.getenv(
                "ANIVAG_VIDEO_EMBEDDING_FPS",
                str(embedding_config.fps),
            )
        )

    embedding = DoubaoMultimodalEmbedder(
        model,
        output_dimensionality=dimensions,
        fps=fps,
    )
    if args.command == "embed-video":
        result = embedding.embed_video(args.video)
    else:
        result = embedding.embed_query(args.query)
    _write_json(result, args.output)


if __name__ == "__main__":
    main()
