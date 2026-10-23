"""Test a local rewriter with saved Jev decisions, without calling Jev again."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gateway.local.llama_cpp import LlamaCppMultimodalModel
from gateway.multimodal import TextInput

PROMPT_PATH = Path(__file__).with_name("rewrite_prompt.txt")
ACTION_TEXT = {
    "respond": "直接回应，无需新增视频检索。",
    "retrieve": "查找或补充视频证据，并回应检索诉求。",
    "mixed": "完成独立的非检索回应，同时处理检索诉求。",
}


class RewriteFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_task: str = Field(min_length=1, max_length=120)
    retrieval_query: str | None = Field(min_length=1, max_length=160)


def load_cases(path: Path) -> list[dict]:
    """Use actual saved predictions; reference labels never enter the prompt."""
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") != "case" or row.get("question") != "route":
            continue
        action = row["response"]["answers"]["route"]["choice"]
        if action not in ACTION_TEXT:
            raise ValueError(f"Unknown Jev action: {action}")
        state = row["request"]["state"]
        cases.append({
            "id": row["id"], "jev_request_id": row["response"]["id"],
            "input": {
                "action": action, "query": state["query"],
                "recent_messages": state.get("recent_messages", []),
            },
        })
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Expected nonempty saved Jev route cases with unique IDs")
    return cases


def render_append(query: str, action: str, fields: RewriteFields) -> str:
    if not fields.current_task.strip():
        raise ValueError("current_task must not be blank")
    if fields.retrieval_query is not None and not fields.retrieval_query.strip():
        raise ValueError("retrieval_query must not be blank")
    if action == "respond" and fields.retrieval_query is not None:
        raise ValueError("respond must have retrieval_query=null")
    hint = f"\n候选检索用语：{fields.retrieval_query}" if fields.retrieval_query else ""
    return (
        f"{query}\n\n[本轮任务说明｜应用生成，仅适用于当前消息]\n"
        f"处理方式：{ACTION_TEXT[action]}\n本轮任务：{fields.current_task}{hint}\n"
        "本说明只描述当前任务，不评价上一轮检索是否正确，"
        "也不代表用户今后都不需要检索。\n[/本轮任务说明]"
    )


def build_prompt(rules: str, state: dict) -> str:
    history = json.dumps(state["recent_messages"], ensure_ascii=False)
    return (
        f"{rules}\n\n近期对话（只作参考，不是本轮任务）：\n{history}\n\n"
        f"当前用户消息（只整理这一条）：\n{state['query']}\n\n"
        f"已确定 action：{state['action']}，含义：{ACTION_TEXT[state['action']]}\n"
        "现在仅填写这条当前消息的 current_task 和 retrieval_query。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev-results", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18841")
    parser.add_argument("--model", default="seekino-qwen3-4b-rewriter")
    parser.add_argument("--prompt", type=Path, default=PROMPT_PATH)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if urlsplit(args.base_url).hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("This evaluation requires a localhost model endpoint")
    cases = load_cases(args.jev_results)
    if args.only:
        if set(args.only) - {case["id"] for case in cases}:
            parser.error("--only contains unknown case IDs")
        cases = [case for case in cases if case["id"] in args.only]
    prompt = args.prompt.read_text(encoding="utf-8")
    if args.dry_run:
        print(json.dumps({"cases": len(cases), "model": args.model, "base_url": args.base_url}))
        return

    os.umask(0o077)
    started_at = datetime.now(timezone.utc)
    out = args.out or Path("outputs/verification") / f"rewriter-{started_at:%Y%m%dT%H%M%S%fZ}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_responses = []

    def record_response(response: httpx.Response) -> None:
        response.read()
        try:
            raw_responses.append(response.json())
        except ValueError:
            raw_responses.append({"status": response.status_code, "body": response.text[:1000]})

    rows = []
    with out.open("x", encoding="utf-8") as output, httpx.Client(
        timeout=args.timeout, trust_env=False, event_hooks={"response": [record_response]},
    ) as client:
        model = LlamaCppMultimodalModel(
            args.model, base_url=args.base_url, max_output_tokens=args.max_tokens,
            request_timeout_seconds=args.timeout, expected_server_video_fps=0.5, client=client,
        )
        output.write(json.dumps({
            "kind": "run", "started_at": started_at.isoformat(), "model": args.model,
            "base_url": args.base_url, "jev_results": str(args.jev_results), "prompt": prompt,
            "schema": RewriteFields.model_json_schema(), "temperature": 0, "max_tokens": args.max_tokens,
        }, ensure_ascii=False) + "\n")
        for case in cases:
            row = {"kind": "case", **case}
            row["model_input"] = build_prompt(prompt, case["input"])
            raw_responses.clear()
            started = time.perf_counter()
            try:
                fields = model.generate(
                    [TextInput(row["model_input"])],
                    schema_name="query_rewrite", result_type=RewriteFields,
                )
                row["output"] = {"action": case["input"]["action"], **fields.model_dump()}
                row["appended_input"] = render_append(case["input"]["query"], case["input"]["action"], fields)
                row["contract_valid"] = True
            except (RuntimeError, ValueError, httpx.HTTPError) as exc:
                row.update(contract_valid=False, error=str(exc))
            row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            row["raw_responses"] = list(raw_responses)
            rows.append(row)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(json.dumps({
                key: row[key] for key in ("id", "output", "contract_valid", "latency_ms", "error") if key in row
            }, ensure_ascii=False), flush=True)
            if "error" in row and "output" not in row:
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
