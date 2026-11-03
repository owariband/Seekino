"""Run bounded independent QA questions over the real API; never infer streaming FTT."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from time import monotonic
from urllib.parse import urlparse
from uuid import uuid4

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live or urlparse(args.api).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Use --live and the preflight-verified isolated loopback API")
    suite = json.loads(args.cases.read_text())
    if not 1 <= len(suite["turns"]) <= 6:
        parser.error("This runner accepts one to six independent questions")
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n")
    results = []

    def save() -> None:
        (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")

    async with httpx.AsyncClient(base_url=args.api, trust_env=False, timeout=250) as client:
        for case in suite["turns"]:
            created = await client.post("/api/agent/conversations/create", json={
                "tenantId": "local-default", "title": f"[course-qa] {case['id']} boundary validation",
            }, headers={"Idempotency-Key": f"course-qa-{uuid4()}"})
            created.raise_for_status()
            conversation = created.json()["conversationId"]
            payload = {
                "tenantId": "local-default", "content": case["query"], "seriesIds": [], "reference": None,
                "extraContext": {"playback": {
                    "seriesId": suite["series_id"], "videoId": suite["video_id"],
                    "episodeOrder": 1, "positionSeconds": suite["position_ms"],
                }},
            }
            row = {
                "case": case["id"], "conversation_id": conversation, "request": deepcopy(payload),
                "ftt_seconds": None, "ftt_status": "not_measured",
                "ftt_reason": "HTTP client events alone do not prove live provider-delta forwarding or browser rendering",
                "tt_seconds": None, "status": "RUNNING", "evidence_scope": "real_http_not_browser",
            }
            results.append(row)
            save()
            print(json.dumps({"event": "sending", "case": case["id"], "conversation": conversation}), flush=True)
            started = monotonic()
            try:
                async with asyncio.timeout(240):
                    async with client.stream("POST", "/api/agent/messages/send", params={"conversationId": conversation},
                                             json=payload, headers={"Accept": "text/event-stream", "Idempotency-Key": f"course-qa-{uuid4()}"}) as response:
                        response.raise_for_status()
                        event = None
                        async for line in response.aiter_lines():
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data = json.loads(line[5:])
                                elapsed = monotonic() - started
                                with (args.out / "events.jsonl").open("a") as handle:
                                    handle.write(json.dumps({"case": case["id"], "event": event, "seconds": elapsed, "data": data}, ensure_ascii=False) + "\n")
                                if data.get("messageId"):
                                    row["message_id"] = data["messageId"]
                                if event in {"answer.completed", "answer.failed", "answer.interrupted"}:
                                    message = data.get("assistantMessage", {})
                                    row.update(status=message.get("status", event), answer=message.get("content"), terminal_event=event)
                                    if event == "answer.completed":
                                        row["tt_seconds"] = elapsed
                                    else:
                                        row["terminal_seconds"] = elapsed
                                    save()
            except Exception as error:
                row.update(status="RUNNER_ERROR", error=type(error).__name__, terminal_seconds=monotonic() - started)
                save()
                if row.get("message_id"):
                    try:
                        cancelled = await client.post("/api/agent/messages/cancel", params={
                            "tenantId": "local-default", "conversationId": conversation, "messageId": row["message_id"],
                        }, timeout=15)
                        row["cancel_ack_status"] = cancelled.status_code
                    except Exception as cancel_error:
                        row["cancel_error"] = type(cancel_error).__name__
                save()
                raise
            saved = await client.get("/api/agent/messages/list", params={"tenantId": "local-default", "conversationId": conversation}, timeout=15)
            saved.raise_for_status()
            (args.out / f"{case['id']}-messages.json").write_text(json.dumps(saved.json(), ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"event": "finished", "case": case["id"], "status": row["status"], "tt_seconds": row["tt_seconds"]}), flush=True)
            if row.get("terminal_event") != "answer.completed":
                raise RuntimeError("Non-success terminal; no further paid requests")


if __name__ == "__main__":
    asyncio.run(main())
