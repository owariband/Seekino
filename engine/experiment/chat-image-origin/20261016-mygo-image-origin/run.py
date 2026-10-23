"""Submit the manifest's original question and images to a fresh local chat."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx


def main() -> int:
    experiment = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=experiment / "manifest.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:9621")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--log-file", type=Path, default=experiment.parents[3] / "outputs/server/logs/development/api.log")
    parser.add_argument("--validate-only", action="store_true", help="Check local input hashes without contacting the API")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    samples = []
    for sample in manifest["samples"]:
        raw = (args.manifest.parent / sample["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sample["sha256"]:
            raise ValueError(f"Sample hash mismatch: {sample['sample_id']}")
        samples.append((sample, raw))
    if args.validate_only:
        print(json.dumps({"samples_verified": len(samples), "question": manifest["question"], "api_called": False}, ensure_ascii=False))
        return 0
    if args.output_dir is None or args.timeout <= 0:
        parser.error("A new --output-dir and a positive --timeout are required")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    out = args.output_dir / "run.json"
    offset = args.log_file.stat().st_size if args.log_file.is_file() else None
    started = monotonic()
    result = {
        "experiment_id": manifest["experiment_id"], "run_id": args.output_dir.name,
        "started_at": datetime.now().astimezone().isoformat(), "status": "RUNNING",
        "question": manifest["question"], "base_url": args.base_url,
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "samples_verified": [], "events": [],
    }

    def save() -> None:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    save()
    try:
        with httpx.Client(base_url=args.base_url, timeout=args.timeout, trust_env=False) as client:
            response = client.get("/healthz")
            response.raise_for_status()
            result["health"] = response.json()
            response = client.post(
                "/api/agent/conversations/create", headers={"Idempotency-Key": str(uuid4())},
                json={"tenantId": manifest["tenant_id"], "title": "MyGO 六图出处实验"},
            )
            response.raise_for_status()
            conversation = response.json()["conversationId"]
            result["conversation_id"] = conversation
            params = {"tenantId": manifest["tenant_id"], "conversationId": conversation}
            attachments = []
            for sample, raw in samples:
                response = client.post(
                    "/api/agent/attachments/upload", params=params,
                    headers={"Content-Type": sample["media_type"]}, content=raw,
                )
                response.raise_for_status()
                uri = response.json()["mediaUri"]
                back = client.get("/api/agent/attachments/content", params={**params, "mediaUri": uri})
                back.raise_for_status()
                if hashlib.sha256(back.content).hexdigest() != sample["sha256"]:
                    raise ValueError(f"Uploaded sample hash mismatch: {sample['sample_id']}")
                attachments.append({"mediaUri": uri})
                result["samples_verified"].append({"sample_id": sample["sample_id"], "sha256": sample["sha256"]})
                save()
            payload = {
                "tenantId": manifest["tenant_id"], "content": manifest["question"],
                "seriesIds": [manifest["series_id"]], "attachments": attachments,
            }
            result["request_json"] = payload
            result["idempotency_key"] = str(uuid4())
            save()
            print(json.dumps({"conversation_id": conversation, "uploaded_and_verified": len(attachments)}), flush=True)
            event_type = None
            with client.stream(
                "POST", "/api/agent/messages/send", params=params, json=payload,
                headers={"Idempotency-Key": result["idempotency_key"], "Accept": "text/event-stream"},
            ) as stream:
                result["http_status"] = stream.status_code
                result["request_id"] = stream.headers.get("x-request-id")
                stream.raise_for_status()
                with (args.output_dir / "run.sse").open("w") as sse:
                    for line in stream.iter_lines():
                        sse.write(line + "\n")
                        sse.flush()
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:].strip())
                        kind = data.get("type") or event_type
                        event = {"elapsed_ms": round((monotonic() - started) * 1000), "type": kind, "sequence": data.get("sequence")}
                        result["events"].append(event)
                        if data.get("messageId"):
                            result["assistant_message_id"] = data["messageId"]
                        if kind in {"answer.completed", "answer.failed", "answer.interrupted"}:
                            result["terminal_event"] = kind
                        save()
                        print(json.dumps(event), flush=True)
            response = client.get("/api/agent/messages/list", params={**params, "limit": 100})
            response.raise_for_status()
            result["persisted_messages"] = response.json()
            assistants = [m for m in response.json()["items"] if m["role"] == "ASSISTANT"]
            if assistants:
                result["assistant_message_id"] = assistants[-1]["messageId"]
                result["answer"] = assistants[-1].get("content")
                result["assistant_status"] = assistants[-1].get("status")
            if result.get("terminal_event") == "answer.completed" and result.get("assistant_status") == "COMPLETED":
                result["status"] = "FINISHED"
            else:
                result["status"] = {
                    "answer.failed": "FAILED", "answer.interrupted": "INTERRUPTED",
                }.get(result.get("terminal_event"), "INCOMPLETE")
    except KeyboardInterrupt:
        result["status"] = "INTERRUPTED"
        raise
    except Exception as exc:
        result["status"] = "FAILED"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["finished_at"] = datetime.now().astimezone().isoformat()
        result["duration_ms"] = round((monotonic() - started) * 1000)
        if offset is not None and args.log_file.is_file():
            with args.log_file.open("rb") as source:
                source.seek(offset)
                (args.output_dir / "run.log").write_bytes(source.read())
        save()
        print(json.dumps({key: result.get(key) for key in ("status", "terminal_event", "assistant_status", "duration_ms")}), flush=True)
    return 0 if result["status"] == "FINISHED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
