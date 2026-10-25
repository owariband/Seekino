"""Compare the configured Chat model with local rewrite results, using saved Jev decisions."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from evals.benchmark.intent_routing.local_rewriter import (
    PROMPT_PATH, RewriteFields, build_prompt, load_cases, render_append,
)
from gateway.config import CodexModelSourceConfig, load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput
from gateway.usage import capture_model_usage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev-results", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        cases = load_cases(args.jev_results)
        config = load_config()
        binding = config.resolve_model_binding("supervisor").model_copy(update={
            "reasoning_effort": "low", "fallback": None, "request_timeout_seconds": 60,
        })
        source = config.source(binding.source)
        if not isinstance(source, CodexModelSourceConfig):
            raise ValueError("This comparison requires a Codex supervisor binding")
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if args.only:
        if set(args.only) - {case["id"] for case in cases}:
            parser.error("--only contains unknown case IDs")
        cases = [case for case in cases if case["id"] in args.only]
    for case in cases:
        case["model_input"] = build_prompt(prompt, case["input"])
    metadata = {
        "model": binding.model, "source": binding.source, "provider": source.provider,
        "adapter": source.adapter, "requested_reasoning_effort": binding.reasoning_effort,
        "request_timeout_seconds": binding.request_timeout_seconds, "fallback": None,
        "planned": len(cases), "jev_results": str(args.jev_results),
        "prompt": prompt, "schema": RewriteFields.model_json_schema(),
        "api_max_output_tokens": None, "context_output_reserve_tokens": binding.max_output_tokens,
        "latency_scope": "Gateway end-to-end, including a new Codex session/app-server per case",
        "output_limit_note": "Codex adapter does not send an API generation max-output limit",
    }
    if args.dry_run:
        print(json.dumps({
            key: value for key, value in metadata.items() if key not in ("prompt", "schema")
        }, ensure_ascii=False))
        return

    os.umask(0o077)
    started_at = datetime.now(timezone.utc)
    out = args.out or Path("outputs/verification") / f"chat-rewriter-{started_at:%Y%m%dT%H%M%S%fZ}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with out.open("x", encoding="utf-8") as output, closing(GatewayClient(config)) as client:
        output.write(json.dumps({
            "kind": "run", "started_at": started_at.isoformat(), **metadata,
        }, ensure_ascii=False) + "\n")
        output.flush()
        with closing(client.create_generative_model(binding)) as model:
            for case in cases:
                row = {"kind": "case", **case}
                usage = []
                started = time.perf_counter()
                try:
                    with capture_model_usage(usage.append):
                        fields = model.generate(
                            [TextInput(row["model_input"])], schema_name="query_rewrite",
                            result_type=RewriteFields, tools=(), max_model_calls=1,
                        )
                    row["output"] = {"action": case["input"]["action"], **fields.model_dump()}
                    row["appended_input"] = render_append(
                        case["input"]["query"], case["input"]["action"], fields,
                    )
                    row["contract_valid"] = True
                except Exception as exc:
                    row.update(contract_valid=False, error_type=type(exc).__name__, error=str(exc))
                    row["error_causes"] = []
                    cause = exc.__cause__
                    while cause is not None and len(row["error_causes"]) < 4:
                        row["error_causes"].append({
                            "type": type(cause).__name__, "message": str(cause)[:1000],
                        })
                        cause = cause.__cause__
                row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                row["usage"] = [
                    asdict(replace(call, context=dict(call.context))) for call in usage
                ]
                rows.append(row)
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(json.dumps({
                    key: row[key] for key in ("id", "output", "contract_valid", "latency_ms", "error")
                    if key in row
                }, ensure_ascii=False), flush=True)
                if "error" in row:
                    break
        latencies = [row["latency_ms"] for row in rows if row["contract_valid"]]
        summary = {
            "kind": "summary", "attempted": len(rows), "planned": len(cases),
            "contract_valid": len(latencies), "semantic_review": "pending",
            "latency_ms": {
                "min": min(latencies), "median": statistics.median(latencies), "max": max(latencies),
            } if latencies else None,
        }
        output.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))
    print(f"Results: {out}")
    if len(latencies) != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
