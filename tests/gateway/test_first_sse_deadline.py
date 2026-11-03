"""Real SDK/socket tests: no paid providers and no sleeping model worker left behind."""
import json
import ssl
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from gateway.multimodal import TextInput
from gateway.openai.vision import OpenAIVisionModel
from gateway.openai.multimodal import DeepSeekModelError, DeepSeekMultimodalModel
from gateway.usage import ModelCallCancelledError, capture_model_usage, control_model_calls, model_call_context


class Answer(BaseModel):
    answer: str


def _chunk(delta, finish=None):
    return {"id": "test", "object": "chat.completion.chunk", "created": 0,
            "model": "offline", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


@contextmanager
def _server(mode, *, first_delta=None, tls_context=None, body_delay=0.8):
    state = SimpleNamespace(requests=0, received=Event(), disconnected=Event(), stop=Event(), first_event=Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            responses = "input" in request
            state.requests += 1
            state.received.set()
            try:
                if mode == "no_headers":
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b"":
                        state.disconnected.set()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if mode != "nonstream" else "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if mode == "no_event":
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b"":
                        state.disconnected.set()
                    return
                if mode == "heartbeats":
                    while not state.stop.wait(0.02):
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    return
                result = {"id": "test", "object": "response", "created_at": 0,
                          "model": "offline", "status": "completed", "output": [{
                              "type": "message", "id": "message", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}],
                          }]}
                if mode in {"timely", "after_first"}:
                    if responses:
                        self._event({"type": "response.created", "sequence_number": 0,
                                     "response": {**result, "status": "in_progress", "output": []}})
                    else:
                        self._event(_chunk(first_delta or {"role": "assistant"}))
                    state.first_event.set()
                    if mode == "after_first":
                        self.connection.settimeout(2)
                        if self.connection.recv(1) == b"":
                            state.disconnected.set()
                        return
                state.stop.wait(body_delay)  # Longer than startup budget, less than total budget.
                if responses:
                    if mode == "nonstream":
                        self.wfile.write(json.dumps(result).encode())
                    else:
                        self._event({"type": "response.output_text.delta", "sequence_number": 1,
                                     "output_index": 0, "content_index": 0, "item_id": "message", "delta": '{"answer":"ok"}'})
                        self._event({"type": "response.completed", "sequence_number": 2, "response": result})
                elif mode == "nonstream":
                    self.wfile.write(json.dumps({"id": "test", "object": "chat.completion", "created": 0,
                        "model": "offline", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                            "role": "assistant", "content": '{"answer":"ok"}'}}]}).encode())
                else:
                    self._event(_chunk({"role": "assistant", "content": '{"answer":"ok"}'}))
                    self._event(_chunk({}, "stop"))
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                state.disconnected.set()
            finally:
                self.close_connection = True

        def _event(self, payload):
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        scheme = "https" if tls_context is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}/v1", state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture(params=["chat", "responses"])
def run_model(request):
    def run(url, *, timeout=2, stream=True):
        if request.param == "responses":
            model = DeepSeekMultimodalModel(
                "offline", base_url=url, api_key="offline", max_output_tokens=100,
                request_timeout_seconds=timeout, stream=stream,
            )
        else:
            model = OpenAIVisionModel("offline", provider="offline", base_url=url, api_key="offline",
                                     request_timeout_seconds=timeout, stream=stream)
        try:
            return model.generate([TextInput("answer")], schema_name="answer", result_type=Answer)
        finally:
            close = getattr(model, "close", None)
            if close is not None:
                close()
    return run


@pytest.fixture(params=["http", "https"])
def local_server(request, monkeypatch, tmp_path):
    context = None
    if request.param == "https":
        pytest.importorskip("cryptography")
        from datetime import datetime, timedelta, timezone
        from ipaddress import ip_address
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / "localhost.pem", tmp_path / "localhost.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    return lambda mode, **kwargs: _server(mode, tls_context=context, **kwargs)


@pytest.fixture(autouse=True)
def startup_budget(monkeypatch):
    monkeypatch.setattr("gateway.streaming.first_sse.FIRST_SSE_TIMEOUT_SECONDS", 0.5)


@pytest.mark.parametrize("mode", ["no_headers", "no_event", "heartbeats"])
def test_blocked_first_event_fails_and_releases_connection_before_retry(mode, tmp_path, monkeypatch, local_server, run_model):
    monkeypatch.setenv("SEEKINO_MODEL_TRACE_DIR", str(tmp_path))
    usage = []
    with local_server(mode) as (url, state), capture_model_usage(usage.append), model_call_context({"video_parse_task_id": "offline-first-sse"}):
        for attempt in range(2):
            state.disconnected.clear()
            started = monotonic()
            with pytest.raises(ModelFailureError) as caught:
                run_model(url)
            elapsed = monotonic() - started
            assert caught.value.reason == ModelFailureReason.MODEL_TIMEOUT
            assert "first SSE" in str(caught.value)
            assert 0.4 <= elapsed < 1.4
            assert state.disconnected.wait(0.5), "retry must not overlap a blocked old transport"
            assert state.requests == attempt + 1  # No hidden SDK retries.
    assert len(usage) == 2 and all(record.status == "failed" for record in usage)
    events = [json.loads(line) for path in tmp_path.glob("*/events.jsonl") for line in path.read_text().splitlines()]
    assert sum(event["event"] == "first_sse_timeout" for event in events) == 2
    assert sum(event["event"] == "request" for event in events) == 2


@pytest.mark.parametrize("delta", [{"role": "assistant"}, {"reasoning_content": "thinking"}])
def test_first_role_or_reasoning_event_disarms_startup_limit(delta, local_server, run_model, monkeypatch):
    # The SDK builds a large Responses event union on its first decoded event.
    # Allow cold decoding under concurrent CPU load; still deliver the body only
    # after this startup limit, proving that the first event disarms the guard.
    monkeypatch.setattr("gateway.streaming.first_sse.FIRST_SSE_TIMEOUT_SECONDS", 2.0)
    with local_server("timely", first_delta=delta, body_delay=2.5) as (url, state):
        assert run_model(url, timeout=5).answer == "ok"
        assert state.requests == 1


def test_original_total_timeout_can_be_shorter_than_startup_timeout(local_server, run_model):
    with local_server("no_headers") as (url, state):
        started = monotonic()
        with pytest.raises(ModelFailureError) as caught:
            run_model(url, timeout=0.1)
        assert caught.value.reason == ModelFailureReason.MODEL_TIMEOUT
        assert monotonic() - started < 0.7
        assert state.disconnected.wait(0.5)


def test_first_event_does_not_extend_original_total_budget(local_server, run_model):
    with local_server("timely", first_delta={"role": "assistant"}) as (url, state):
        with pytest.raises((ModelFailureError, DeepSeekModelError)) as caught:
            run_model(url, timeout=0.4)
        if isinstance(caught.value, ModelFailureError):
            assert caught.value.reason == ModelFailureReason.MODEL_TIMEOUT
        else:
            assert "timed out" in str(caught.value).lower()


def test_nonstream_has_no_first_event_deadline(local_server, run_model):
    with local_server("nonstream") as (url, state):
        assert run_model(url, stream=False).answer == "ok"
        assert state.requests == 1


@pytest.mark.parametrize("mode", ["no_headers", "no_event", "after_first"])
def test_owner_cancellation_remains_cancellation(mode, local_server, run_model):
    calls, released = [], []
    with local_server(mode) as (url, state):
        def cancel():
            ready = state.first_event if mode == "after_first" else state.received
            assert ready.wait(1)
            calls[0].cancel()
        thread = Thread(target=cancel, daemon=True)
        thread.start()
        with control_model_calls(SimpleNamespace(acquire=calls.append, release=released.append)):
            with pytest.raises(ModelCallCancelledError):
                run_model(url)
        thread.join(1)
        assert not thread.is_alive() and released == calls
        assert state.disconnected.wait(0.5)
