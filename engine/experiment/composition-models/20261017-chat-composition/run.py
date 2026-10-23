"""Submit one reference image to the existing AniVAG Chat and preserve its response."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:9621"


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    sample = manifest["sample"]
    raw = (ROOT / sample["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sample["sha256"]
    run_dir = ROOT / "run-01"
    run_dir.mkdir(exist_ok=False)
    started = monotonic()
    result = {"status": "RUNNING", "started_at": datetime.now().astimezone().isoformat(),
              "base_url": BASE, "question": manifest["question"], "events": [],
              "manifest_sha256": hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "conversation_idempotency_key": str(uuid4()), "message_idempotency_key": str(uuid4())}

    def save():
        (run_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    save()
    try:
        with httpx.Client(base_url=BASE, timeout=1300, trust_env=False) as client:
            preflight = {}
            for name, path, params in [
                ("health", "/healthz", {}),
                ("runtime", "/api/agent/global-settings", {}),
                ("series", "/api/series/get", {"tenantId": manifest["tenant_id"], "seriesId": manifest["series_id"]}),
            ]:
                response = client.get(path, params=params)
                response.raise_for_status()
                preflight[name] = response.json()
            (run_dir / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2) + "\n")
            response = client.post("/api/agent/conversations/create",
                headers={"Idempotency-Key": result["conversation_idempotency_key"]},
                json={"tenantId": manifest["tenant_id"], "title": manifest["title"]})
            response.raise_for_status()
            conversation = response.json()["conversationId"]
            result["conversation_id"] = conversation
            save()
            params = {"tenantId": manifest["tenant_id"], "conversationId": conversation}
            response = client.post("/api/agent/attachments/upload", params=params,
                headers={"Content-Type": sample["media_type"]}, content=raw)
            response.raise_for_status()
            uri = response.json()["mediaUri"]
            response = client.get("/api/agent/attachments/content", params={**params, "mediaUri": uri})
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == sample["sha256"]
            result["uploaded_sha256_verified"] = sample["sha256"]
            payload = {"tenantId": manifest["tenant_id"], "content": manifest["question"],
                       "seriesIds": [manifest["series_id"]], "topK": manifest["top_k"],
                       "attachments": [{"mediaUri": uri}]}
            result["request_json"] = payload
            save()
            print(json.dumps({"conversation_id": conversation, "uploaded_and_verified": 1}), flush=True)
            event_type = None
            with client.stream("POST", "/api/agent/messages/send", params=params, json=payload,
                headers={"Idempotency-Key": result["message_idempotency_key"], "Accept": "text/event-stream"}) as stream:
                result["http_status"] = stream.status_code
                result["request_id"] = stream.headers.get("x-request-id")
                stream.raise_for_status()
                with (run_dir / "run.sse").open("w") as sse:
                    for line in stream.iter_lines():
                        sse.write(line + "\n")
                        sse.flush()
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:].strip())
                        kind = data.get("type") or event_type
                        event = {"elapsed_ms": round((monotonic()-started)*1000), "type": kind, "sequence": data.get("sequence")}
                        result["events"].append(event)
                        if data.get("messageId"):
                            result["assistant_message_id"] = data["messageId"]
                        if kind in {"answer.completed", "answer.failed", "answer.interrupted"}:
                            result["terminal_event"] = kind
                        save()
                        if kind != "answer.delta":
                            print(json.dumps(event), flush=True)
            response = client.get("/api/agent/messages/list", params={**params, "limit": 100})
            response.raise_for_status()
            result["persisted_messages"] = response.json()
            assistants = [m for m in response.json()["items"] if m["role"] == "ASSISTANT"]
            if assistants:
                answer = assistants[-1]
                result.update(assistant_message_id=answer["messageId"], answer=answer.get("content"),
                              assistant_status=answer.get("status"), model_name=answer.get("modelName"))
                (run_dir / "assistant.json").write_text(json.dumps(answer, ensure_ascii=False, indent=2)+"\n")
                (run_dir / "answer.md").write_text(answer.get("content") or "")
            result["status"] = "FINISHED" if result.get("terminal_event") == "answer.completed" and result.get("assistant_status") == "COMPLETED" else "INCOMPLETE"
    except Exception as exc:
        result["status"] = "CLIENT_ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["recovery_note"] = "Do not resend; read messages/list for the saved conversation to check server execution."
        raise
    finally:
        result["finished_at"] = datetime.now().astimezone().isoformat()
        result["duration_ms"] = round((monotonic()-started)*1000)
        save()
        print(json.dumps({k: result.get(k) for k in ["status", "conversation_id", "terminal_event", "assistant_status", "duration_ms"]}), flush=True)


if __name__ == "__main__":
    main()
