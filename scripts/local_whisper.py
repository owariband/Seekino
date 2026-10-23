"""Serve the configured local Whisper model through Audio Transcriptions."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import OpenAIAudioSourceConfig, load_config


def main() -> None:
    config = load_config()
    binding = config.resolve_model_binding("transcription")
    source = config.source(binding.source)
    if not isinstance(source, OpenAIAudioSourceConfig) or source.provider != "whisper.cpp":
        raise ValueError("transcription must select a whisper.cpp audio source")
    endpoint = urlsplit(source.base_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local Whisper requires a loopback HTTP endpoint")
    if Path(binding.model).name != binding.model:
        raise ValueError("Whisper model must be a model name, not a path")
    binary = ROOT / "outputs/tools/whisper.cpp/build/bin/whisper-server"
    model = ROOT / "outputs/local-models/whisper" / f"ggml-{binding.model}.bin"
    for path in (binary, model):
        if not path.is_file():
            raise FileNotFoundError(f"Local Whisper deployment is missing {path}")
    os.execv(binary, [
        str(binary), "--model", str(model),
        "--host", endpoint.hostname, "--port", str(endpoint.port or 80),
        "--inference-path", endpoint.path.rstrip("/") + "/audio/transcriptions",
        "--language", "auto", "--threads", "6",
    ])


if __name__ == "__main__":
    main()
