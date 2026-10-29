"""Run a prepared llama.cpp GGUF embedding model in the foreground, without Ollama."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=root / "outputs/tools/llama.cpp" / ("llama-server.exe" if os.name == "nt" else "llama-server"))
    parser.add_argument("--model", type=Path, default=root / "outputs/local-models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf")
    parser.add_argument("--alias", default="seekino-qwen3-embedding-0.6b")
    parser.add_argument("--port", type=int, default=18843)
    parser.add_argument("--context-size", type=int, default=4096)
    parser.add_argument("--gpu-layers", type=int, default=99)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.context_size < 1 or args.gpu_layers < 0:
        parser.error("port, context-size or gpu-layers is out of range")
    for path in (args.binary, args.model):
        if not path.is_file():
            parser.error(f"missing local file: {path}; prepare the runtime/model first")
    command = [
        str(args.binary.resolve()), "-m", str(args.model.resolve()),
        "--alias", args.alias, "--embedding", "--pooling", "last",
        "--ctx-size", str(args.context_size), "--batch-size", str(args.context_size),
        "--ubatch-size", str(args.context_size), "--parallel", "1",
        "--host", "127.0.0.1", "--port", str(args.port),
        "--gpu-layers", str(args.gpu_layers),
    ]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
