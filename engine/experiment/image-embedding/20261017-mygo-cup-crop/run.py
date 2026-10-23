"""Compare one user-provided crop with its corresponding view and fixed controls."""
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


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / math.hypot(*a) / math.hypot(*b)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["samples"]:
        assert hashlib.sha256((base / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    args.output.mkdir(parents=True, exist_ok=False)
    config = load_config()
    client = GatewayClient(config)
    binding = config.resolve_embedding_binding("embedding")
    info = client.describe_embedding(binding)
    started = time.perf_counter()
    result = {
        "status": "RUNNING", "started_at": datetime.now().astimezone().isoformat(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": asdict(info), "resolved_binding": binding.model_dump(mode="json"),
        "metric": "cosine = dot(a,b) / (L2(a) * L2(b)); provider vectors preserved without normalization",
        "input_modality": "image", "embedding_calls": [],
    }
    calls, embeddings = [], {}

    def usage(call):
        calls.append({
            "id": call.call_id, "provider": call.provider, "model": call.model,
            "operation": call.operation, "status": call.status,
            "started_at": call.started_at, "ended_at": call.ended_at,
            "request_id": call.request_id,
            "usage": asdict(call.usage) if call.usage is not None else None,
        })
        write(args.output / "model-calls.json", calls)

    write(args.output / "results.json", result)
    try:
        model = client.create_embedding_model(binding)
        with capture_model_usage(usage):
            for item in manifest["samples"]:
                tick = time.perf_counter()
                response = model.embed_image(base / item["file"])
                assert response.modality == "image" and response.dimension == info.dimensions
                assert response.model in {info.model_name, info.encoder_model}
                assert all(math.isfinite(x) for x in response.vector)
                norm = math.hypot(*response.vector)
                assert norm > 0
                elapsed = (time.perf_counter() - tick) * 1000
                embeddings[item["id"]] = {
                    "input_sha256": item["sha256"], "norm": norm,
                    "embedding_duration_ms": elapsed, **response.model_dump(mode="json"),
                }
                result["embedding_calls"].append({
                    "id": item["id"], "duration_ms": elapsed, "dimension": response.dimension,
                })
                write(args.output / "embeddings.json", embeddings)
                write(args.output / "results.json", result)
                print(json.dumps(result["embedding_calls"][-1]), flush=True)
        query = embeddings[manifest["query_id"]]["vector"]
        ranking = sorted([
            {"id": key, "cosine": cosine(query, record["vector"]),
             "user_confirmed_corresponding_view": key == manifest["corresponding_candidate_id"]}
            for key, record in embeddings.items() if key != manifest["query_id"]
        ], key=lambda row: row["cosine"], reverse=True)
        result["ranking"] = [{"rank": index, **row} for index, row in enumerate(ranking, 1)]
        result["pairwise_cosine"] = {
            a: {b: cosine(va["vector"], vb["vector"]) for b, vb in embeddings.items()}
            for a, va in embeddings.items()
        }
        assert len(calls) == len(embeddings) == len(manifest["samples"])
        assert all(call["status"] == "completed" for call in calls)
        result["status"] = "COMPLETED"
    except Exception as error:
        result.update(status="FAILED", error_type=type(error).__name__)
        raise
    finally:
        client.close()
        result["finished_at"] = datetime.now().astimezone().isoformat()
        result["total_duration_ms"] = (time.perf_counter() - started) * 1000
        write(args.output / "results.json", result)
    print(json.dumps({key: result[key] for key in ["status", "model", "total_duration_ms", "ranking"]}), flush=True)


if __name__ == "__main__":
    main()
