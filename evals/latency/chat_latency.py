"""Measure end-to-end Chat latency on a fixed query set without the HTTP server.

Builds the same services as the API process but never starts the parse
consumer, so it can run beside a live server. Each case is sent in its own
conversation titled ``[latency-eval] ...`` and the stage timing plus answer
are written as JSON lines.

Usage:
    uv run --no-sync --env-file .env.local python -m evals.latency.chat_latency \
        --cases evals/latency/ep04_cases.json --out outputs/verification/<run>.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from uuid import uuid4


def _build_services():
    from server.config import ServerSettings
    from server.main import init_application
    from server.service.agent_attachments import AgentAttachmentService
    from server.service.upload import UploadService

    # Startup reconciliation belongs to the live server; skip it here.
    UploadService.reconcile_deletions = lambda self: None
    AgentAttachmentService.reconcile_deletions = lambda self: None
    return init_application(ServerSettings.from_environment())


def _run_case(agent, case: dict, *, tenant_id: str, label: str) -> dict:
    conversation = agent.create_conversation(
        tenant_id=tenant_id, title=f"[latency-eval] {label} {case['id']}",
        idempotency_key=f"latency-eval-{uuid4()}",
    )
    started = time.monotonic()
    result = agent.send_message(
        conversation.conversation_id, tenant_id=tenant_id, content=case["query"],
        idempotency_key=f"latency-eval-{uuid4()}", series_ids=case["series_ids"],
    )
    wall = time.monotonic() - started
    message = result.assistant_message
    timing = message.timing
    content = message.content or ""
    expected = case.get("expect_any", [])
    return {
        "case": case["id"],
        "label": label,
        "conversation_id": conversation.conversation_id,
        "message_id": message.message_id,
        "status": str(message.status),
        "wall_seconds": round(wall, 1),
        "stages": [
            {"phase": str(stage.phase.value if hasattr(stage.phase, "value") else stage.phase),
             "seconds": round(stage.duration_ms / 1000, 1)}
            for stage in (timing.stages if timing is not None else ())
        ],
        "hit": any(token in content for token in expected) if expected else None,
        "content": content,
        "error": message.last_error.code if message.last_error is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--label", default="run")
    parser.add_argument("--tenant-id", default="local-default")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.only:
        cases = [case for case in cases if case["id"] in set(args.only)]
    runtime = _build_services()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.out.open("a", encoding="utf-8") as output:
            for _ in range(args.repeat):
                for case in cases:
                    row = _run_case(runtime.services.agent, case, tenant_id=args.tenant_id, label=args.label)
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    output.flush()
                    stages = " ".join(f"{stage['phase'][:4]}={stage['seconds']:.0f}" for stage in row["stages"])
                    print(f"{row['case']:<24} {row['status']:<10} {row['wall_seconds']:>6.1f}s "
                          f"hit={row['hit']} {stages}", flush=True)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
