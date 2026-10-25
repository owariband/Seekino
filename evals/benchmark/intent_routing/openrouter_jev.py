"""Evaluate Jev Decisions over OpenRouter; never starts application services.

Usage:
    uv run --no-sync --env-file .env.local python -m evals.benchmark.intent_routing.openrouter_jev
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.14"
DEFAULT_CASES = Path(__file__).with_name("cases.json")


def build_requests(fixture: dict, model: str, only: list[str] | None) -> list[tuple[dict, dict]]:
    """Keep reference labels out of the model's state and questions."""
    cases = fixture["cases"]
    if only:
        unknown = set(only) - {case["id"] for case in cases}
        if unknown:
            raise ValueError(f"Unknown case IDs: {sorted(unknown)}")
        cases = [case for case in cases if case["id"] in only]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Cases must be nonempty with unique IDs")
    requests = []
    for case in cases:
        question = fixture["questions"][case["question"]]
        if question["type"] == "choice":
            if case["expected"] not in question["criteria"]:
                raise ValueError(f"Invalid expected choice: {case['id']}")
        elif question["type"] != "noul" or type(case["expected"]) is not bool:
            raise ValueError(f"Unsupported question or expected label: {case['id']}")
        requests.append((case, {
            "model": model,
            "state": case["state"],
            "questions": {case["question"]: question},
        }))
    return requests


def submit_decision(client: httpx.Client, payload: dict, *, api_key: str) -> dict:
    response = client.post(
        DECISIONS_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"},
    )
    # Do not persist credentials even if an upstream error echoes a request header.
    text = response.text.replace(api_key, "[REDACTED]")
    if not response.is_success:
        raise ValueError(f"OpenRouter HTTP {response.status_code}: {text[:800]}")
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("OpenRouter response must be a JSON object")
    return result


def read_prediction(result: dict, question_id: str, question: dict) -> str | bool:
    answer = result["answers"][question_id]
    if answer["type"] != question["type"]:
        raise ValueError("Unexpected answer type")
    if question["type"] == "choice":
        prediction = answer["choice"]
        if prediction not in question["criteria"]:
            raise ValueError("Unknown returned choice")
        return prediction
    probability = answer["noul"]
    if type(probability) not in (int, float) or not 0 <= probability <= 1:
        raise ValueError("Invalid noul probability")
    # Threshold is only for scoring the supplied smoke example.
    return probability >= 0.5


def summarize(rows: list[dict]) -> dict:
    completed = [row for row in rows if "correct" in row]
    routing = [row for row in completed if row["question"] == "route"]
    costs = []
    for row in rows:
        usage = row.get("response", {}).get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            costs.append(cost)
    summary = {
        "attempted": len(rows),
        "completed": len(completed),
        "correct": sum(row["correct"] for row in completed),
        "router_correct": sum(row["correct"] for row in routing),
        "router_total": len(routing),
        "reported_cost_usd": sum(costs) if costs else None,
        "responses_with_reported_cost": len(costs),
        "confusion": {
            label: {
                predicted: sum(row["expected"] == label and row["prediction"] == predicted for row in routing)
                for predicted in ("respond", "retrieve", "mixed")
            }
            for label in ("respond", "retrieve", "mixed")
        },
    }
    if completed:
        latencies = [row["latency_ms"] for row in completed]
        summary["latency_ms"] = {
            "min": min(latencies), "median": statistics.median(latencies), "max": max(latencies),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--api-key-env", default="OPENROUTER_JEV_API_KEY")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate fixtures without network or credentials")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    requests = build_requests(fixture, args.model, args.only)
    if args.dry_run:
        print(json.dumps({"endpoint": DECISIONS_URL, "model": args.model, "cases": len(requests)}))
        return
    api_key = os.getenv(args.api_key_env, "").strip()
    if not api_key:
        parser.error(f"Missing {args.api_key_env}; configure it in .env.local and use uv --env-file")

    os.umask(0o077)
    started_at = datetime.now(timezone.utc)
    output_path = args.out or Path("outputs/verification") / f"jev-{started_at:%Y%m%dT%H%M%S%fZ}.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    # One connection, sequential requests, no automatic retries or fallback providers.
    with output_path.open("x", encoding="utf-8") as output, httpx.Client(timeout=args.timeout) as client:
        output.write(json.dumps({
            "kind": "run", "started_at": started_at.isoformat(), "endpoint": DECISIONS_URL,
            "model": args.model, "fixture": str(args.cases), "planned_cases": len(requests),
        }) + "\n")
        for case, payload in requests:
            row = {
                "kind": "case", "id": case["id"], "question": case["question"],
                "expected": case["expected"], "request": payload,
            }
            started = time.perf_counter()
            try:
                result = submit_decision(client, payload, api_key=api_key)
                row["response"] = result
                row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                row["prediction"] = read_prediction(result, case["question"], payload["questions"][case["question"]])
                row["correct"] = row["prediction"] == case["expected"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                row["error"] = str(exc).replace(api_key, "[REDACTED]")
            rows.append(row)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(json.dumps({key: value for key, value in row.items() if key not in ("request", "response")}, ensure_ascii=False), flush=True)
            if "error" in row:
                break
        summary = {"kind": "summary", **summarize(rows)}
        output.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"Results: {output_path}")
    if len(rows) != len(requests) or any(not row.get("correct", False) for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
