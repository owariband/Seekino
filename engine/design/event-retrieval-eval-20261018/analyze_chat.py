"""Extract experiment evidence without truncating the original persisted trace."""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path


def save(directory, name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    terminal = json.loads((directory / "terminal-event.json").read_text())
    assistant = terminal["assistantMessage"]
    entries = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    first_visible = next((entry["elapsed_seconds"] for entry in entries
                          if (entry["data"].get("assistantMessage", {}).get("content") or "").strip()), None)
    first_tool = next((entry["elapsed_seconds"] for entry in entries
                      if any(block.get("type") == "tool" for block in
                             entry["data"].get("assistantMessage", {}).get("renderBlocks", []))), None)
    decoder = json.JSONDecoder()
    tools, reviews = [], []
    for line in (directory / "server.scoped.log").read_text().splitlines():
        if "event=agent_tool_" not in line and "event=agent_turn_evaluated" not in line:
            continue
        item = {"raw_line": line, "timestamp": line.split()[0]}
        for key in ("event", "role", "tool", "tool_run_id", "status", "duration_ms", "turn_number",
                    "retrieved_hit_count", "selected_hit_count", "review_action", "evaluator_action"):
            match = re.search(r"(?:^|\s)" + key + r"=([^\s]+)", line)
            if match:
                item[key] = match.group(1)
        for key in ("arguments_summary", "result_summary", "evaluator_advice", "review_advice"):
            marker = key + "="
            if marker in line:
                item[key] = decoder.raw_decode(line.split(marker, 1)[1])[0]
        (tools if "event=agent_tool_" in line else reviews).append(item)
    trace = (assistant.get("searchResult") or {}).get("retrievalTrace", [])
    finished = [item for item in tools if item.get("event") == "agent_tool_finished"]
    retrieval_summary = []
    for call in trace:
        event = call.get("eventTrace") or {}
        retrieval_summary.append({
            **{key: value for key, value in call.items() if key != "eventTrace"},
            "raw_candidate_count": len(event.get("raw_candidates", [])),
            "fused_candidate_count": len(event.get("fused_candidates", [])),
            "matched_event_count": len(event.get("matched_events", [])),
            "matched_events": event.get("matched_events", []),
            "rejected": event.get("rejected", []),
        })
    analysis = {
        "conversation_id": assistant["conversationId"],
        "assistant_message_id": assistant["messageId"],
        "status": assistant["status"],
        "first_visible_seconds": first_visible,
        "first_tool_seconds": first_tool,
        "terminal_received_seconds": entries[-1]["elapsed_seconds"],
        "timing": assistant.get("timing"),
        "tool_counts_by_role": dict(collections.Counter(item.get("role") for item in finished)),
        "tool_counts_by_name": dict(collections.Counter(item.get("tool") for item in finished)),
        "tool_failures": [item for item in finished if item.get("status") != "success"],
        "review_rounds": reviews,
        "retrieval_summary": retrieval_summary,
        "final_content": assistant.get("content"),
        "selected_hits": (assistant.get("searchResult") or {}).get("hits", []),
        "limits": "Original SSE, final persisted response and raw logs remain separate. Tool summaries are not full model prompts or hidden reasoning. Query/Evaluator ephemeral transcripts may be unavailable.",
    }
    save(directory, "tool-events.json", tools)
    save(directory, "review-events.json", reviews)
    save(directory, "retrieval-trace.json", trace)
    save(directory, "analysis.json", analysis)
    print(json.dumps({key: value for key, value in analysis.items()
                      if key not in {"tool_failures", "review_rounds", "retrieval_summary", "selected_hits", "limits"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
