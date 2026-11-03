"""Serve the configured whisper.cpp model; local tests can require Apple Metal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import OpenAIAudioSourceConfig, load_config


def run_with_metal_required(command: list[str]) -> None:
    """Reject a CPU fallback before accepting a ready local model service."""
    process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True)
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    metal = False
    try:
        assert process.stderr is not None
        for line in process.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            metal = metal or bool(re.search(r"using (?:Metal|MTL\d+) backend", line))
            if ("no GPU found" in line or "failed to initialize" in line
                    or ("whisper_backend_init_gpu: using" in line and not metal)
                    or ("whisper server listening" in line and not metal)):
                raise RuntimeError("whisper.cpp did not initialize Metal; refusing CPU fallback")
        returncode = process.wait()
        if returncode:
            raise SystemExit(returncode)
        if not metal:
            raise RuntimeError("whisper.cpp exited without confirming a Metal backend")
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stderr is not None:
            process.stderr.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="explicit model configuration, including outside this worktree")
    parser.add_argument("--binary", type=Path, default=ROOT / "outputs/tools/whisper.cpp/build/bin/whisper-server")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "outputs/local-models/whisper")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--threads", type=int, default=6, help="CPU threads; not GPU cores or WhisperX batch size")
    parser.add_argument("--require-metal", action="store_true", help="stop if the actual backend is not Metal")
    parser.add_argument("--dry-run", action="store_true", help="print the checked command without loading weights")
    args = parser.parse_args()
    if not 1 <= args.threads <= 64:
        parser.error("threads must be between 1 and 64")
    config = load_config(args.config)
    binding = config.resolve_model_binding("transcription")
    source = config.source(binding.source)
    if not isinstance(source, OpenAIAudioSourceConfig) or source.provider != "whisper.cpp":
        raise ValueError("transcription must select a whisper.cpp audio source")
    endpoint = urlsplit(source.base_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local Whisper requires a loopback HTTP endpoint")
    if Path(binding.model).name != binding.model:
        raise ValueError("Whisper model must be a model name, not a path")
    model = args.model_dir / f"ggml-{binding.model}.bin"
    for path in (args.binary, model):
        if not path.is_file():
            raise FileNotFoundError(f"Local Whisper deployment is missing {path}")
    command = [
        str(args.binary.resolve()), "--model", str(model.resolve()),
        "--host", endpoint.hostname, "--port", str(endpoint.port or 80),
        "--inference-path", endpoint.path.rstrip("/") + "/audio/transcriptions",
        "--language", args.language, "--threads", str(args.threads), "--processors", "1",
        "--flash-attn", "--no-fallback", "--max-context", "0", "--beam-size", "5", "--best-of", "5",
    ]
    print(json.dumps({"command": command, "require_metal": args.require_metal,
                      "timing_source": "whisper.cpp", "forced_alignment": False}), flush=True)
    if args.dry_run:
        return
    if args.require_metal:
        run_with_metal_required(command)
    else:
        os.execv(command[0], command)


if __name__ == "__main__":
    main()
