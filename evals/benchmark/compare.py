"""Run the Chat suites against three Supervisor harnesses using Kimi only.

Default is offline planning. --live requires existing SQL/ES, a prepared Pi
runtime, and an explicitly supplied Kimi config. Raw runs stay under outputs/.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

import httpx

from . import run
from .harnesses import alternative_supervisor, validate_kimi

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ARMS = ("deepagents", "pi", "codex")
TENANT = "local-default"
SERIES = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def source_hashes() -> dict:
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("agent", "engine", "gateway", "server", "evals/benchmark")
            for p in sorted((ROOT / folder).rglob("*.py"))}


def schedule(behavior_repeats: int) -> list[dict]:
    behavior = json.loads((HERE / "chat_behavior/cases.json").read_text())
    coverage = run.load_cases(HERE / "query_capabilities/cases.json")["cases"]
    rows = []
    for suite, cases, repeats in [("chat_behavior", behavior, behavior_repeats), ("query_capabilities", coverage, 1)]:
        for repeat in range(1, repeats + 1):
            ordered = cases if repeat % 2 else list(reversed(cases))
            for position, case in enumerate(ordered):
                offset = (position + repeat - 1) % len(ARMS)
                for arm in ARMS[offset:] + ARMS[:offset]:
                    rows.append({"index": len(rows), "suite": suite, "case_id": case["id"], "arm": arm, "repeat": repeat})
    return rows


def case_directory(output: Path, step: dict) -> Path:
    return output / "runs" / f"{step['index']:03}-{step['arm']}-{step['case_id']}"


class RecordingSupervisor:
    def __init__(self, delegate, directory: Path, began):
        self.delegate, self.directory, self.began = delegate, directory, began
        self.texts, self.tools = [], []
        self.lock = Lock()

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run_agent(self, prompt, **options):
        save(self.directory / "model-input.json", {"prompt": prompt,
            "user_message": options["user_message"], "history": options["history"],
            "tools": [{"name": t.name, "description": t.description, "schema": t.get_input_schema().model_json_schema()}
                      for t in options["tools"]]})

        def wrap(tool):
            original = tool.func
            def invoke(*args, **kwargs):
                row = {"name": tool.name, "arguments": kwargs, "started_seconds": time.monotonic() - self.began[0]}
                with self.lock:
                    self.tools.append(row)
                try:
                    result = original(*args, **kwargs)
                    row["result_text"] = result[0] if isinstance(result, tuple) else str(result)
                    return result
                except Exception as error:
                    row["error"] = {"type": type(error).__name__, "message": str(error)}
                    raise
                finally:
                    row["ended_seconds"] = time.monotonic() - self.began[0]
                    with self.lock, (self.directory / "tools.jsonl").open("a") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            return tool.model_copy(update={"func": invoke})

        options["tools"] = [wrap(tool) for tool in options["tools"]]
        original_text = options["handle_text"]
        def accept(text):
            self.texts.append({"text": text, "seconds": time.monotonic() - self.began[0]})
            original_text(text)
        options["handle_text"] = accept
        return self.delegate.run_agent(prompt, **options)


def worker(output: Path, config_path: Path, index: int) -> None:
    from fastapi.testclient import TestClient
    from evals.latency.chat_latency import _build_services
    from gateway.config import load_config
    from gateway.usage import capture_model_usage
    from server.api.app import build_app
    from server.config import ServerSettings
    from server.model.agent import AgentConversation, AgentMessage

    protocol = json.loads((output / "protocol.json").read_text())
    step = protocol["schedule"][index]
    directory = case_directory(output, step)
    directory.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(level=logging.WARNING, filename=directory / "runtime.log")
    assert source_hashes() == protocol["source_sha256"], "Source changed after protocol freeze"
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == protocol["config_sha256"]
    for suite, digest in protocol["case_sha256"].items():
        assert hashlib.sha256((HERE / suite / "cases.json").read_bytes()).hexdigest() == digest, "Fixture changed after freeze"
    config = load_config(config_path); validate_kimi(config)
    fixture = run.load_cases(HERE / "query_capabilities/cases.json")
    cases = (json.loads((HERE / "chat_behavior/cases.json").read_text()) if step["suite"] == "chat_behavior" else fixture["cases"])
    case = next(c for c in cases if c["id"] == step["case_id"])
    previous = None
    if case.get("after"):
        parent = next(s for s in protocol["schedule"] if s["suite"] == step["suite"] and s["arm"] == step["arm"]
                      and s["case_id"] == case["after"] and s["repeat"] == step["repeat"])
        previous = json.loads((case_directory(output, parent) / "result.json").read_text())
        if previous["observed"]["status"] != "COMPLETED":
            save(directory / "result.json", {**step, "observed": {"status": "BLOCKED", "error": "predecessor_not_completed"}})
            return
    with ExitStack() as cleanup:
        application = _build_services(); cleanup.callback(application.close)
        agent = application.services.agent
        runtime = agent._runtime
        if step["arm"] != "deepagents":
            model, close = alternative_supervisor(step["arm"], config, output, output / "legacy-pi")
            cleanup.callback(close)
            cleanup.callback(model.close)
            runtime._model = model
        began = [time.monotonic()]
        recorder = RecordingSupervisor(runtime._model, directory, began)
        runtime._model = recorder
        usage_lock, finished = Lock(), Event()
        def observe(call):
            row = {k: getattr(call, k) for k in ("call_id", "model", "provider", "operation", "status",
                   "started_at", "ended_at", "granularity", "usage_source")}
            row.update(context=dict(call.context), usage=asdict(call.usage) if call.usage else None)
            with usage_lock, (directory / "usage.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        original_kick = runtime.kick
        def kick(inputs, **kwargs):
            save(directory / "active.json", {"conversation_id": inputs["conversation_id"], "message_id": inputs["assistant_message_id"]})
            try:
                with capture_model_usage(observe, combine=True):
                    return original_kick(inputs, **kwargs)
            except Exception as error:
                causes = []
                while error is not None and len(causes) < 8:
                    message = str(error)
                    for name in ("KIMI_API_KEY", "ARK_API_KEY", "OPENROUTER_JEV_API_KEY"):
                        if os.environ.get(name):
                            message = message.replace(os.environ[name], "[REDACTED]")
                    causes.append({"type": type(error).__name__, "message": message})
                    error = error.__cause__
                save(directory / "failure.json", causes)
                raise
            finally:
                finished.set()
        runtime.kick = kick
        app = build_app(application.services, settings=ServerSettings.from_environment())
        with TestClient(app) as client:
            if previous:
                cid = previous["observed"]["conversation_id"]
            else:
                response = client.post("/api/agent/conversations/create", headers={"Idempotency-Key": str(uuid4())},
                    json={"tenantId": TENANT, "title": f"[harness-kimi] {step['arm']} {step['suite']} {step['case_id']} r{step['repeat']}"})
                response.raise_for_status(); cid = response.json()["conversationId"]
            history = case.get("state", {}).get("recent_messages", [])
            if history:
                with agent._repository._session_factory.begin() as session:
                    conversation = session.get(AgentConversation, cid)
                    for sequence, item in enumerate(history):
                        session.add(AgentMessage(id=str(uuid4()), conversation_id=cid, sequence_no=sequence,
                            role=item["role"].upper(), status="COMPLETED", content=item["content"], series_ids=[SERIES], version=1))
                    conversation.next_message_sequence = len(history)
            payload = (run.chat_request(fixture, case, TENANT) if step["suite"] == "query_capabilities" else
                       {"tenantId": TENANT, "content": case["state"]["query"], "seriesIds": [SERIES]})
            save(directory / "input.json", {"request": payload, "history": history, "conversation_id": cid})
            began[0] = time.monotonic()
            row = dict(step)
            try:
                with capture_model_usage(observe):
                    response = client.post("/api/agent/messages/send", params={"conversationId": cid},
                        headers={"Idempotency-Key": str(uuid4()), "Accept": "application/json"}, json=payload)
                row["wall_seconds"] = time.monotonic() - began[0]
                body = response.json(); save(directory / "response.json", body)
                row["http_status"] = response.status_code
                answer = body.get("assistantMessage")
                row["observed"] = (run.normalize_chat(answer) if answer else
                    {"status": "HTTP_ERROR", "error": body})
            except Exception as error:
                row.update(wall_seconds=time.monotonic() - began[0],
                    observed={"status": "HARNESS_ERROR", "error_type": type(error).__name__, "error": str(error)})
            row["observed"]["conversation_id"] = cid
            row["first_text_seconds"] = recorder.texts[0]["seconds"] if recorder.texts else None
            row["text_events"] = recorder.texts
            row["tool_calls"] = [{k: v for k, v in t.items() if k != "result_text"} for t in recorder.tools]
            row["worker_finished"] = finished.wait(15)
            if step["suite"] == "query_capabilities":
                row["evaluation"] = run.evaluate(fixture, case, row["observed"])
            if row["observed"].get("message_id"):
                stored = agent.get_message(cid, tenant_id=TENANT, message_id=row["observed"]["message_id"]).assistant_message
                row["persistence"] = {"content_matches": stored.content == row["observed"].get("content"), "status": str(stored.status)}
            save(directory / "result.json", row)
            print(json.dumps({k: row.get(k) for k in ("index", "arm", "case_id", "wall_seconds", "first_text_seconds")}
                             | {"status": row["observed"]["status"]}), flush=True)


def cancel_owned(directory: Path) -> None:
    if not (directory / "active.json").exists():
        return
    from evals.latency.chat_latency import _build_services
    active = json.loads((directory / "active.json").read_text())
    application = _build_services()
    try:
        pair = application.services.agent.cancel_message(active["conversation_id"], tenant_id=TENANT, message_id=active["message_id"])
        save(directory / "cancellation.json", {**active, "status": str(pair.assistant_message.status)})
    finally:
        application.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--behavior-repeats", type=int, default=2)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--cancel", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.config, args.out = args.config.resolve(), args.out.resolve()
    os.environ["SEEKINO_MODEL_CONFIG"] = str(args.config)
    for key in ("SEEKINO_AGENT_CHAT_MODEL", "SEEKINO_AGENT_QUERY_MODEL", "SEEKINO_AGENT_FALLBACK_MODEL", "SEEKINO_MODEL_TRACE_DIR"):
        os.environ.pop(key, None)
    from gateway.config import load_config
    config = load_config(args.config); validate_kimi(config)
    if args.cancel:
        cancel_owned(args.cancel); return
    if args.worker is not None:
        worker(args.out, args.config, args.worker); return
    if args.behavior_repeats < 1:
        parser.error("--behavior-repeats must be positive")
    planned = schedule(args.behavior_repeats)
    if not args.live:
        print(json.dumps({"api_called": False, "planned_turns": len(planned), "arms": ARMS,
                          "model": "k3.1-pro", "schedule": planned}, ensure_ascii=False))
        return
    args.out.mkdir(parents=True, exist_ok=True)
    protocol_path = args.out / "protocol.json"
    if not protocol_path.exists():
        save(protocol_path, {"started_at": datetime.now(timezone.utc).isoformat(), "schedule": planned,
            "model": "k3.1-pro", "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "packages": {name: importlib.metadata.version(name) for name in ("deepagents", "langgraph", "langchain", "openai-codex")},
            "pi_runtime": json.loads((args.out / "legacy-pi/manifest.json").read_text()),
            "data_snapshot": "data-before.json",
            "source_sha256": source_hashes(), "case_sha256": {suite: hashlib.sha256((HERE / suite / "cases.json").read_bytes()).hexdigest()
                for suite in ("chat_behavior", "query_capabilities")},
            "method": "Real ASGI HTTP with production services; same prompt/tools/data/API bindings, only Supervisor harness replaced; no Parse workers.",
            "concurrency": 1, "watchdog_seconds": 570, "query_hook": config.query_hook.model_dump(),
            "limitations": ["Native instructions, built-in tools, compaction and per-call output controls differ by SDK; wall deadline and configured model/context are common.",
                "Codex uses Kimi Responses while Deep Agents/Pi use Chat Completions; this is a harness+adapter comparison, not an isolated algorithm effect.",
                "Existing shared dataset; record readiness and index snapshots. No model cache flush or independent blind grading."]})
    protocol = json.loads(protocol_path.read_text())
    assert protocol["schedule"] == planned
    assert protocol["source_sha256"] == source_hashes(), "Source changed after freeze"
    assert protocol["config_sha256"] == hashlib.sha256(args.config.read_bytes()).hexdigest()
    for step in planned:
        assert protocol["source_sha256"] == source_hashes(), "Source changed during comparison; stop before another model call"
        directory = case_directory(args.out, step)
        if (directory / "result.json").exists():
            continue
        print(json.dumps({"event": "started", **step}), flush=True)
        command = [sys.executable, "-m", "evals.benchmark.compare", "--config", str(args.config), "--out", str(args.out), "--worker", str(step["index"])]
        with (args.out / "workers.log").open("a") as log:
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            save(args.out / "active-worker.json", {**step, "pid": child.pid})
            timed_out = False
            try:
                child.wait(timeout=protocol["watchdog_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=10)
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "result.json").exists():
            save(directory / "result.json", {**step, "wall_seconds": protocol["watchdog_seconds"] if timed_out else None,
                "observed": {"status": "WATCHDOG_TIMEOUT" if timed_out else "WORKER_ERROR", "worker_exit_code": child.returncode},
                "wall_time_basis": "External watchdog, not successful request latency"})
            subprocess.run([sys.executable, "-m", "evals.benchmark.compare", "--config", str(args.config), "--out", str(args.out),
                            "--cancel", str(directory)], cwd=ROOT, timeout=45, check=True)
            if not (directory / "active.json").exists():
                raise RuntimeError(f"Worker failed before Chat started: {directory}; inspect workers.log before resuming")
        row = json.loads((directory / "result.json").read_text())
        row.update(worker_exit_code=child.returncode, cleanup_watchdog=timed_out and row["observed"]["status"] == "COMPLETED")
        save(directory / "result.json", row)
        print(json.dumps({"event": "finished", **step, "status": row["observed"]["status"], "seconds": row.get("wall_seconds")}), flush=True)
    with (args.out / "observations.jsonl").open("w") as stream:
        stream.write(json.dumps({"kind": "run", **protocol}, ensure_ascii=False) + "\n")
        for step in planned:
            stream.write(json.dumps(json.loads((case_directory(args.out, step) / "result.json").read_text()), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
