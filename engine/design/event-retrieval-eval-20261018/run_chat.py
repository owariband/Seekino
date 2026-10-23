"""One authorized live Chat experiment; preserves SSE, final SQL view and scoped logs."""
from __future__ import annotations

import json
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "chat-natural"
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:9621"
QUERY = "真是高高在上的，出于哪一集？"
SERIES = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"
TENANT = "local-default"


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def main():
    global OUT, QUERY, SERIES
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, help="Local JSON with question, series_id and output_subdirectory.")
    args = parser.parse_args()
    case = json.loads(args.case.read_text()) if args.case is not None else {}
    if case:
        QUERY, SERIES = case["question"], case["series_id"]
        OUT = Path(__file__).resolve().parent / case["output_subdirectory"]
    OUT.mkdir(exist_ok=True)
    log = ROOT / "outputs/server/logs/development/api.log"
    offset = log.stat().st_size if log.exists() else None
    request = {"tenantId": TENANT, "content": QUERY, "seriesIds": [SERIES]}
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "base_url": BASE,
                "request": request, "conversation_key": str(uuid4()), "message_key": str(uuid4()),
                "server_log_offset": offset, "instruction": "No Event hint or episode/speaker seed in question; selected Series scope and default budgets.",
                "case_file": str(args.case) if args.case is not None else None}
    save("manifest.json", manifest)
    started = time.perf_counter()
    timings = {}
    events = []
    with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
        response = client.post(BASE + "/api/agent/conversations/create",
            headers={"Idempotency-Key": manifest["conversation_key"]},
            json={"tenantId": TENANT, "title": case.get("title", "Event 自然问题实测 2026-10-18")})
        response.raise_for_status()
        conversation = response.json()
        save("conversation.json", conversation)
        cid = conversation["conversationId"]
        manifest["conversation_id"] = cid
        save("manifest.json", manifest)
        print(json.dumps({"conversation_id": cid, "query": QUERY}, ensure_ascii=False), flush=True)
        with (OUT / "sse.raw.log").open("w") as raw, (OUT / "events.jsonl").open("w") as event_log:
            with client.stream("POST", BASE + "/api/agent/messages/send",
                params={"conversationId": cid},
                headers={"Accept": "text/event-stream", "Idempotency-Key": manifest["message_key"]},
                json=request) as stream:
                stream.raise_for_status()
                lines = []
                for line in stream.iter_lines():
                    raw.write(line + "\n")
                    raw.flush()
                    if line:
                        lines.append(line)
                        continue
                    payload = "\n".join(line[5:].strip() for line in lines if line.startswith("data:"))
                    lines = []
                    if not payload:
                        continue
                    elapsed = round(time.perf_counter() - started, 4)
                    data = json.loads(payload)
                    kind = data.get("type")
                    entry = {"elapsed_seconds": elapsed, "received_at": datetime.now(timezone.utc).isoformat(), "data": data}
                    events.append(entry)
                    event_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    event_log.flush()
                    timings.setdefault(kind, elapsed)
                    if kind == "answer.delta" and data.get("delta", "").strip():
                        timings.setdefault("first_nonblank_text", elapsed)
                    if kind in {"answer.started", "answer.progress", "answer.completed", "answer.failed", "answer.interrupted"}:
                        print(json.dumps({"type": kind, "seconds": elapsed, "phase": data.get("phase")}, ensure_ascii=False), flush=True)
                    if kind in {"answer.completed", "answer.failed", "answer.interrupted"}:
                        save("terminal-event.json", data)
                        break
        response = client.get(BASE + "/api/agent/messages/list", params={"conversationId": cid, "tenantId": TENANT})
        response.raise_for_status()
        save("persisted-messages.json", response.json())
    save("timings.json", timings)
    if offset is not None:
        with log.open("rb") as file:
            file.seek(offset)
            content = file.read().decode(errors="replace")
        (OUT / "server.window.raw.log").write_text(content)
        (OUT / "server.scoped.log").write_text("\n".join(line for line in content.splitlines() if cid in line) + "\n")
    print(json.dumps({"conversation_id": cid, "timings": timings}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
