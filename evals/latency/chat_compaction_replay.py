"""Replay a frozen real Chat model boundary through the production DeepAgents middleware.

Private inputs are supplied explicitly. No SQL, ES, business tool or checkpoint
writes are performed. The normal model response is recorded before executing its
next tools, so this measures compact + one continuation, not full Chat latency.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import DEEPAGENTS_DEFAULT_SUMMARY_PROMPT, SummarizationMiddleware
from langchain_core.messages import HumanMessage, SystemMessage, convert_to_openai_messages, messages_from_dict, messages_to_dict
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.deepagents import _GatewayChatModel, _HostControlMiddleware, _count_context_tokens
from gateway.agent_model.factory import AgentModelFactory
from gateway.agent_model.tool_loop import _copy_assistant_reasoning, _function_tool_spec
from gateway.config import AgentModelConfig, ModelBinding, OpenAIModelSourceConfig
from gateway.model_diagnostics import model_diagnostic_scope
from gateway.usage import capture_model_usage


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    path.chmod(0o600)


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def unavailable(*args, **kwargs):
    raise RuntimeError("Business tools must not execute during boundary replay")


class Captured(BaseException):
    pass


def current_prompt_and_tools(row):
    from agent.chat.supervisor import SupervisorRuntime
    from agent.chat.video_search.runtime import AgentRuntime

    user = row["history"][-2]
    captured = {}

    class Model:
        supports_vision_input = True

        def run_agent(self, prompt, **options):
            captured.update(prompt=prompt, tools=options["tools"])
            raise Captured()

    video = AgentRuntime(
        SimpleNamespace(model_name="unused"), unavailable,
        event_retrieve=unavailable, video_outline=unavailable,
        series_overview=unavailable, outline_evidence=unavailable,
        frame_reader=unavailable, frame_max_height=720, maximum_frame_inspections=12,
        retrieval_channel="lexical",
    )
    series = decoded(user["series_ids"])
    extra = decoded(user["extra_context"])
    # Use the display name present in the recorded source tool output.
    overview = next(m["data"]["content"] for m in row["checkpoints"][-1]["checkpoint"]["channel_values"]["messages"]
                    if m["data"].get("name") == "get_series_overview")
    name = json.loads(overview)[0]["displayName"]
    inputs = dict(
        conversation_id="local-boundary-replay", assistant_message_id="local-replay",
        tenant_id="local-replay", series_ids=series,
        series_context=[{"seriesId": sid, "name": name, "displayName": name} for sid in series],
        user_message=user["content"],
        history=[{"role": m["role"].lower(), "content": m["content"], "status": m["status"]}
                 for m in row["history"][:-2]],
        requested_top_k=3, extra_context=extra,
    )
    runtime = SupervisorRuntime(
        video, Model(), SimpleNamespace(max_query_chars=500), max_web_results=5,
        web_timeout_seconds=12, execution_timeout_seconds=480,
        max_tool_result_bytes=16384, max_prompt_chars=200000,
        realtime_runtime=SimpleNamespace(),
    )
    try:
        runtime.kick(inputs, load_video_context=lambda: inputs["series_context"])
    except Captured:
        return captured["prompt"], captured["tools"]
    raise RuntimeError("Supervisor did not reach its model boundary")


def prepare(snapshots, output):
    from langchain_core.language_models.chat_models import BaseChatModel

    class CaptureModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "seekino_supervisor"

        def bind_tools(self, tools, **options):
            return self.bind(tools=list(tools), **options)

        def _generate(self, messages, stop=None, run_manager=None, **options):
            self.captured = (messages, options.get("tools", []))
            raise Captured()

        captured: object = None

    fixtures = []
    for index, row in enumerate(json.loads(snapshots.read_text()), 1):
        prompt, tools = current_prompt_and_tools(row)
        model = CaptureModel()
        backend = StateBackend()
        graph = create_deep_agent(
            model=model, system_prompt=prompt, tools=tools, backend=backend,
            middleware=[FilesystemMiddleware(backend=backend, tools=["read_file"]),
                        SummarizationMiddleware(model=model, backend=backend,
                                                trigger=("tokens", 100000000), keep=("tokens", 24000),
                                                token_counter=_count_context_tokens)],
        )
        try:
            graph.invoke({"messages": [HumanMessage(content="capture request structure")]})
        except Captured:
            pass
        rendered, request_tools = model.captured
        system = rendered[0]
        assert isinstance(system, SystemMessage)
        before = row["checkpoints"][0]["checkpoint"]["channel_values"]
        after = row["checkpoints"][-1]["checkpoint"]["channel_values"]
        messages = messages_from_dict(after["messages"])
        event = before.get("_summarization_event")
        if event:
            event = {**event, "summary_message": messages_from_dict([event["summary_message"]])[0]}
        user_index = next(i for i, m in enumerate(messages)
                          if m.id == row["assistant"]["id"] + ":user")
        detector = SummarizationMiddleware(model=model, backend=backend, trigger=("tokens", 48000),
                                           keep=("tokens", 24000), token_counter=_count_context_tokens)
        selected = None
        for i in range(user_index + 1, len(messages)):
            if messages[i].type != "ai":
                continue
            effective = detector._apply_event_to_messages(messages[:i], event)
            count = detector._count_tokens(effective, system, request_tools)
            if detector._should_summarize(effective, count):
                selected = effective
                break
        if selected is None:
            raise RuntimeError(f"T0{index}: original compact boundary not found")
        fixture = dict(
            case=f"T0{index}", system=prompt, messages=messages_to_dict(selected),
            tools=[_function_tool_spec(t) for t in request_tools],
            files=before.get("files", {}), boundary_index=i,
            estimated_before_tokens=count, source_assistant_id=row["assistant"]["id"],
            scope="production SDK compact + first continuation; no business tools or HTTP/SQL/SSE",
            prompt_scope="current dev Supervisor prompt with frozen scope name/playback/history; original system prompt was not logged",
        )
        encoded = json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode()
        fixture["sha256"] = hashlib.sha256(encoded).hexdigest()
        fixtures.append(fixture)
        print(json.dumps({k: fixture[k] for k in ("case", "boundary_index", "estimated_before_tokens", "sha256")}), flush=True)
    save(output, fixtures)


class BoundaryComplete(BaseException):
    pass


class BoundaryModel(_GatewayChatModel):
    _calls: list = PrivateAttr(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **options):
        summary = messages[0].type != "system" and "Context Extraction Assistant" in messages[0].text
        tools = options.get("tools", [])
        row = dict(kind="compact" if summary else "normal", estimated_input_tokens=_count_context_tokens(messages, tools=tools))
        self._calls.append(row)
        began = monotonic()
        try:
            result = super()._generate(messages, stop, run_manager, **options)
        finally:
            row["seconds"] = monotonic() - began
        row["response"] = messages_to_dict([result.generations[0].message])[0]
        if not summary:
            raise BoundaryComplete()
        return result


def run(fixtures, output, arm, only, expected_context_size=None):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    binding_options = dict(
        source="deepseek_api", model="deepseek-flash", max_output_tokens=8192,
        request_timeout_seconds=240, supports_vision_input=True, reasoning_effort="high",
        context_window_tokens=262144, context_safety_margin_tokens=4096,
        auto_compact_tokens=200000, compact_target_tokens=150000, max_input_chars=600000,
    )
    if arm == "dynamic":
        binding_options["auto_compact_ratio"] = 0.85
    binding = ModelBinding(**binding_options)
    factory = AgentModelFactory(
        AgentModelConfig(source=binding.source, model=binding.model, supports_vision_input=True,
                         max_output_tokens=8192, request_timeout_seconds=240),
        {"deepseek_api": OpenAIModelSourceConfig(provider="deepseek", profile="deepseek",
                                                 base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY")},
        binding=binding,
    )
    try:
        build_started = monotonic()
        built = factory.build_deep_agent(
            model=binding.model, reasoning_effort="high", request_timeout_seconds=240,
            max_output_tokens=8192, max_input_chars=200000,
            context_window_policy=ContextWindowPolicy(64000, 8192, 4096, 48000, 24000),
        )
        build_seconds = monotonic() - build_started
        supervisor = getattr(built, "_delegate", built)
        policy = supervisor._context_window_policy
        if arm == "dynamic" and expected_context_size is not None:
            assert policy.context_window_tokens == expected_context_size, "Dynamic arm did not resolve the expected model metadata"
            assert policy.auto_compact_tokens == min(int(expected_context_size * 0.85), policy.maximum_input_tokens - 1)
        for fixture in json.loads(fixtures.read_text()):
            if only and fixture["case"] != only:
                continue
            directory = output / fixture["case"]
            directory.mkdir(mode=0o700)
            model = BoundaryModel(model_name="seekino_supervisor:deepseek-flash")
            usage = []
            before_request = {}
            with ExitStack() as stack:
                model._adapter = supervisor._agent_model
                model._stack = stack
                deadline = monotonic() + 240
                model._remaining = lambda: max(0.01, deadline - monotonic())
                model._check = lambda: None
                model._supports_vision_input = True
                model._max_input_chars = 200000
                model._maximum_input_tokens = policy.maximum_input_tokens
                model.profile = {"image_inputs": True}
                backend = StateBackend()
                # read_file is provided by the production FilesystemMiddleware.
                tools = [StructuredTool(name=t["function"]["name"], description=t["function"]["description"],
                                        args_schema=t["function"]["parameters"], func=unavailable)
                         for t in fixture["tools"] if t["function"]["name"] != "read_file"]
                budget = min(policy.maximum_input_tokens - _count_context_tokens([HumanMessage(content=DEEPAGENTS_DEFAULT_SUMMARY_PROMPT)]),
                             (200000 - len(DEEPAGENTS_DEFAULT_SUMMARY_PROMPT)) // 3)
                summary = SummarizationMiddleware(model=model, backend=backend,
                                                  trigger=("tokens", policy.auto_compact_tokens),
                                                  keep=("tokens", policy.compact_target_tokens),
                                                  trim_tokens_to_summarize=budget, token_counter=_count_context_tokens)
                original_summary_call = summary.wrap_model_call
                def track_boundary(request, handler):
                    assert not before_request, "Boundary replay reached more than one normal model step"
                    complete = [request.system_message, *request.messages]
                    before_request["estimated_tokens"] = _count_context_tokens(complete, tools=request.tools)
                    wire_messages = convert_to_openai_messages(complete)
                    _copy_assistant_reasoning(complete, wire_messages)
                    encoded = json.dumps({"messages": wire_messages,
                                          "tools": [_function_tool_spec(t) for t in request.tools]},
                                         ensure_ascii=False, sort_keys=True).encode()
                    before_request["sha256"] = hashlib.sha256(encoded).hexdigest()
                    return original_summary_call(request, handler)
                # Instrument the actual summarizer entry: extra middleware can be
                # placed inside it by SDK middleware replacement/ordering.
                summary.wrap_model_call = track_boundary
                graph = create_deep_agent(
                    model=model, tools=tools, system_prompt=fixture["system"], backend=backend,
                    middleware=[FilesystemMiddleware(backend=backend, tools=["read_file"]),
                                _HostControlMiddleware(lambda: None, lambda _: None, model._invalid_tool_messages),
                                summary],
                )
                began = monotonic()
                status = "unexpected_end"
                try:
                    def observe(call):
                        usage.append({"call_id": call.call_id, "model": call.model, "status": call.status,
                                      "started_at": call.started_at, "ended_at": call.ended_at,
                                      "usage": asdict(call.usage) if call.usage else None})
                    with capture_model_usage(observe), model_diagnostic_scope(directory / "trace"):
                        graph.invoke({"messages": messages_from_dict(fixture["messages"]), "files": fixture["files"]})
                except BoundaryComplete:
                    status = "boundary_complete"
                except Exception as error:
                    status = type(error).__name__
                result = dict(case=fixture["case"], arm=arm, status=status,
                              wall_seconds=monotonic() - began, policy=asdict(policy),
                              factory_build_seconds=build_seconds,
                              fixture_sha256=fixture["sha256"], calls=model._calls, usage=usage,
                              scope=fixture["scope"], before_request=before_request)
                import gateway.agent_model.factory as factory_module
                result["factory_source"] = str(Path(factory_module.__file__).resolve())
                result["factory_sha256"] = hashlib.sha256(Path(factory_module.__file__).read_bytes()).hexdigest()
                save(directory / "result.json", result)
                print(json.dumps({k: result[k] for k in ("case", "arm", "status", "wall_seconds", "policy")}), flush=True)
    finally:
        factory.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--arm", choices=["baseline", "dynamic"])
    parser.add_argument("--only")
    parser.add_argument("--expected-context-size", type=int)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare, args.fixtures)
    else:
        run(args.fixtures, args.out, args.arm, args.only, args.expected_context_size)


if __name__ == "__main__":
    main()
