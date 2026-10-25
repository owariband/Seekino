"""Replay a frozen chunk through LangChain ReAct, Parse Deep Agents, or Codex.

Run with uv run --no-sync --env-file .env.local python -m evals.parse_harness --help.
Only local experiment artifacts are written; no task, database or service is changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, replace
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import as_langchain_tools
from agent.video_parse_v2.prompts import build_canvas_understanding_prompt
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseCanvasWrite, ParseContextCanvas
from engine.parsev2.validation import validate_result_time_range
from gateway.agent_model import deepagents_multimodal as harness
from gateway.agent_model.context import ContextWindowController
from gateway.codex.client import CodexSession
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.usage import capture_model_usage, model_call_context


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arm", choices=("react", "deepagents", "codex"), required=True)
    p.add_argument("--inputs", type=Path, required=True, help="Directory containing inputs.json and frames.json")
    p.add_argument("--config", type=Path, required=True, help="Gateway config with a Kimi vision binding")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke", action="store_true", help="One image and one read; excluded from quality results")
    p.add_argument("--native-no-schema", action="store_true", help="Codex diagnostic: validate final JSON after tool use")
    p.add_argument("--codex-gpt-baseline", action="store_true", help="Use the existing default GPT vision binding")
    p.add_argument("--context-window", type=int, help="Explicit experimental token budget; compact at 75%%, target 50%%")
    args = p.parse_args()
    if (args.native_no_schema or args.codex_gpt_baseline) and args.arm != "codex":
        p.error("Native variants require --arm codex")
    evidence = json.loads((args.inputs / "inputs.json").read_text())
    frames = json.loads((args.inputs / "frames.json").read_text())
    for row in frames:
        assert hashlib.sha256((args.inputs / row["file"]).read_bytes()).hexdigest() == row["sha256"]
    config = load_config(args.config)
    binding = config.normalized_models["vision"].model_copy(update={"fallback": None})
    assert binding.model == "k3.1-pro" and binding.reasoning_effort in ("low", "high", "max")
    source = config.model_sources[binding.source]
    if args.codex_gpt_baseline:
        binding = load_config().normalized_models["vision"].model_copy(update={"fallback": None})
    if args.context_window:
        binding = binding.model_copy(update={"context_window_tokens": args.context_window,
            "auto_compact_tokens": args.context_window*3//4, "compact_target_tokens": args.context_window//2})
    canvas = ParseContextCanvas(scope="chunk:ep04-000", stage="understanding", members={"ep04-000": {
        "sequence_no": 0, "version": 0, "bounds": (0, 60000), "content": {"persons": [], "timeline": []},
    }}, background=evidence["background"], evidence={"media": {
        "source_media_ref": "frozen:ep04", "video_id": "ep04", "start_ms": 0, "end_ms": 60000}})
    canvas.id = "frozen-ep04-chunk0-harness-comparison"
    canvas.register_evidence("ep04-000", {"asr": evidence["asr"], "ocr": evidence["ocr"]})
    prompt = build_canvas_understanding_prompt(SimpleNamespace(chunk_parse_task_id="ep04-000",
        allowed_start_seconds=0, allowed_end_seconds=60000), canvas=canvas)
    prompt += ("\nHost visual input range: [0, 60000) ms. Writable chunk range: [0, 60000) ms. "
               "ASR/OCR timestamps remain absolute source-video milliseconds.\nOrdered frame timestamps: "
               + str([row["timestamp_ms"] for row in frames]))
    if args.smoke:
        frames = frames[:1]
        prompt = ('Read get_context_canvas(collection="evidence", reference="ep04-000", section="asr"). '
                  'Then complete with {"complete":true}. This is a connectivity check, do not write events.')
    tools = as_langchain_tools(build_context_canvas_tools(canvas, role="understanding",
        read_collections=("members", "events", "evidence", "background", "compact"),
        write_args_schema=ParseCanvasWrite, maximum_result_bytes=32000,
        maximum_read_chars=12000, maximum_catalog_items=100))
    protocol = {"arm": args.arm, "smoke": args.smoke, "binding": binding.model_dump(mode="json"), "pid": os.getpid(),
        "native_no_schema": args.native_no_schema, "codex_gpt_baseline": args.codex_gpt_baseline,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_directory": str(args.inputs.resolve()), "frames": frames,
        "evidence_sha256": hashlib.sha256((args.inputs / "inputs.json").read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "bounds_ms": [0, 60000],
        "packages": {x: importlib.metadata.version(x) for x in ("langchain", "deepagents", "langgraph", "openai-codex")},
        "source_sha256": {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for root in
            ("gateway", "agent/common", "agent/utils", "agent/video_parse_v2", "engine/parsev2") for f in Path(root).rglob("*.py")}}
    if args.dry_run:
        print(json.dumps({k: protocol[k] for k in ("arm", "binding", "prompt_sha256", "evidence_sha256", "packages")}))
        return
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / "protocol.json", protocol)
    (args.out / "prompt.txt").write_text(prompt)
    save(args.out / "business-tools.json", [{"name": t.name, "description": t.description,
        "schema": t.get_input_schema().model_json_schema()} for t in tools])
    began, lock = time.monotonic(), Lock()
    def record(kind, **row):
        with lock, (args.out / "trace.jsonl").open("a") as f:
            f.write(json.dumps({"kind": kind, "seconds": time.monotonic()-began, **row}, ensure_ascii=False, default=str)+"\n")
        print(json.dumps({"kind": kind, "seconds": round(time.monotonic()-began, 2), **{k: v for k, v in row.items() if k in ("name", "status", "calls", "compacted")}}), flush=True)
    original_invoke, original_prepare, original_generate = BaseTool.invoke, ContextWindowController.prepare, harness._ParseChatModel._generate
    def invoke(tool, input, *a, **kw):
        result = original_invoke(tool, input, *a, **kw)
        record("tool", name=tool.name, input=input, output=getattr(result, "content", result), status=getattr(result, "status", None))
        return result
    def prepare(controller, *a, **kw):
        update = original_prepare(controller, *a, **kw)
        record("context", **{k: v for k, v in asdict(update).items() if k != "messages"})
        return update
    def generate(model, messages, *a, **kw):
        record("model_start", calls=model._calls+1, tools=[getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in kw.get("tools", [])],
               system_messages=[m.content for m in messages if m.type == "system"])
        result = original_generate(model, messages, *a, **kw)
        record("model_end", calls=model._calls, tool_calls=result.generations[0].message.tool_calls)
        return result
    def react(**kw):
        kw.pop("backend")
        kw["middleware"] = [m for m in kw["middleware"] if isinstance(m, harness._ParseControlMiddleware)]
        return create_agent(**kw)
    class Session(CodexSession):
        def __init__(self, **kw):
            super().__init__(**kw)
            if args.codex_gpt_baseline:
                return
            catalog = {"slug": binding.model, "display_name": binding.model, "description": "Kimi experiment",
                "default_reasoning_level": binding.reasoning_effort, "supported_reasoning_levels": [
                    {"effort": e, "description": e} for e in ("low", "high", "max")],
                "shell_type": "shell_command", "visibility": "list", "supported_in_api": True, "priority": 0,
                "base_instructions": "", "supports_reasoning_summaries": True, "default_reasoning_summary": "none",
                "support_verbosity": False, "truncation_policy": {"mode": "bytes", "limit": 32000},
                "context_window": binding.context_window_tokens, "max_context_window": 1048576,
                "effective_context_window_percent": 95, "supports_parallel_tool_calls": True,
                "experimental_supported_tools": [], "input_modalities": ["text", "image"]}
            save(args.out / "codex-models.json", {"models": [catalog]})
            overrides = (f'model_catalog_json={json.dumps(str((args.out / "codex-models.json").resolve()))}',
                'model_provider="kimi_experiment"', 'model_providers.kimi_experiment.name="Kimi"',
                f'model_providers.kimi_experiment.base_url={json.dumps(source.base_url)}',
                f'model_providers.kimi_experiment.env_key={json.dumps(source.api_key_env)}',
                'model_providers.kimi_experiment.wire_api="responses"',
                'model_providers.kimi_experiment.requires_openai_auth=false',
                'model_providers.kimi_experiment.supports_websockets=false',
                'model_providers.kimi_experiment.request_max_retries=0', 'model_providers.kimi_experiment.stream_max_retries=0',
                'web_search="disabled"', f'model_auto_compact_token_limit={binding.auto_compact_tokens}')
            self._config = replace(self._config, config_overrides=self._config.config_overrides + overrides)
            save(args.out / "codex-overrides.json", list(self._config.config_overrides))
        def run_turn(self, prompt, *, output_schema=None):
            result = super().run_turn(prompt, output_schema=None if args.native_no_schema else output_schema)
            record("native_result", final=result.final_response, tool_count=self.tool_call_count)
            return result
        def _handle_notification(self, event):
            name = type(event.payload).__name__
            if name in ("ErrorNotification", "TurnCompletedNotification", "ThreadTokenUsageUpdatedNotification"):
                record("native", name=name, payload=event.payload.model_dump(mode="json"))
            super()._handle_notification(event)
    if args.arm == "codex":
        config.model_sources["codex_local"] = load_config().model_sources["codex_local"]
        binding = binding.model_copy(update={"source": "codex_local"})
    gateway = GatewayClient(config)
    model = gateway.create_generative_model(binding if args.arm == "codex" else
        binding.model_copy(update={"reasoning_effort": "none"}), mcp=False)
    if args.arm == "codex":
        model._delegate._delegate._session_factory = Session
    else:
        # The existing vision factory rejects effort; bind it explicitly on the experimental transport.
        adapter = model._delegate._delegate
        client = adapter._new_client()
        original_create = client.chat.completions.create
        def kimi_create(**request):
            request.update(reasoning_effort=binding.reasoning_effort, temperature=1)
            response = original_create(**request)
            record("transport", model=response.model, effort=request["reasoning_effort"])
            return response
        client.chat.completions.create = kimi_create
        adapter._new_client = lambda: client
    summary = {"status": "FAILED", "arm": args.arm, "smoke": args.smoke}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(BaseTool, "invoke", invoke))
            stack.enter_context(patch.object(ContextWindowController, "prepare", prepare))
            stack.enter_context(patch.object(harness._ParseChatModel, "_generate", generate))
            if args.arm == "react":
                stack.enter_context(patch.object(harness, "create_deep_agent", react))
            stack.enter_context(capture_model_usage(lambda c: record("usage", **{
                k: getattr(c, k) for k in ("provider", "model", "status", "granularity", "usage_source")},
                usage=asdict(c.usage) if c.usage else None)))
            stack.enter_context(model_call_context({"experiment": "parse-harness", "arm": args.arm}))
            receipt = model.generate([*(ImageInput(path=args.inputs/r["file"]) for r in frames), TextInput(prompt)],
                schema_name="video_parse_v2_chunk", result_type=CanvasCompletion, tools=tools,
                max_model_calls=None, context_checkpoint=canvas.checkpoint, tool_harness="deepagents")
            CanvasCompletion.model_validate(receipt)
        result = canvas.result("ep04-000")
        validate_result_time_range(result, start_seconds=0, end_seconds=60000)
        save(args.out / "result.json", result.model_dump(mode="json"))
        summary.update(status="COMPLETE", schema_valid=True, time_range_valid=True,
            event_counts=dict(Counter(e.event_type for e in result.timeline)))
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        summary["elapsed_seconds"] = time.monotonic()-began
        save(args.out / "draft.json", canvas.result("ep04-000").model_dump(mode="json"))
        save(args.out / "summary.json", summary)
        gateway.close()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if summary["status"] != "COMPLETE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
