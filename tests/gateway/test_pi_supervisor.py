from dataclasses import replace

import pytest
from langchain_core.tools import StructuredTool

from gateway.agent_model.contracts import AgentModelProfile, SupervisorSessionContext
from gateway.agent_model.errors import AgentModelTimeoutError, ModelFailureError, ModelFailureReason, ModelInputTooLargeError
from gateway.media import ToolImage, ToolImageArtifact
from gateway.pi.client import PiHostError
from gateway.pi.supervisor import PiSupervisorModel
from gateway.usage import capture_model_usage, model_call_context


def profile(**options):
    return AgentModelProfile(
        id="user-A:profile", revision="1", provider="compatible", model="configured-model",
        api_key="private-key", base_url="https://model.example/v1", **options,
    )


class Host:
    def __init__(self):
        self.prompts, self.commands = [], []
        self.error = None
        self.tool_call = None
        self.tool_result = None

    def prompt(self, **options):
        self.prompts.append(options)
        options["check_control"]()
        options["on_event"]({"type": "state", "state": {"sessionFile": "opaque.jsonl", "pendingRunId": options["run_id"]}})
        options["on_event"]({"type": "usage", "usage": {"input": 10, "output": 3, "cacheRead": 2, "cacheWrite": 1}})
        if self.tool_call:
            self.tool_result = options["on_tool"](*self.tool_call)
        if self.error:
            raise self.error
        options["on_event"]({"type": "text_delta", "delta": "answer"})
        return {"text": "answer", "state": {"sessionFile": "opaque.jsonl", "pendingRunId": options["run_id"]}}

    def request(self, command, **options):
        self.commands.append((command, options))
        return {"state": {"sessionFile": "opaque.jsonl", "committedLeafId": "committed"}}


def model(host=None, **options):
    return PiSupervisorModel(client=host or Host(), profile=profile(**options),
                             request_timeout_seconds=30, max_input_chars=100_000)


def run(adapter, **options):
    output = []
    adapter.run_agent("dynamic business instructions", thread_id="conversation", message_id="message",
                      user_message="current question", history=[], tools=[], max_model_calls=4,
                      handle_text=output.append, **options)
    return output


def test_sql_history_state_and_canonical_confirmation_use_existing_contract():
    host, saved = Host(), []
    context = SupervisorSessionContext(
        states={"pi": {"sessionFile": "old.jsonl", "committedLeafId": "old"}},
        message_sequence_no=4,
        load_history=lambda after: [
            {"role": "USER", "content": "prior question", "messageId": "u1"},
            {"role": "ASSISTANT", "content": "SQL answer", "messageId": "a1"},
            {"role": "ASSISTANT", "content": "failed", "status": "FAILED"},
            {"role": "USER", "content": "duplicate", "messageId": "u1"},
        ],
        save_state=lambda key, state: saved.append((key, state)),
    )
    adapter = model(host)
    assert run(adapter, session_context=context) == ["answer"]
    assert host.prompts[0]["history"] == [
        {"role": "user", "content": "prior question"}, {"role": "assistant", "content": "SQL answer"},
    ]
    assert host.prompts[0]["state"]["sessionFile"] == "old.jsonl"
    assert saved[-1][1]["pendingRunId"] == "message"
    assert host.commands == []
    with pytest.raises(RuntimeError, match="not been confirmed"):
        run(adapter)
    adapter.confirm_turn("conversation", "stale", "unused")
    assert host.commands == []
    adapter.confirm_turn("conversation", "message", "canonical SQL content")
    assert host.commands[-1] == ("confirm_turn", {
        "session_id": "conversation", "run_id": "message", "content": "canonical SQL content",
    })
    assert saved[-1] == ("pi", {"sessionFile": "opaque.jsonl", "committedLeafId": "committed"})


def test_each_run_uses_its_authorized_profile_without_mutating_default_or_state():
    host, saved = Host(), []
    adapter = model(host)
    chosen = replace(profile(), id="user-B:profile", api_key="another-secret", base_url="https://other.example/v1")
    context = SupervisorSessionContext({}, 1, lambda _: [], lambda *args: saved.append(args), chosen)
    run(adapter, session_context=context)
    assert host.prompts[0]["profile"]["apiKey"] == "another-secret"
    assert host.prompts[0]["profile"]["id"] == "user-B:profile"
    assert "another-secret" not in repr(chosen)
    assert "another-secret" not in str(saved)
    adapter.confirm_turn("conversation", "message", "answer")
    run(adapter)
    assert host.prompts[1]["profile"]["apiKey"] == "private-key"


@pytest.mark.parametrize("failure", [None, PiHostError("model_error", "provider failed")])
def test_usage_survives_failure_and_keeps_sdk_run_granularity(failure):
    host = Host()
    host.error = failure
    observed = []
    with model_call_context({"assistant_message_id": "message"}), capture_model_usage(observed.append):
        if failure:
            with pytest.raises(ModelFailureError):
                run(model(host))
            assert host.commands[-1][0] == "discard_turn"
        else:
            run(model(host))
    assert len(observed) == 1
    assert observed[0].usage.input_tokens == 13
    assert observed[0].usage.output_tokens == 3
    assert observed[0].usage.cached_input_tokens == 2
    assert observed[0].usage.cache_write_input_tokens == 1
    assert observed[0].granularity == "sdk_turn_run"
    assert observed[0].status == ("failed" if failure else "completed")
    assert observed[0].context["assistant_message_id"] == "message"


def test_timeout_maps_to_existing_error_and_does_not_close_shared_host():
    host = Host()
    host.error = PiHostError("timeout", "expired")
    with pytest.raises(AgentModelTimeoutError):
        run(model(host))
    assert host.commands[-1][0] == "discard_turn"
    host.error = None
    assert run(model(host)) == ["answer"]


@pytest.mark.parametrize(("code", "error_type", "reason"), [
    ("input_too_large", ModelInputTooLargeError, None),
    ("session_limit", ModelInputTooLargeError, None),
    ("model_call_limit", ModelFailureError, ModelFailureReason.LOOP_LIMIT),
    ("output_token_limit", ModelFailureError, ModelFailureReason.OUTPUT_TOKEN_LIMIT),
])
def test_pi_budget_failures_preserve_existing_business_error_categories(code, error_type, reason):
    host = Host()
    host.error = PiHostError(code, "budget exceeded")
    with pytest.raises(error_type) as failure:
        run(model(host))
    if reason is not None:
        assert failure.value.reason == reason
    assert host.commands[-1][0] == "discard_turn"


def test_tool_images_and_model_visible_errors_keep_langchain_contract():
    host = Host()
    artifact = ToolImageArtifact((ToolImage("data:image/png;base64,AAAA", 3, "frame"),))
    tool = StructuredTool.from_function(lambda: ("evidence", artifact), name="video_frame",
                                       description="Read a frame", response_format="content_and_artifact")
    host.tool_call = ("video_frame", {}, "call-1")
    adapter = model(host, supports_vision_input=True)
    adapter.run_agent("instructions", thread_id="c", message_id="m", user_message="question",
                      history=[], tools=[tool], max_model_calls=2, handle_text=lambda _: None)
    assert host.tool_result == {"content": [
        {"type": "text", "text": "evidence"}, {"type": "text", "text": "frame"},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
    ], "isError": False}


def test_fatal_business_tool_error_is_propagated_and_turn_discarded():
    from agent.utils.errors import AgentToolInvocationError
    def broken():
        raise AgentToolInvocationError("business execution failed")
    tool = StructuredTool.from_function(broken, name="business_tool", description="Execute business operation")
    host = Host()
    host.tool_call = ("business_tool", {}, "call-1")
    with pytest.raises(AgentToolInvocationError):
        model(host).run_agent("instructions", thread_id="c", message_id="m", user_message="question",
                              history=[], tools=[tool], max_model_calls=2, handle_text=lambda _: None)
    assert host.commands[-1][0] == "discard_turn"


def test_preflight_rejects_images_without_starting_host_and_borrowed_close_is_safe():
    host = Host()
    adapter = model(host)
    with pytest.raises(ValueError, match="does not support image"):
        run(adapter, image_urls=["data:image/png;base64,AAAA"])
    assert host.prompts == host.commands == []
    assert run(adapter) == ["answer"]
    adapter.close()
    adapter.close()
    assert all(command != "shutdown" for command, _ in host.commands)
    with pytest.raises(RuntimeError, match="closed"):
        run(adapter)


def test_close_during_history_load_cannot_start_a_late_host_run():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    host, entered, release = Host(), Event(), Event()
    adapter = model(host)

    def load(_after):
        entered.set()
        assert release.wait(3)
        return []

    context = SupervisorSessionContext({}, 1, load, lambda *_: None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run, adapter, session_context=context)
        assert entered.wait(3)
        adapter.close()
        release.set()
        with pytest.raises(RuntimeError, match="closed"):
            pending.result(timeout=3)
    assert host.prompts == host.commands == []


def test_real_node_host_multiplexes_profiles_tools_and_restores_evicted_history(tmp_path):
    import json
    import os
    import signal
    import shutil
    import time
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from threading import Lock, Thread

    from gateway.pi.client import PiClient

    entry = Path(__file__).resolve().parents[2] / "gateway/pi/host/dist/src/main.js"
    if not entry.is_file() or shutil.which("node") is None:
        pytest.skip("Build gateway/pi/host to run the real SDK integration test")
    calls, calls_lock = [], Lock()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with calls_lock:
                calls.append((self.headers.get("Authorization"), payload))
            uses_tool = any(message.get("role") == "tool" for message in payload["messages"])
            delta = {"content": "answer"} if uses_tool else {"tool_calls": [{
                "index": 0, "id": "echo-call", "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }]}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if uses_tool else "tool_calls"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps({'id': 'test', 'object': 'chat.completion.chunk', 'model': payload['model'], **chunk})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server_thread = Thread(target=provider.serve_forever, daemon=True)
    server_thread.start()
    client = PiClient(config={"sessionDir": str(tmp_path), "maxLoadedSessions": 2})
    endpoint = f"http://127.0.0.1:{provider.server_port}/v1"
    observed_tools = []
    tool = StructuredTool.from_function(lambda: observed_tools.append("called") or "tool-evidence",
                                       name="echo", description="Get test evidence")
    states, histories = {"A": {}, "B": {}, "C": {}}, {"A": [], "B": [], "C": []}

    def execute(identity, turn):
        chosen = replace(profile(), id=f"user-{identity}", api_key=f"key-{identity}", base_url=endpoint)
        adapter = PiSupervisorModel(client=client, profile=chosen, request_timeout_seconds=20, max_input_chars=100_000)
        context = SupervisorSessionContext(
            states[identity], turn * 2, lambda _: histories[identity],
            lambda key, state: states[identity].update({key: state}),
        )
        output = []
        user_message = f"question-{identity}-{turn}"
        adapter.run_agent("Use echo for evidence, then answer.", thread_id=identity,
                          message_id=f"{identity}-{turn}", user_message=user_message,
                          history=[], tools=[tool], max_model_calls=4, handle_text=output.append,
                          session_context=context)
        histories[identity].extend([{"role": "user", "content": user_message}, {"role": "assistant", "content": "answer"}])
        adapter.confirm_turn(identity, f"{identity}-{turn}", "answer")
        adapter.close()
        return "".join(output)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(execute, identity, 1) for identity in ("A", "B")]
            assert [future.result(timeout=30) for future in futures] == ["answer", "answer"]
        pid = client.pid
        original_file = states["A"]["pi"]["sessionFile"]
        assert execute("C", 1) == "answer"
        assert execute("A", 2) == "answer"
        assert client.pid == pid
        assert states["A"]["pi"]["sessionFile"] == original_file
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while client.pid is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.pid is None
        assert execute("A", 3) == "answer"
        assert client.pid != pid
        assert states["A"]["pi"]["sessionFile"] == original_file
        assert observed_tools
        for auth, payload in calls:
            identity = auth.removeprefix("Bearer key-")
            assert identity in {"A", "B", "C"}
            text = json.dumps(payload["messages"])
            assert {item["function"]["name"] for item in payload["tools"]} == {"echo", "subagent"}
            assert f"question-{identity}-1" in text
            assert all(f"question-{other}-1" not in text for other in {"A", "B", "C"} - {identity})
        assert any("question-A-2" in json.dumps(payload["messages"]) and
                   "question-A-1" in json.dumps(payload["messages"]) for _, payload in calls)
        assert all("private-key" not in path.read_text() and "key-A" not in path.read_text()
                   for path in tmp_path.rglob("*.jsonl"))
    finally:
        client.close()
        provider.shutdown()
        provider.server_close()
        server_thread.join(timeout=3)
