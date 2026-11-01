"""Bounded real-HTTP replay, conflict, cancel and recovery probe in QA conversations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--browser-output", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live or urlparse(args.api).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Explicit --live and an isolated loopback API are required")
    args.out.mkdir(parents=True, exist_ok=False)
    requests = json.loads((args.browser_output / "requests.json").read_text())
    original = requests[0]
    conversation = parse_qs(urlparse(original["url"]).query)["conversationId"][0]
    client = httpx.Client(base_url=args.api, trust_env=False, timeout=240)
    result = {}

    def save() -> None:
        (args.out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    def messages(cid: str):
        response = client.get("/api/agent/messages/list", params={"conversationId": cid, "tenantId": "local-default"})
        response.raise_for_status()
        return response.json()

    before = messages(conversation)
    response = client.post("/api/agent/messages/send", params={"conversationId": conversation}, json=original["payload"],
                           headers={"Idempotency-Key": original["key"]})
    result["completed_replay"] = {"status": response.status_code, "body": response.json(), "before": before, "after": messages(conversation)}
    save()
    assert response.status_code == 200
    assert result["completed_replay"]["before"] == result["completed_replay"]["after"]
    response = client.post("/api/agent/messages/send", params={"conversationId": conversation},
                           json={**original["payload"], "content": "QA altered payload must be rejected"},
                           headers={"Idempotency-Key": original["key"]})
    result["changed_payload"] = {"status": response.status_code, "body": response.json()}
    save()
    assert response.status_code == 409
    save()

    response = client.post("/api/agent/conversations/create", json={"tenantId": "local-default", "title": "[course-qa] cancellation and recovery"},
                           headers={"Idempotency-Key": f"course-qa-{uuid4()}"})
    response.raise_for_status()
    cid = response.json()["conversationId"]
    result["conversation_id"] = cid
    payload = {**original["payload"], "content": "请仔细核对当前画面和附近讲解，解释这里的文本生成例子。"}
    key = f"course-qa-cancel-{uuid4()}"
    event_started = threading.Event()
    events = []
    began = time.monotonic()

    def consume():
        try:
            with httpx.Client(base_url=args.api, trust_env=False, timeout=240) as stream_client:
                with stream_client.stream("POST", "/api/agent/messages/send", params={"conversationId": cid}, json=payload,
                                          headers={"Accept": "text/event-stream", "Idempotency-Key": key}) as stream:
                    stream.raise_for_status()
                    event = None
                    for line in stream.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            row = {"event": event, "seconds": time.monotonic() - began, "data": json.loads(line[5:])}
                            events.append(row)
                            with (args.out / "cancel-events.jsonl").open("a") as handle:
                                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                            if event == "answer.started":
                                event_started.set()
        except Exception as error:
            result["stream_error"] = repr(error)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    assert event_started.wait(20), "No answer.started; inspect server before continuing"
    message_id = next(row["data"]["messageId"] for row in events if row["event"] == "answer.started")
    duplicate = client.post("/api/agent/messages/send", params={"conversationId": cid}, json=payload, headers={"Idempotency-Key": key})
    result["inflight_replay"] = {"status": duplicate.status_code, "body": duplicate.json()}
    cancelled_at = time.monotonic()
    cancellation = client.post("/api/agent/messages/cancel", params={"tenantId": "local-default", "conversationId": cid, "messageId": message_id})
    result["cancel"] = {"status": cancellation.status_code, "body": cancellation.json(), "response_seconds": time.monotonic() - cancelled_at}
    save()
    print(json.dumps({"event": "cancel_requested", "status": cancellation.status_code}), flush=True)
    worker.join(90)
    result["stream_finished"] = not worker.is_alive()
    result["after_cancel"] = messages(cid)
    save()
    assert duplicate.status_code == 409
    assert cancellation.status_code == 200
    assert not worker.is_alive(), "Cancellation did not finish in observation window"
    assistant = [row for row in result["after_cancel"]["items"] if row["role"] == "ASSISTANT"]
    assert len(assistant) == 1 and assistant[0]["status"] == "FAILED" and assistant[0]["lastError"]["code"] == "AGENT_CANCELLED", assistant

    started = time.monotonic()
    recovery = client.post("/api/agent/messages/send", params={"conversationId": cid},
                           json={"tenantId": "local-default", "content": "上一条已取消。请只回答‘收到’，不要调用任何工具。", "seriesIds": []},
                           headers={"Idempotency-Key": f"course-qa-recovery-{uuid4()}"})
    result["recovery"] = {"status": recovery.status_code, "body": recovery.json(), "seconds": time.monotonic() - started}
    save()
    assert recovery.status_code == 200 and recovery.json()["assistantMessage"]["status"] == "COMPLETED"

    # Formal retrieval must not pretend that the unfinished course is searchable.
    unavailable = client.post("/api/agent/messages/send", params={"conversationId": cid},
                              json={"tenantId": "local-default", "content": "从这门课程中找到反向传播的讲解并给出处。", "seriesIds": [original["payload"]["extraContext"]["playback"]["seriesId"]]},
                              headers={"Idempotency-Key": f"course-qa-scope-{uuid4()}"})
    result["unready_scope"] = {"status": unavailable.status_code, "body": unavailable.json()}
    result["final_messages"] = messages(cid)
    save()
    print(json.dumps({"replay": result["completed_replay"]["status"], "conflict": result["changed_payload"]["status"],
                      "cancel": assistant[0]["lastError"]["code"], "recovery_seconds": result["recovery"]["seconds"],
                      "unready_scope": unavailable.status_code}), flush=True)
    client.close()


if __name__ == "__main__":
    main()
