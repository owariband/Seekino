"""Compare the three Chat query-intent paths on real local services.

Path A uses the combined DeepSeek decision model (``models.query_hook``);
path B uses JEV routing plus the existing rewriter; path C skips the hook and
lets the current Supervisor decide.  One arm is one fresh conversation and one
full turn through ``AgentChatService.start_message(stream=True)``.  The parent
supplies the three model configs and case file; this module never prints or
serializes raw configs or credentials.

Default is an offline plan.  ``--live`` runs each frozen step in its own child
process with a 300 second turn cutoff, retains failures, and resumes without
rewriting completed ``result.json`` files.

Usage:
    python -m evals.benchmark.query_intent_compare \
        --cases cases.json --configs-dir configs --output outputs/run --plan
    python -m evals.benchmark.query_intent_compare \
        --cases cases.json --configs-dir configs --output outputs/run --live
    python -m evals.benchmark.query_intent_compare \
        --cases cases.json --configs-dir configs --output outputs/run --only-index 4
"""

from __future__ import annotations

import argparse
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from queue import Empty
import re
import signal
import subprocess
import sys
from threading import Lock
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("deepseek_decider", "jev_rewriter", "supervisor_direct")
ARM_FILES = {arm: f"{arm}.yaml" for arm in ARMS}
TURN_TIMEOUT_SECONDS = 300.0
CHILD_WATCHDOG_SECONDS = 360.0
EVAL_PREFIX = "query-intent"
DATA_ENV_FIELDS = (
    "SEEKINO_ENVIRONMENT", "SEEKINO_ELASTICSEARCH_INDEX",
    "SEEKINO_ELASTICSEARCH_EVENT_INDEX", "SEEKINO_ELASTICSEARCH_INDEX_GENERATION",
)
ARTIFACTS = (
    "result.json", "final.json", "input.json", "model_inputs.jsonl",
    "tools.jsonl", "usage.jsonl", "hook.jsonl", "events.jsonl",
    "failure.json", "harness_failure.json", "cancellation.json",
    "active.json", "runtime.log",
)
SOURCE_FILES = (
    "evals/benchmark/query_intent_compare.py",
    "evals/latency/chat_latency.py",
    "agent/chat/query_hook.py",
    "agent/chat/query_hook_prompts.py",
    "agent/chat/supervisor.py",
    "agent/chat/common/state.py",
    "gateway/query_hook.py",
    "gateway/usage.py",
    "gateway/config.py",
    "gateway/model_events.py",
    "gateway/agent_model/contracts.py",
    "gateway/agent_model/deepagents.py",
    "gateway/agent_model/factory.py",
    "server/service/agent_chat.py",
    "server/bootstrap.py",
    "server/config/agent.py",
    "server/config/global_agent_settings.py",
)
BINDING_FIELDS = (
    "source", "model", "stream", "temperature", "reasoning_effort",
    "max_output_tokens", "request_timeout_seconds", "context_window_tokens",
    "context_safety_margin_tokens", "auto_compact_ratio", "auto_compact_tokens",
    "compact_target_tokens", "max_input_chars",
)
RUNTIME_FIELDS = (
    "limits_enabled", "total_timeout_enabled", "max_loop_turns",
    "max_output_tokens", "max_prompt_chars", "context_window_tokens",
    "context_safety_margin_tokens", "auto_compact_tokens",
    "compact_target_tokens", "supervisor_reasoning_effort",
    "query_reasoning_effort", "stage_timeout_seconds",
    "supervisor_timeout_seconds", "execution_timeout_seconds",
    "maximum_concurrent_kicks", "maximum_parallel_queries",
)


def save(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, value) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_text(value: str) -> str:
    return _sha(value.encode("utf-8"))


def _case_hash(case: dict) -> str:
    canonical = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha_text(canonical)


def load_cases(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("cases file must be a JSON object")
    tenant = document.get("tenant_id")
    series = document.get("series_id")
    cases = document.get("cases")
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("cases.tenant_id must be a nonempty string")
    if not isinstance(series, str) or not series.strip():
        raise ValueError("cases.series_id must be a nonempty string")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.cases must be a nonempty list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError("every case needs a nonempty string id")
        if case["id"] in seen:
            raise ValueError(f"duplicate case id: {case['id']}")
        seen.add(case["id"])
        state = case.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("query"), str) or not state["query"].strip():
            raise ValueError(f"case {case['id']} needs state.query")
        history = state.get("recent_messages", [])
        if not isinstance(history, list):
            raise ValueError(f"case {case['id']} recent_messages must be a list")
        for item in history:
            if (
                not isinstance(item, dict)
                or item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("content"), str)
            ):
                raise ValueError(f"case {case['id']} has an invalid recent_messages item")
    repeats = document.get("repeat_case_ids", [])
    if not isinstance(repeats, list) or any(not isinstance(item, str) for item in repeats):
        raise ValueError("repeat_case_ids must be a list of case ids")
    missing = [item for item in repeats if item not in seen]
    if missing:
        raise ValueError(f"repeat_case_ids not present in cases: {missing}")
    if len(set(repeats)) != len(repeats):
        raise ValueError("repeat_case_ids must not contain duplicates")
    return document


def build_schedule(document: dict) -> list[dict]:
    cases = document["cases"]
    repeat_ids = set(document.get("repeat_case_ids", []))
    positions = {case["id"]: index for index, case in enumerate(cases)}
    rows: list[dict] = []

    def add(case, case_position, round_number, offset):
        order = ARMS[offset:] + ARMS[:offset]
        for arm_order, arm in enumerate(order):
            rows.append({
                "index": len(rows), "round": round_number, "arm_order": arm_order,
                "case_position": case_position, "case_id": case["id"], "arm": arm,
            })

    for case_position, case in enumerate(cases):
        add(case, case_position, 1, case_position % len(ARMS))
    for case in (item for item in reversed(cases) if item["id"] in repeat_ids):
        case_position = positions[case["id"]]
        # Turn one used case_position % 3; +1 always starts with a different arm.
        add(case, case_position, 2, (case_position + 1) % len(ARMS))
    return rows


def case_directory(output: Path, step: dict) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", step["case_id"])[:80]
    return output / "runs" / f"{step['index']:03d}-r{step['round']}-{step['arm']}-{safe}"


def source_hashes() -> dict:
    hashes = {}
    for relative in SOURCE_FILES:
        path = ROOT / relative
        if path.is_file():
            hashes[relative] = _sha(path.read_bytes())
    return hashes


def git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _binding_metadata(binding) -> dict:
    metadata = {
        name: getattr(binding, name)
        for name in BINDING_FIELDS if hasattr(binding, name)
    }
    fallback = getattr(binding, "fallback", None)
    metadata["fallback_model"] = getattr(fallback, "model", None) if fallback is not None else None
    return metadata


def config_info(path: Path) -> dict:
    """Hash a config and keep only non-secret effective policy metadata."""
    info = {"name": path.name, "path": str(path), "sha256": _sha(path.read_bytes())}
    try:
        from gateway.config import load_config

        config = load_config(path)
    except Exception as error:  # Plan still reports the hash when a config is absent/invalid.
        info["load_error"] = type(error).__name__
        return info
    hook = config.query_hook
    info["query_hook"] = {
        "enabled": hook.enabled,
        "rewriter_enabled": hook.rewriter_enabled,
        "router_model": hook.router_model,
        "router_timeout_seconds": hook.router_timeout_seconds,
        "rewriter_timeout_seconds": hook.rewriter_timeout_seconds,
        "total_timeout_seconds": hook.total_timeout_seconds,
        "max_output_tokens": hook.max_output_tokens,
        "history_message_limit": hook.history_message_limit,
        "history_max_chars": hook.history_max_chars,
        "query_max_chars": hook.query_max_chars,
    }
    info["wiring"] = {
        "has_query_hook_model": "query_hook" in config.normalized_models,
        "has_query_rewriter_model": "query_rewriter" in config.normalized_models,
    }
    info["agent_model"] = {
        name: getattr(config.agent_model, name)
        for name in ("source", "model", "stream", "max_output_tokens", "request_timeout_seconds")
    }
    info["models"] = {
        ref: _binding_metadata(config.normalized_models[ref])
        for ref in ("agent", "supervisor", "query_hook", "query_rewriter")
        if ref in config.normalized_models
    }
    try:
        info["effective_supervisor"] = _binding_metadata(config.resolve_model_binding("supervisor"))
        info["effective_supervisor_reasoning_effort"] = (
            info["effective_supervisor"].get("reasoning_effort")
            or config.agent_runtime.get("supervisor_reasoning_effort", "none")
        )
    except Exception as error:
        info["effective_supervisor_error"] = type(error).__name__
    info["agent_runtime"] = {
        name: config.agent_runtime[name]
        for name in RUNTIME_FIELDS if name in config.agent_runtime
    }
    return info


def validate_wiring(arm: str, info: dict) -> None:
    if "load_error" in info:
        raise ValueError(f"{arm} config did not load: {info['load_error']}")
    hook = info["query_hook"]
    wiring = info["wiring"]
    if arm == "deepseek_decider":
        if not hook["enabled"] or not wiring["has_query_hook_model"]:
            raise ValueError("deepseek_decider must enable query_hook and define models.query_hook")
    elif arm == "jev_rewriter":
        if not hook["enabled"]:
            raise ValueError("jev_rewriter must enable query_hook")
        if wiring["has_query_hook_model"]:
            raise ValueError("jev_rewriter must not define models.query_hook (that selects the combined decision path)")
        if not hook["rewriter_enabled"] or not wiring["has_query_rewriter_model"]:
            raise ValueError("jev_rewriter must enable and define models.query_rewriter")
    elif hook["enabled"]:
        raise ValueError("supervisor_direct must disable query_hook")


def _redact(value: str) -> str:
    for name, secret in os.environ.items():
        if len(secret) < 8:
            continue
        if any(token in name.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            value = value.replace(secret, "[REDACTED]")
    return value


def error_chain(error: BaseException) -> list[dict]:
    rows = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(rows) < 8:
        seen.add(id(current))
        rows.append({"type": type(current).__name__, "message": _redact(str(current))[:2000]})
        current = current.__cause__ or current.__context__
    return rows


def _prepare_env(config_path: Path) -> None:
    os.environ["SEEKINO_MODEL_CONFIG"] = str(config_path)
    for name in (
        "SEEKINO_AGENT_CHAT_MODEL", "SEEKINO_DEEPSEEK_CHAT_MODEL",
        "SEEKINO_AGENT_QUERY_MODEL", "SEEKINO_AGENT_FALLBACK_MODEL",
        "SEEKINO_MODEL_TRACE_DIR", "SEEKINO_CHAT_TRACE_DIR",
    ):
        os.environ.pop(name, None)


class RecordingModel:
    """Record Supervisor input and tool activity while delegating unchanged calls."""

    def __init__(self, delegate, directory: Path, began: list[float]) -> None:
        self.delegate, self.directory, self.began = delegate, directory, began
        self.inputs: list[dict] = []
        self.tools: list[dict] = []
        self.lock = Lock()

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run_agent(self, prompt: str, **options):
        record = {
            "started_seconds": time.monotonic() - self.began[0],
            "prompt": prompt,
            "prompt_sha256": _sha_text(prompt),
            "user_message": options.get("user_message"),
            "history": options.get("history", []),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": tool.get_input_schema().model_json_schema(),
                }
                for tool in options.get("tools", [])
            ],
            "stream_callbacks": {
                name: callable(options.get(name))
                for name in ("handle_text", "handle_text_delta", "handle_reasoning_delta")
            },
        }
        with self.lock:
            self.inputs.append(record)
            append_jsonl(self.directory / "model_inputs.jsonl", record)

        def wrap(tool):
            original = tool.func

            def invoke(*args, **kwargs):
                row = {
                    "name": tool.name, "arguments": kwargs,
                    "started_seconds": time.monotonic() - self.began[0],
                }
                with self.lock:
                    self.tools.append(row)
                try:
                    result = original(*args, **kwargs)
                    row["result_text"] = result[0] if isinstance(result, tuple) else str(result)
                    return result
                except Exception as error:
                    row["error"] = {"type": type(error).__name__, "message": _redact(str(error))}
                    raise
                finally:
                    row["ended_seconds"] = time.monotonic() - self.began[0]
                    with self.lock:
                        append_jsonl(self.directory / "tools.jsonl", row)

            return tool.model_copy(update={"func": invoke})

        options = dict(options)
        options["tools"] = [wrap(tool) for tool in options.get("tools", [])]
        return self.delegate.run_agent(prompt, **options)


class RecordingHook:
    """Record the actual hook result and elapsed time; never change its output."""

    def __init__(self, delegate, directory: Path, began: list[float]) -> None:
        self.delegate, self.directory, self.began = delegate, directory, began
        self.calls: list[dict] = []
        self.lock = Lock()

    def __call__(self, inputs, control):
        if self.delegate is None:
            return inputs["user_message"]
        started = time.monotonic()
        row = {
            "started_seconds": started - self.began[0],
            "input": {
                "user_message": inputs.get("user_message"),
                "history": inputs.get("history", []),
            },
        }
        try:
            output = self.delegate(inputs, control)
            row["output"] = output
            row["changed"] = output != inputs.get("user_message")
            return output
        except Exception as error:
            row["error"] = {"type": type(error).__name__, "message": _redact(str(error))}
            raise
        finally:
            row["elapsed_seconds"] = time.monotonic() - started
            with self.lock:
                self.calls.append(row)
                append_jsonl(self.directory / "hook.jsonl", row)


def _event_content(event: dict) -> str | None:
    if isinstance(event.get("content"), str):
        return event["content"]
    assistant = event.get("assistantMessage")
    if isinstance(assistant, dict) and isinstance(assistant.get("content"), str):
        return assistant["content"]
    return None


def _event_row(event: dict, received_seconds: float) -> dict:
    row = {
        "received_seconds": round(received_seconds, 6),
        "type": event.get("type"),
        "sequence": event.get("sequence"),
        "occurredAt": event.get("occurredAt"),
    }
    content = _event_content(event)
    if content is not None:
        row["content_chars"] = len(content)
    if event.get("type") == "answer.delta":
        row["delta_chars"] = len(event.get("delta") or "")
    if event.get("callId"):
        row["callId"] = event["callId"]
    if isinstance(event.get("textStream"), dict):
        row["textStream"] = dict(event["textStream"])
    assistant = event.get("assistantMessage")
    if isinstance(assistant, dict):
        row["assistant_status"] = assistant.get("status")
        if assistant.get("modelRetry"):
            row["modelRetry"] = assistant["modelRetry"]
        error = assistant.get("lastError")
        if isinstance(error, dict):
            row["lastError"] = {"code": error.get("code")}
    if event.get("modelRetry"):
        row["modelRetry"] = event["modelRetry"]
    if isinstance(event.get("error"), dict):
        row["error"] = {"code": event["error"].get("code")}
    return row


def _consume_execution(execution, began: list[float], directory: Path) -> dict:
    deadline = began[0] + TURN_TIMEOUT_SECONDS
    cancel_deadline = None
    ftt_seconds = None
    first_text_event = None
    before_model_completed = None
    text_stream_flags: list[bool] = []
    retries: list[dict] = []
    timed_out = False
    event_count = 0

    while True:
        try:
            event = execution.next_event(timeout=1)
        except Empty:
            now = time.monotonic()
            if cancel_deadline is not None and now >= cancel_deadline:
                break
            if cancel_deadline is None and now >= deadline:
                timed_out = True
                execution.cancellation.set()
                cancel_deadline = now + 15
            continue
        if event is None:
            break
        event_count += 1
        received = time.monotonic() - began[0]
        row = _event_row(event, received)
        append_jsonl(directory / "events.jsonl", row)
        if row.get("modelRetry"):
            retries.append({"received_seconds": row["received_seconds"], "modelRetry": row["modelRetry"]})
        stream = row.get("textStream")
        if isinstance(stream, dict) and isinstance(stream.get("beforeModelCompleted"), bool):
            text_stream_flags.append(stream["beforeModelCompleted"])
        content = _event_content(event)
        if (
            ftt_seconds is None
            and event.get("type") in {"answer.delta", "answer.snapshot"}
            and isinstance(content, str) and content.strip()
        ):
            ftt_seconds = received
            first_text_event = row
            if isinstance(stream, dict) and isinstance(stream.get("beforeModelCompleted"), bool):
                before_model_completed = stream["beforeModelCompleted"]
        if cancel_deadline is None and time.monotonic() >= deadline:
            timed_out = True
            execution.cancellation.set()
            cancel_deadline = time.monotonic() + 15

    terminal = None
    tt_seconds = None
    error = None
    try:
        if execution.future is not None:
            terminal = execution.future.result(timeout=15)
            tt_seconds = time.monotonic() - began[0]
    except FutureTimeoutError as exc:
        error = error_chain(exc)
        timed_out = True
    except Exception as exc:
        error = error_chain(exc)
    return {
        "ftt_seconds": ftt_seconds,
        "first_text_event": first_text_event,
        "before_model_completed": before_model_completed,
        "text_stream_flags": text_stream_flags,
        "tt_seconds": tt_seconds,
        "timed_out": timed_out,
        "event_count": event_count,
        "retries": retries,
        "terminal": terminal,
        "error": error,
    }


def _usage_totals(rows: list[dict]) -> dict:
    fields = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
    totals = {name: 0 for name in fields}
    missing_by_field = {name: 0 for name in fields}
    usage_missing = 0
    completed = failed = 0
    by_operation: dict[str, int] = {}
    by_model: dict[str, int] = {}
    for row in rows:
        if row.get("status") == "completed":
            completed += 1
        else:
            failed += 1
        by_operation[row.get("operation")] = by_operation.get(row.get("operation"), 0) + 1
        by_model[row.get("model")] = by_model.get(row.get("model"), 0) + 1
        usage = row.get("usage")
        if not isinstance(usage, dict):
            usage_missing += 1
            for name in fields:
                missing_by_field[name] += 1
            continue
        for name in fields:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[name] += value
            else:
                missing_by_field[name] += 1
    for name in fields:
        if rows and missing_by_field[name] == len(rows):
            totals[name] = None
    return {
        "calls": len(rows), "completed": completed, "failed": failed,
        "usage_missing": usage_missing, **totals,
        "missing_by_field": missing_by_field,
        "by_operation": by_operation, "by_model": by_model,
    }


def _insert_history(agent, conversation_id: str, history: list[dict], series_id: str) -> None:
    if not history:
        return
    from server.model.agent import AgentConversation, AgentMessage

    with agent._repository._session_factory.begin() as session:
        conversation = session.get(AgentConversation, conversation_id)
        if conversation is None:
            raise RuntimeError("new conversation disappeared before history insertion")
        for sequence, item in enumerate(history):
            session.add(AgentMessage(
                id=str(uuid4()), conversation_id=conversation_id, sequence_no=sequence,
                role=item["role"].upper(), status="COMPLETED", content=item["content"],
                series_ids=[series_id], version=1,
            ))
        conversation.next_message_sequence = len(history)


def _clear_artifacts(directory: Path) -> None:
    for name in ARTIFACTS:
        (directory / name).unlink(missing_ok=True)


def _assert_protocol(protocol: dict, args, step: dict, config_path: Path, case: dict) -> None:
    if source_hashes() != protocol["source_sha256"]:
        raise RuntimeError("source changed after protocol freeze")
    if _sha(args.cases.read_bytes()) != protocol["cases_sha256"]:
        raise RuntimeError("cases file changed after protocol freeze")
    if _sha(config_path.read_bytes()) != protocol["configs"][step["arm"]]["sha256"]:
        raise RuntimeError("arm config changed after protocol freeze")
    if protocol["case_sha256"].get(case["id"]) != _case_hash(case):
        raise RuntimeError("case changed after protocol freeze")
    if protocol["data_environment"] != {name: os.environ.get(name) for name in DATA_ENV_FIELDS}:
        raise RuntimeError("data environment changed after protocol freeze")


def _observed_from_terminal(terminal) -> tuple[dict, dict | None]:
    assistant = terminal.assistant_message
    final = {
        "user_message": asdict(terminal.user_message),
        "assistant_message": asdict(assistant),
    }
    error = None
    if assistant.last_error is not None:
        error = {"code": assistant.last_error.code, "message": _redact(assistant.last_error.message)}
    observed = {
        "status": str(assistant.status),
        "conversation_id": assistant.conversation_id,
        "message_id": assistant.message_id,
        "content": assistant.content or "",
        "model_name": assistant.model_name,
        "error": error,
        "timing": asdict(assistant.timing) if assistant.timing is not None else None,
        "model_retry": assistant.model_retry,
        "search_result": assistant.search_result,
        "render_blocks": list(assistant.render_blocks) if assistant.render_blocks else None,
    }
    return observed, final


def run_worker(args, index: int) -> None:
    protocol = json.loads((args.output / "protocol.json").read_text(encoding="utf-8"))
    if not 0 <= index < len(protocol["schedule"]):
        raise ValueError(f"--only-index must be between 0 and {len(protocol['schedule']) - 1}")
    step = protocol["schedule"][index]
    directory = case_directory(args.output, step)
    if (directory / "result.json").exists():
        print(json.dumps({"event": "skipped", **step}), flush=True)
        return
    directory.mkdir(parents=True, exist_ok=True)
    _clear_artifacts(directory)
    logging.basicConfig(
        level=logging.INFO, filename=directory / "runtime.log",
        filemode="w", force=True,
    )

    config_path = (args.configs_dir / ARM_FILES[step["arm"]]).resolve()
    result = {
        **step,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": {"name": config_path.name, "sha256": None},
    }
    began = [time.monotonic()]
    application = None
    try:
        document = load_cases(args.cases)
        case = next(item for item in document["cases"] if item["id"] == step["case_id"])
        result["tenant_id"] = document["tenant_id"]
        result["series_id"] = document["series_id"]
        result["config"]["sha256"] = _sha(config_path.read_bytes())
        _prepare_env(config_path)
        _assert_protocol(protocol, args, step, config_path, case)
        validate_wiring(step["arm"], config_info(config_path))

        from evals.latency.chat_latency import _build_services
        from gateway.usage import capture_model_usage

        application = _build_services()
        agent = application.services.agent
        runtime = agent._runtime
        result["runtime_config"] = asdict(agent._config)
        result["runtime_model"] = {
            "model_name": getattr(runtime, "model_name", None),
            "provider_name": getattr(getattr(runtime, "_model", None), "provider_name", None),
        }
        context_policy = getattr(runtime._model, "_context_window_policy", None)
        result["runtime_model"]["context_policy"] = (
            asdict(context_policy) if context_policy is not None else None
        )

        usage_rows: list[dict] = []
        usage_seen: set[str] = set()
        usage_lock = Lock()

        def observe(call):
            row = {
                name: getattr(call, name)
                for name in (
                    "call_id", "provider", "model", "operation", "started_at",
                    "ended_at", "status", "granularity", "usage_source",
                )
            }
            row["request_id"] = getattr(call, "request_id", None)
            row["context"] = dict(call.context)
            row["usage"] = asdict(call.usage) if call.usage is not None else None
            with usage_lock:
                if row["call_id"] in usage_seen:
                    return
                usage_seen.add(row["call_id"])
                usage_rows.append(row)
                append_jsonl(directory / "usage.jsonl", row)

        recorder = hook_recorder = None

        def instrument(actual):
            nonlocal recorder, hook_recorder
            recorder = RecordingModel(actual._model, directory, began)
            actual._model = recorder
            hook_recorder = RecordingHook(actual._query_hook, directory, began)
            actual._query_hook = hook_recorder
            policy = getattr(actual._model, "_context_window_policy", None)
            result["runtime_model"] = {
                "model_name": actual.model_name,
                "provider_name": getattr(actual._model, "provider_name", None),
                "supports_text_deltas": getattr(actual._model, "supports_text_deltas", False),
                "context_policy": asdict(policy) if policy is not None else None,
            }
            original_kick = actual.kick

            def kick(inputs, **kwargs):
                save(directory / "active.json", {
                    "conversation_id": inputs["conversation_id"],
                    "message_id": inputs["assistant_message_id"],
                    "tenant_id": inputs["tenant_id"],
                })
                try:
                    with capture_model_usage(observe, combine=True):
                        return original_kick(inputs, **kwargs)
                except Exception as error:
                    save(directory / "failure.json", error_chain(error))
                    raise

            actual.kick = kick

        instrument(runtime)
        if agent._configured_runtime is not None:
            original_configured_runtime = agent._configured_runtime

            @contextmanager
            def configured_runtime(selection, config):
                with original_configured_runtime(selection, config) as actual:
                    instrument(actual)
                    yield actual

            agent._configured_runtime = configured_runtime

        conversation_id = agent.create_conversation(
            tenant_id=document["tenant_id"],
            title=f"[{EVAL_PREFIX}] {step['arm']} r{step['round']} {case['id']}",
            idempotency_key=f"{EVAL_PREFIX}-{uuid4()}",
        ).conversation_id
        _insert_history(agent, conversation_id, case["state"].get("recent_messages", []), document["series_id"])
        payload = {
            "tenantId": document["tenant_id"],
            "content": case["state"]["query"],
            "seriesIds": [document["series_id"]],
        }
        save(directory / "input.json", {
            "request": payload,
            "history": case["state"].get("recent_messages", []),
            "conversation_id": conversation_id,
        })
        result["conversation_id"] = conversation_id
        began[0] = time.monotonic()
        started_monotonic = began[0]
        result["started_monotonic"] = started_monotonic

        try:
            execution = agent.start_message(
                conversation_id, tenant_id=document["tenant_id"],
                content=payload["content"], idempotency_key=f"{EVAL_PREFIX}-{uuid4()}",
                series_ids=payload["seriesIds"], stream=True,
            )
            save(directory / "active.json", {
                "conversation_id": conversation_id,
                "message_id": execution.initial.assistant_message.message_id,
                "tenant_id": document["tenant_id"],
            })
            result["message_id"] = execution.initial.assistant_message.message_id
            outcome = _consume_execution(execution, began, directory)
        except Exception as error:
            outcome = {
                "ftt_seconds": None, "first_text_event": None,
                "before_model_completed": None, "text_stream_flags": [],
                "tt_seconds": None, "timed_out": False, "event_count": 0,
                "retries": [], "terminal": None, "error": error_chain(error),
            }
            save(directory / "harness_failure.json", outcome["error"])

        result.update({
            "wall_seconds": round(time.monotonic() - began[0], 6),
            "ftt_seconds": outcome["ftt_seconds"],
            "first_text_event": outcome["first_text_event"],
            "text_stream_before_model_completed": outcome["before_model_completed"],
            "text_stream_flags": outcome["text_stream_flags"],
            "tt_seconds": outcome["tt_seconds"],
            "timeout": outcome["timed_out"],
            "event_count": outcome["event_count"],
            "retries": outcome["retries"],
            "tool_calls": [
                {
                    name: row.get(name)
                    for name in ("name", "arguments", "started_seconds", "ended_seconds")
                } | {
                    "result_chars": len(row.get("result_text") or ""),
                    **({"error": row["error"]} if row.get("error") else {}),
                }
                for row in recorder.tools
            ],
            "model_inputs": [
                {
                    "started_seconds": row["started_seconds"],
                    "prompt_sha256": row["prompt_sha256"],
                    "prompt_chars": len(row["prompt"]),
                    "tool_names": [tool["name"] for tool in row["tools"]],
                    "stream_callbacks": row["stream_callbacks"],
                }
                for row in recorder.inputs
            ],
            "hook": {
                "configured": hook_recorder.delegate is not None,
                "calls": hook_recorder.calls,
                "fail_open_suspected": any(
                    not call.get("changed", True) for call in hook_recorder.calls
                ),
            },
            "usage": {"rows": len(usage_rows), "totals": _usage_totals(usage_rows)},
            "api_errors": [
                {
                    name: row.get(name) for name in (
                        "call_id", "provider", "model", "operation", "started_at",
                        "ended_at", "status", "context",
                    )
                }
                for row in usage_rows if row.get("status") != "completed"
            ],
        })

        terminal = outcome["terminal"]
        if terminal is not None and (
            not recorder.inputs or not usage_rows
        ):
            raise RuntimeError("completed turn missed active runtime inputs or provider usage")
        if terminal is not None:
            observed, final = _observed_from_terminal(terminal)
            if outcome["timed_out"]:
                observed["timeout"] = True
            observed["conversation_id"] = conversation_id
            result["observed"] = observed
            result["message_id"] = observed["message_id"]
            save(directory / "final.json", final)
            try:
                stored = agent.get_message(
                    conversation_id, tenant_id=document["tenant_id"],
                    message_id=observed["message_id"],
                ).assistant_message
                result["persistence"] = {
                    "content_matches": (stored.content or "") == observed["content"],
                    "status": str(stored.status),
                    "message_id": stored.message_id,
                }
            except Exception as error:
                result["persistence"] = {"error": error_chain(error)}
        else:
            result["observed"] = {
                "status": "TIMEOUT" if outcome["timed_out"] else "HARNESS_ERROR",
                "conversation_id": conversation_id,
                "message_id": result.get("message_id"),
                "error": outcome["error"],
                "timeout": outcome["timed_out"],
            }
    except Exception as error:
        result["observed"] = {
            "status": "WORKER_ERROR",
            "error": error_chain(error),
        }
        result["error"] = error_chain(error)
        save(directory / "harness_failure.json", result["error"])
    finally:
        save(directory / "result.json", result)
        if application is not None:
            try:
                application.close()
            except Exception as error:
                result["close_error"] = error_chain(error)
                save(directory / "result.json", result)
    print(json.dumps({
        key: result.get(key) for key in (
            "index", "arm", "case_id", "round", "ftt_seconds", "tt_seconds",
            "wall_seconds", "timeout",
        )
    } | {"status": result.get("observed", {}).get("status")}, ensure_ascii=False), flush=True)


def cancel_owned(args, index: int) -> None:
    protocol = json.loads((args.output / "protocol.json").read_text(encoding="utf-8"))
    if not 0 <= index < len(protocol["schedule"]):
        raise ValueError(f"--cancel-index must be between 0 and {len(protocol['schedule']) - 1}")
    step = protocol["schedule"][index]
    directory = case_directory(args.output, step)
    active_path = directory / "active.json"
    if not active_path.exists():
        save(directory / "cancellation.json", {"status": "NO_ACTIVE_CONVERSATION"})
        return
    active = json.loads(active_path.read_text(encoding="utf-8"))
    config_path = (args.configs_dir / ARM_FILES[step["arm"]]).resolve()
    _prepare_env(config_path)
    from evals.latency.chat_latency import _build_services

    application = _build_services()
    try:
        pair = application.services.agent.cancel_message(
            active["conversation_id"], tenant_id=active.get("tenant_id", protocol["tenant_id"]),
            message_id=active["message_id"],
        )
        save(directory / "cancellation.json", {
            **active, "status": str(pair.assistant_message.status),
            "cancelled_at": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        application.close()


def _freeze_protocol(args, document: dict, schedule: list[dict], configs: dict) -> dict:
    for arm, info in configs.items():
        validate_wiring(arm, info)
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Real local AgentChatService.start_message(stream=True) with production Supervisor, "
            "same prompt/tools/history/series data; only the configured query-intent path differs."
        ),
        "git_head": git_head(),
        "source_sha256": source_hashes(),
        "cases_path": str(args.cases.resolve()),
        "cases_sha256": _sha(args.cases.read_bytes()),
        "case_sha256": {case["id"]: _case_hash(case) for case in document["cases"]},
        "configs_dir": str(args.configs_dir.resolve()),
        "configs": configs,
        "arms": list(ARMS),
        "tenant_id": document["tenant_id"],
        "series_id": document["series_id"],
        "repeat_case_ids": document.get("repeat_case_ids", []),
        "schedule": schedule,
        "turn_timeout_seconds": TURN_TIMEOUT_SECONDS,
        "child_watchdog_seconds": CHILD_WATCHDOG_SECONDS,
        "concurrency": 1,
        "data_environment": {name: os.environ.get(name) for name in DATA_ENV_FIELDS},
    }


def _live(args, document: dict, schedule: list[dict]) -> None:
    args.output.mkdir(parents=True, exist_ok=True)
    configs = {
        arm: config_info((args.configs_dir / ARM_FILES[arm]).resolve())
        for arm in ARMS
    }
    protocol_path = args.output / "protocol.json"
    if protocol_path.exists():
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol["schedule"] != schedule:
            raise RuntimeError("frozen schedule differs from the current cases/configs")
        if protocol["source_sha256"] != source_hashes():
            raise RuntimeError("source changed after protocol freeze")
        if protocol["cases_sha256"] != _sha(args.cases.read_bytes()):
            raise RuntimeError("cases file changed after protocol freeze")
        for arm, info in configs.items():
            if info["sha256"] != protocol["configs"][arm]["sha256"]:
                raise RuntimeError(f"{arm} config changed after protocol freeze")
            validate_wiring(arm, info)
    else:
        protocol = _freeze_protocol(args, document, schedule, configs)
        save(protocol_path, protocol)

    infrastructure_failures = 0
    for step in schedule:
        if (case_directory(args.output, step) / "result.json").exists():
            continue
        if source_hashes() != protocol["source_sha256"]:
            raise RuntimeError("source changed during comparison; stop before another model call")
        print(json.dumps({"event": "started", **step}), flush=True)
        config_path = (args.configs_dir / ARM_FILES[step["arm"]]).resolve()
        env = os.environ.copy()
        env["SEEKINO_MODEL_CONFIG"] = str(config_path)
        for name in (
            "SEEKINO_AGENT_CHAT_MODEL", "SEEKINO_DEEPSEEK_CHAT_MODEL",
            "SEEKINO_AGENT_QUERY_MODEL", "SEEKINO_AGENT_FALLBACK_MODEL",
            "SEEKINO_MODEL_TRACE_DIR", "SEEKINO_CHAT_TRACE_DIR",
        ):
            env.pop(name, None)
        command = [
            sys.executable, "-m", "evals.benchmark.query_intent_compare",
            "--cases", str(args.cases), "--configs-dir", str(args.configs_dir),
            "--output", str(args.output), "--only-index", str(step["index"]),
        ]
        with (args.output / "workers.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"event": "spawn", **step}, ensure_ascii=False) + "\n")
            log.flush()
            child = subprocess.Popen(
                command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        timed_out = False
        try:
            child.wait(timeout=CHILD_WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
        directory = case_directory(args.output, step)
        result_path = directory / "result.json"
        missing_result = not result_path.exists()
        if missing_result:
            save(result_path, {
                **step,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "wall_seconds": CHILD_WATCHDOG_SECONDS if timed_out else None,
                "wall_time_basis": "External watchdog, not successful turn latency",
                "timeout": timed_out,
                "observed": {
                    "status": "WATCHDOG_TIMEOUT" if timed_out else "WORKER_ERROR",
                    "worker_exit_code": child.returncode,
                },
            })
        if timed_out or missing_result:
            try:
                cancelled = subprocess.run(
                    [
                        sys.executable, "-m", "evals.benchmark.query_intent_compare",
                        "--cases", str(args.cases), "--configs-dir", str(args.configs_dir),
                        "--output", str(args.output), "--cancel-index", str(step["index"]),
                    ],
                    cwd=ROOT, env=env, timeout=60, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if cancelled.returncode != 0 and not (directory / "cancellation.json").exists():
                    save(directory / "cancellation.json", {
                        "status": "CANCEL_FAILED", "worker_exit_code": cancelled.returncode,
                    })
            except Exception as error:
                save(directory / "cancellation.json", {"status": "CANCEL_FAILED", "error": error_chain(error)})
        row = json.loads(result_path.read_text(encoding="utf-8"))
        print(json.dumps({
            "event": "finished", **step,
            "status": row.get("observed", {}).get("status"),
            "seconds": row.get("wall_seconds"),
        }, ensure_ascii=False), flush=True)
        infrastructure_failures = infrastructure_failures + 1 if (
            row.get("observed", {}).get("status") in {"WORKER_ERROR", "HARNESS_ERROR"}
        ) else 0
        if infrastructure_failures >= 3:
            raise RuntimeError("three consecutive infrastructure failures; inspect retained runs before resuming")

    with (args.output / "observations.jsonl").open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "run", **protocol}, ensure_ascii=False, default=str) + "\n")
        for step in schedule:
            path = case_directory(args.output, step) / "result.json"
            if path.exists():
                stream.write(path.read_text(encoding="utf-8").strip() + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--configs-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="offline schedule and hashes (default)")
    parser.add_argument("--live", action="store_true", help="run real local Chat turns")
    parser.add_argument("--only-index", type=int, help="run one frozen schedule step")
    parser.add_argument("--cancel-index", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.cases = args.cases.resolve()
    args.configs_dir = args.configs_dir.resolve()
    args.output = args.output.resolve()
    if args.plan and args.live:
        parser.error("--plan and --live are mutually exclusive")
    if args.only_index is not None and args.cancel_index is not None:
        parser.error("only one of --only-index and --cancel-index is allowed")

    if args.cancel_index is not None:
        cancel_owned(args, args.cancel_index)
        return
    if args.only_index is not None:
        protocol_path = args.output / "protocol.json"
        if not protocol_path.exists():
            args.output.mkdir(parents=True, exist_ok=True)
            document = load_cases(args.cases)
            schedule = build_schedule(document)
            configs = {
                arm: config_info((args.configs_dir / ARM_FILES[arm]).resolve())
                for arm in ARMS
            }
            save(protocol_path, _freeze_protocol(args, document, schedule, configs))
        run_worker(args, args.only_index)
        return

    document = load_cases(args.cases)
    schedule = build_schedule(document)
    if not args.live:
        configs = {
            arm: config_info((args.configs_dir / ARM_FILES[arm]).resolve())
            for arm in ARMS
        }
        print(json.dumps({
            "api_called": False,
            "services_started": False,
            "planned_turns": len(schedule),
            "arms": list(ARMS),
            "repeat_case_ids": document.get("repeat_case_ids", []),
            "schedule": schedule,
            "cases_sha256": _sha(args.cases.read_bytes()),
            "case_sha256": {case["id"]: _case_hash(case) for case in document["cases"]},
            "git_head": git_head(),
            "source_sha256": source_hashes(),
            "configs": configs,
        }, ensure_ascii=False, indent=2, default=str))
        return
    _live(args, document, schedule)


if __name__ == "__main__":
    main()
