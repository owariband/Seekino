"""Exercise real SDK SSE decoding through Deep Agents and public snapshots."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI
from langchain_core.tools import StructuredTool

from gateway.agent_model.deepagents import DeepAgentSessionStore, DeepAgentsSupervisorModel
from gateway.model_diagnostics import model_diagnostic_scope
from gateway.openai.agent import OpenAIAgentModel
from gateway.usage import model_call_context
from tests.agent.chat.test_supervisor import _inputs, _runtime


def chunk(content=None, *, delta=None, finish=None):
    payload = {"id": "wire-response", "object": "chat.completion.chunk", "created": 0, "model": "test",
               "choices": [{"index": 0, "delta": delta if delta is not None else {"content": content},
                            "finish_reason": finish}]}
    return ("data: " + json.dumps(payload) + "\n\n").encode()


class Wire(httpx.SyncByteStream):
    def __init__(self, script):
        self.script = script
        self.closed = False

    def __iter__(self):
        yield from self.script()

    def close(self):
        self.closed = True


def wire_model(script, *, stream=True, fallback=None):
    requests = []
    wire = Wire(script)

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if not body["stream"]:
            return httpx.Response(200, json={"id": "full", "object": "chat.completion", "created": 0,
                "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    client = OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    adapter = OpenAIAgentModel("test", base_url="https://offline.invalid/v1", api_key="offline",
        client=client, max_output_tokens=2000, request_timeout_seconds=20, stream=stream, fallback_adapter=fallback)
    model = DeepAgentsSupervisorModel(session_store=DeepAgentSessionStore(), model="test", provider_name="test",
        request_timeout_seconds=20, max_input_chars=120000, context_window_policy=None, agent_model=adapter)
    return model, requests, wire, client


def test_provider_text_reaches_deltas_before_terminal_and_keeps_whitespace(tmp_path):
    events = []
    def deltas():
        return [e for e in events if e["type"] == "answer.delta"]
    def script():
        yield chunk(delta={"role": "assistant", "reasoning_content": "private"})
        assert deltas() == []
        yield chunk("Hello ")
        assert deltas()[-1]["delta"] == "Hello "
        assert deltas()[-1]["newBlock"]
        assert deltas()[-1]["textStream"]["beforeModelCompleted"]
        yield chunk("world")
        assert deltas()[-1]["delta"] == "world"
        assert not deltas()[-1]["newBlock"]
        assert not any(e["type"] == "answer.model.completed" for e in events)
        yield chunk(delta={}, finish="stop")
        yield b"data: [DONE]\n\n"

    model, requests, wire, client = wire_model(script)
    try:
        with model_call_context({"assistant_message_id": "answer", "conversation_id": "conversation"}), model_diagnostic_scope(tmp_path / "trace"):
            result = _runtime(model).kick(_inputs(), load_video_context=lambda: [], emit=events.append)
        assert result.content == "Hello world"
        assert requests[0]["stream"] is True and wire.closed
        model.confirm_turn("conversation", "answer", result.content)
        assert "".join(e["delta"] for e in deltas()) == "Hello world"
        source = deltas()[0]["textStream"]
        trace = [json.loads(line) for line in (tmp_path / "trace" / source["callId"] / "events.jsonl").read_text().splitlines()]
        assert all("monotonic_ns" in e for e in trace)
        assert next(e for e in trace if e["event"] == "request")["payload"]["request"]["stream"] is True
        assert len([e for e in trace if e["event"] == "chat_text_delta"]) == 2
        assert any(e["type"] == "answer.model.completed" and e["callId"] == source["callId"] for e in events)
    finally:
        client.close()


def test_explicit_nonstream_produces_no_live_source():
    model, requests, _, client = wire_model(lambda: iter(()), stream=False)
    events = []
    try:
        result = _runtime(model).kick(_inputs(), load_video_context=lambda: [], emit=events.append)
        assert result.content == "Hello world" and requests[0]["stream"] is False
        assert not any("textStream" in e or e["type"] == "answer.model.completed" for e in events)
    finally:
        client.close()


def test_streaming_transport_with_buffered_host_is_detected(monkeypatch):
    from contextlib import nullcontext
    events = []
    monkeypatch.setattr("gateway.agent_model.deepagents.observe_model_text", lambda observer: nullcontext())
    def script():
        yield chunk(delta={"role": "assistant"})
        yield chunk("Hello world")
        assert not any(e["type"] == "answer.snapshot" for e in events)
        yield chunk(delta={}, finish="stop")
        yield b"data: [DONE]\n\n"
    model, requests, _, client = wire_model(script)
    try:
        assert _runtime(model).kick(_inputs(), load_video_context=lambda: [], emit=events.append).content == "Hello world"
        assert requests[0]["stream"] and not any("textStream" in e for e in events)
    finally:
        client.close()


def test_failure_before_any_body_preserves_nonstream_fallback_configuration():
    from gateway.ark.agent import ArkFunctionCallingFallback
    from tests.gateway.test_deepagents import FakeClient, _completion
    backup = FakeClient([_completion(content="Backup answer")])
    fallback = ArkFunctionCallingFallback("backup", stream=False, max_output_tokens=200,
                                         request_timeout_seconds=10, client=backup)
    def script():
        yield chunk(delta={"role": "assistant", "reasoning_content": "private"})
        raise httpx.ReadError("disconnected before body")
    model, _, _, client = wire_model(script, fallback=fallback)
    events = []
    try:
        result = _runtime(model).kick(_inputs(), load_video_context=lambda: [], emit=events.append)
        assert result.content == "Backup answer" and backup.calls[0]["stream"] is False
        assert not any("textStream" in e for e in events)
    finally:
        client.close()


def test_split_web_url_is_checked_after_assembly_without_duplicate_text():
    from time import monotonic
    from gateway.model_events import ModelTextDelta
    from tests.agent.chat.test_supervisor import FakeSupervisorModel
    class Model(FakeSupervisorModel):
        supports_text_deltas = True
        def run_agent(self, prompt, *, tools, handle_text, handle_text_delta, **kwargs):
            available = {tool.name: tool for tool in tools}
            available["search_web"].invoke({"query": "source"})
            parts = ["Source [ref](ht", "tps:/", "/example.org/source", ") and https://unverified.invalid/end."]
            for i, part in enumerate(parts, 1):
                handle_text_delta(ModelTextDelta(part, "call", i, monotonic(), i, 1, True))
            handle_text("".join(parts))
    events = []
    result = _runtime(Model()).kick(_inputs(), load_video_context=lambda: [], emit=events.append)
    assert result.content.startswith("Source [ref](https://example.org/source) and [未核实链接]")
    assert all("unverified.invalid" not in e.get("content", "") for e in events)
    assert result.content.count("Source") == 1


def test_partial_stream_failure_keeps_body_and_does_not_replay_fallback():
    events, fallback_calls = [], []
    fallback = SimpleNamespace(provider_name="fallback", model_name="backup",
                               acquire_client=lambda _: fallback_calls.append(True))
    def script():
        yield chunk(delta={"role": "assistant"})
        yield chunk("Partial answer")
        raise httpx.ReadError("disconnected")
    model, requests, wire, client = wire_model(script, fallback=fallback)
    try:
        with pytest.raises(Exception, match="Supervisor model"):
            _runtime(model).kick(_inputs(), load_video_context=lambda: [], emit=events.append)
        assert [e["content"] for e in events if e["type"] == "answer.delta"] == ["Partial answer"]
        assert fallback_calls == [] and len(requests) == 1 and wire.closed
        assert model.session_store.pending == {}
    finally:
        client.close()


def test_tool_arguments_are_not_executed_or_delivered_before_completion():
    texts, calls, round_no = [], [], 0
    tool = StructuredTool.from_function(lambda query: calls.append(query) or "evidence", name="lookup", description="Read evidence")
    def script():
        nonlocal round_no
        round_no += 1
        yield chunk(delta={"role": "assistant"})
        if round_no == 1:
            yield chunk(delta={"tool_calls": [{"index": 0, "id": "lookup-1", "type": "function", "function": {"name": "lookup", "arguments": '{"query":'}}]})
            assert calls == [] and texts == []
            yield chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": '"course"}'}}]})
            assert calls == [] and texts == []
            yield chunk(delta={}, finish="tool_calls")
        else:
            yield chunk("Answer")
            assert texts[-1].text == "Answer" and calls == ["course"]
            yield chunk(delta={}, finish="stop")
        yield b"data: [DONE]\n\n"
    model, _, _, client = wire_model(script)
    try:
        model.run_agent("Answer", thread_id="conversation", message_id="answer", user_message="Question",
                        history=[], tools=[tool], max_model_calls=3, handle_text=lambda _: None, handle_text_delta=texts.append)
        assert calls == ["course"]
    finally:
        client.close()


def test_compaction_stream_is_private_and_only_public_model_text_is_observed():
    from gateway.agent_model.context import ContextWindowPolicy
    deltas, texts = [], []
    def script():
        summary = "Context Extraction Assistant" in json.dumps(requests[-1])
        yield chunk(delta={"role": "assistant"})
        yield chunk("Private summary" if summary else "Visible answer")
        yield chunk(delta={}, finish="stop")
        yield b"data: [DONE]\n\n"
    model, requests, _, client = wire_model(script)
    model._context_window_policy = ContextWindowPolicy(
        context_window_tokens=8000, output_reserve_tokens=500, safety_margin_tokens=500,
        auto_compact_tokens=1500, compact_target_tokens=200,
    )
    history = [{"messageId": f"history-{i}", "role": "user" if i % 2 == 0 else "assistant",
                "content": "Earlier detailed facts " * 100} for i in range(10)]
    try:
        model.run_agent("Answer", thread_id="conversation", message_id="answer", user_message="Question",
                        history=history, tools=[], max_model_calls=3, handle_text=texts.append, handle_text_delta=deltas.append)
        assert len(requests) == 2
        assert texts == ["Visible answer"]
        assert [delta.text for delta in deltas] == ["Visible answer"]
    finally:
        client.close()
