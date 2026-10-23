"""Embed the Anon crop once and compare with the previous fixed image vectors."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.usage import capture_model_usage


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True)) / math.hypot(*a) / math.hypot(*b)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    cache = {}
    for name, item in manifest["cache"].items():
        path = base / item["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        cache[name] = json.loads(path.read_text())
    query = manifest["query"]
    assert hashlib.sha256((base / query["file"]).read_bytes()).hexdigest() == query["sha256"]
    args.output.mkdir(parents=True, exist_ok=False)
    client = GatewayClient(load_config())
    info = client.describe_embedding("embedding")
    assert asdict(info) == cache["results"]["model"], "Cached encoder identity differs"
    calls = []

    def usage(call):
        calls.append({"id": call.call_id, "provider": call.provider, "model": call.model,
                      "operation": call.operation, "status": call.status,
                      "started_at": call.started_at, "ended_at": call.ended_at,
                      "request_id": call.request_id,
                      "usage": asdict(call.usage) if call.usage is not None else None})
        write(args.output / "model-calls.json", calls)

    result = {"status": "RUNNING", "started_at": datetime.now().astimezone().isoformat(),
              "model": asdict(info), "reused_vector_count": len(cache["embeddings"]),
              "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    tick = time.perf_counter()
    write(args.output / "results.json", result)
    try:
        model = client.create_embedding_model("embedding")
        with capture_model_usage(usage):
            response = model.embed_image(base / query["file"])
        assert response.modality == "image" and response.dimension == info.dimensions
        assert response.model in {info.model_name, info.encoder_model}
        assert all(math.isfinite(x) for x in response.vector) and math.hypot(*response.vector) > 0
        record = {"input_sha256": query["sha256"], "norm": math.hypot(*response.vector),
                  "embedding_duration_ms": (time.perf_counter() - tick) * 1000,
                  **response.model_dump(mode="json")}
        write(args.output / "query-embedding.json", record)
        scores = {key: cosine(response.vector, value["vector"])
                  for key, value in cache["embeddings"].items()}
        ranking = sorted(manifest["candidate_ids"], key=lambda key: scores[key], reverse=True)
        result.update(status="COMPLETED", embedding_duration_ms=record["embedding_duration_ms"],
                      all_pair_cosines=scores,
                      ranking=[{"rank": i, "id": key, "cosine": scores[key]}
                               for i, key in enumerate(ranking, 1)],
                      cup_to_full_previous=cache["results"]["pairwise_cosine"]["cup_crop"]["reference_full"])
        assert len(calls) == 1 and calls[0]["status"] == "completed"
    except Exception as error:
        result.update(status="FAILED", error_type=type(error).__name__)
        raise
    finally:
        client.close()
        result.update(finished_at=datetime.now().astimezone().isoformat(),
                      total_duration_ms=(time.perf_counter() - tick) * 1000)
        write(args.output / "results.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
