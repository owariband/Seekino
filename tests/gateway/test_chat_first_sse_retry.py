"""Chat first-SSE deadline + retry against a real local HTTP server; no paid providers."""
from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from gateway.agent_model.deepagents import DeepAgentSessionStore, DeepAgentsSupervisorModel
from gateway.agent_model.errors import ModelFailureReason, OpenAIFunctionCallingError
from gateway.model_events import observe_model_retry, observe_model_text
from gateway.openai import agent as openai_agent_module
from gateway.openai.agent import OpenAIAgentModel
from gateway.usage import ModelCallCancelledError, control_model_calls


def _chunk(delta=None, *, finish=None, usage=None):
    return {
        "id": "chat-test", "object": "chat.completion.chunk", "created": 0,
        "model": "offline",
        "choices": ([] if usage is not None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]),
        "usage": usage,
    }


def _event(payload):
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


@contextmanager
def _server(behaviors):
    state = SimpleNamespace(requests=0, received=Event(), stop=Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state.requests += 1
            state.received.set()
            behavior = behaviors[min(state.requests - 1, len(behaviors) - 1)]
            try:
                if behavior == "keep_alive":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    while not state.stop.wait(0.02):
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    return
                if behavior == "stall":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    self.connection.settimeout(30)
                    self.connection.recv(1)
                    return
                if behavior == "close_before_event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    return
                if behavior == "one_event_then_drop":
                    event = _event(_chunk({"role": "assistant", "content": "partial"}))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(event) + 64))
                    self.end_headers()
                    self.wfile.flush()
                    self.wfile.write(event)
                    self.wfile.flush()
                    return
                if behavior == "http_402":
                    body = json.dumps({"error": {"message": "Insufficient balance"}}).encode()
                    self.send_response(402)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    return
                # default: a normal chat stream ending in stop + usage.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                self.wfile.write(_event(_chunk({"role": "assistant", "content": "answer text"})))
                self.wfile.write(_event(_chunk({}, finish="stop")))
                self.wfile.write(_event(_chunk(usage={
                    "prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6,
                })))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture(autouse=True)
def short_limits(monkeypatch):
    monkeypatch.setattr(openai_agent_module, "CHAT_FIRST_SSE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(openai_agent_module, "CHAT_FIRST_SSE_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _chat_model(url, *, request_timeout_seconds=30):
    return OpenAIAgentModel(
        "offline-model", stream=True, provider_name="deepseek", profile="deepseek",
        base_url=url, api_key="offline", max_output_tokens=100,
        request_timeout_seconds=request_timeout_seconds,
    )


def _search_tool():
    return StructuredTool.from_function(
        lambda query: "accepted", name="search", description="Search evidence.",
    )


def _run_loop(model, *, handle_text=None):
    texts = []
    model.run_agent_loop(
        "Answer briefly.", tools=[_search_tool()], max_model_calls=1,
        handle_text=handle_text or texts.append,
    )
    return texts


def test_keep_alive_only_exhausts_reconnects_with_network_error():
    with _server(["keep_alive"]) as (url, state):
        model = _chat_model(url)
        started = monotonic()
        observations = []
        with observe_model_retry(lambda *event: observations.append(event)), pytest.raises(OpenAIFunctionCallingError) as caught:
            _run_loop(model)
        elapsed = monotonic() - started
    assert caught.value.reason == ModelFailureReason.MODEL_NETWORK_ERROR
    assert [(attempt, maximum) for _, attempt, maximum in observations] == [(1, 3), (2, 3), (3, 3), (0, 3)]
    assert len({call for call, _, _ in observations}) == 1
    assert state.requests == 4
    assert elapsed < 4 * 0.3 + 1.0


def test_closed_before_first_event_retries_and_recovers():
    with _server(["close_before_event", "stream"]) as (url, state):
        model = _chat_model(url)
        texts = _run_loop(model)
    assert texts == ["answer text"]
    assert state.requests == 2


def test_stalled_first_attempt_uses_second_stream():
    with _server(["stall", "stream"]) as (url, state):
        model = _chat_model(url)
        observations = []
        with observe_model_retry(lambda *event: observations.append(event)):
            texts = _run_loop(model)
        assert observations[0][1:] == (1, 3)
        assert observations[1] == (observations[0][0], 0, 3)
    assert texts == ["answer text"]
    assert state.requests == 2


def test_event_then_drop_is_not_retried():
    with _server(["one_event_then_drop"]) as (url, state):
        model = _chat_model(url)
        with pytest.raises(OpenAIFunctionCallingError) as caught:
            _run_loop(model)
    assert caught.value.reason == ModelFailureReason.MODEL_CONNECTION_ERROR
    assert state.requests == 1


def test_http_402_is_not_retried_and_preserves_provider_message():
    with _server(["http_402"]) as (url, state):
        model = _chat_model(url)
        with pytest.raises(OpenAIFunctionCallingError) as caught:
            _run_loop(model)
    assert state.requests == 1
    cause = caught.value.__cause__
    assert getattr(cause, "status_code", None) == 402
    assert "Insufficient balance" in json.dumps(getattr(cause, "body", {}) or {})


def test_cancellation_during_backoff_stops_immediately(monkeypatch):
    monkeypatch.setattr(openai_agent_module, "CHAT_FIRST_SSE_BACKOFF_SECONDS", (30.0, 30.0, 30.0))
    calls = []
    with _server(["keep_alive"]) as (url, state):
        model = _chat_model(url)

        def cancel_after_first_request():
            assert state.received.wait(5)
            sleep(1.0)
            calls[0].cancel()

        thread = Thread(target=cancel_after_first_request, daemon=True)
        thread.start()
        with control_model_calls(SimpleNamespace(acquire=calls.append, release=lambda _call: None)):
            with pytest.raises(ModelCallCancelledError):
                _run_loop(model)
        thread.join(2)
    assert state.requests == 1


def test_failed_attempts_emit_no_text_delta():
    deltas = []
    with _server(["keep_alive"]) as (url, state):
        model = _chat_model(url)
        with observe_model_text(deltas.append):
            with pytest.raises(OpenAIFunctionCallingError) as caught:
                _run_loop(model)
    assert caught.value.reason == ModelFailureReason.MODEL_NETWORK_ERROR
    assert state.requests == 4
    assert deltas == []


def test_deepagents_supervisor_path_retries():
    with _server(["keep_alive", "stream"]) as (url, state):
        adapter = OpenAIAgentModel(
            "offline-model", stream=True, provider_name="deepseek", profile="deepseek",
            base_url=url, api_key="offline", max_output_tokens=100,
            request_timeout_seconds=30,
        )
        model = DeepAgentsSupervisorModel(
            session_store=DeepAgentSessionStore(), model="offline-model",
            provider_name="deepseek", request_timeout_seconds=30,
            max_input_chars=120_000, context_window_policy=None, agent_model=adapter,
        )
        chunks = []
        try:
            model.run_agent(
                "Answer briefly.", thread_id="conversation", message_id="answer",
                user_message="Answer briefly.", history=[], tools=[_search_tool()],
                max_model_calls=1, handle_text=chunks.append,
            )
        finally:
            model.session_store.close()
    assert chunks == ["answer text"]
    assert state.requests == 2
