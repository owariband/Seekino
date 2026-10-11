from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest
from acp import (
    AcceptElicitationResponse, CancelElicitationResponse, DeclineElicitationResponse,
    RequestError, text_block,
)
from acp.schema import McpServerStdio

from gateway.bridge import BridgeClient


# The peer uses the real SDK and stdio transport; only agent behavior is scripted.
_PEER = '''
import asyncio
import json
import os
from pathlib import Path
from acp import (
    run_agent, plan_entry, start_tool_call, update_agent_message_text,
    update_plan, update_tool_call,
)
from acp.schema import (
    AgentCapabilities, ElicitationFormRequestMode, ElicitationFormSessionMode,
    ElicitationSchema, ElicitationStringPropertySchema, ElicitationUrlSessionMode,
    InitializeResponse, LoadSessionResponse, NewSessionResponse, PermissionOption,
    PromptResponse, ResumeSessionResponse, SessionCapabilities,
    SessionResumeCapabilities, ToolCallUpdate,
)


class Peer:
    def __init__(self):
        self.sessions = {}
        self.request_ids = {}

    def on_connect(self, conn):
        self.conn = conn

    def observe(self, event):
        message = event.message
        if event.direction == "incoming" and message.get("method") == "session/prompt":
            self.request_ids[message["params"]["sessionId"]] = message["id"]

    def record(self, kind, **fields):
        with Path("transcript.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, **fields}) + "\\n")

    async def initialize(self, protocol_version, client_capabilities=None, **kwargs):
        self.record("initialize", capabilities=client_capabilities.model_dump(by_alias=True, exclude_none=True))
        if os.environ.get("PEER_FAIL_INITIALIZE"):
            from acp import RequestError
            raise RequestError.invalid_params({"reason": "unsupported client"})
        resume = SessionResumeCapabilities() if os.environ.get("PEER_RESUME") else None
        return InitializeResponse(protocol_version=protocol_version, agent_capabilities=AgentCapabilities(
            load_session=not bool(resume), session_capabilities=SessionCapabilities(resume=resume)))

    async def new_session(self, cwd, mcp_servers=None, additional_directories=None, **kwargs):
        session = "opaque/session:" + str(len(self.sessions) + 1)
        self.sessions[session] = asyncio.Event()
        self.record("new", session=session, cwd=cwd,
            mcp=[item.model_dump(by_alias=True) for item in mcp_servers or []],
            directories=additional_directories)
        return NewSessionResponse(session_id=session)

    async def load_session(self, session_id, cwd, **kwargs):
        self.sessions[session_id] = asyncio.Event()
        self.record("load", session=session_id, cwd=cwd)
        await self.conn.session_update(session_id, update_agent_message_text("replayed answer"))
        return LoadSessionResponse()

    async def resume_session(self, session_id, cwd, **kwargs):
        self.sessions[session_id] = asyncio.Event()
        self.record("resume", session=session_id, cwd=cwd)
        return ResumeSessionResponse()

    async def emit(self, session, text):
        await self.conn.session_update(session, update_agent_message_text(text))

    async def prompt(self, session_id, prompt, **kwargs):
        text = "".join(getattr(item, "text", "") for item in prompt)
        self.record("prompt", session=session_id, text=text)
        stopped = self.sessions[session_id]
        stopped.clear()
        await self.emit(session_id, "started:" + text)
        if text == "events":
            await self.conn.session_update(session_id,
                start_tool_call("tool-1", "Inspect file", kind="read", status="in_progress"))
            await self.conn.session_update(session_id, update_plan([plan_entry("Inspect file", status="completed")]))
            await self.conn.session_update(session_id, update_tool_call("tool-1", status="completed"))
        elif text == "blocked":
            while not Path("release").exists():
                await asyncio.sleep(0.01)
        elif text in ("wait", "ignore_cancel"):
            if text == "ignore_cancel":
                await asyncio.Event().wait()
            await stopped.wait()
            await self.emit(session_id, "cancelled-final")
            return PromptResponse(stop_reason="cancelled")
        elif text in ("permission", "bad_scope"):
            scope = "unknown-session" if text == "bad_scope" else session_id
            request = asyncio.create_task(self.conn.request_permission(
                scope, ToolCallUpdate(tool_call_id="permission-1", title="Edit file", kind="edit"),
                [PermissionOption(option_id="allow-this", name="Allow", kind="allow_once"),
                 PermissionOption(option_id="deny-this", name="Reject", kind="reject_once")]))
            await self.emit(session_id, "while-permission-pending")
            response = await request
            await self.emit(session_id, "permission:" + response.outcome.model_dump_json(
                by_alias=True, exclude_none=True))
        elif text.startswith("form") or text == "url":
            schema = ElicitationSchema(
                properties={"answer": ElicitationStringPropertySchema(type="string")}, required=["answer"])
            if text == "form-request":
                mode = ElicitationFormRequestMode(request_id=self.request_ids[session_id], requested_schema=schema)
            elif text == "form-stale-request":
                mode = ElicitationFormRequestMode(request_id=987654, requested_schema=schema)
            elif text == "url":
                mode = ElicitationUrlSessionMode(
                    session_id=session_id, elicitation_id="url-1", url="https://example.com/auth")
            else:
                mode = ElicitationFormSessionMode(session_id=session_id, requested_schema=schema)
            request = asyncio.create_task(self.conn.create_elicitation("Choose an answer", mode))
            await self.emit(session_id, "while-form-pending")
            response = await request
            await self.emit(session_id, "form:" + response.model_dump_json(by_alias=True, exclude_none=True))
        elif text == "crash":
            os._exit(23)
        elif text == "stderr-child":
            import subprocess
            import sys
            child = subprocess.Popen([sys.executable, "-c",
                "import time; from pathlib import Path; time.sleep(2); Path('child-finished').touch()"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            self.record("child", pid=child.pid)
        await self.emit(session_id, "answer:" + text)
        return PromptResponse(stop_reason="cancelled" if stopped.is_set() else "end_turn")

    async def cancel(self, session_id, **kwargs):
        self.record("cancel", session=session_id)
        self.sessions[session_id].set()


peer = Peer()
asyncio.run(run_agent(peer, use_unstable_protocol=True, observers=[peer.observe]))
'''


@pytest.fixture
def bridge_factory(tmp_path):
    script = tmp_path / "peer.py"
    script.write_text(_PEER)
    clients = []

    def create(**kwargs):
        client = BridgeClient(sys.executable, args=[str(script)], cwd=str(tmp_path), **kwargs)
        clients.append(client)
        client.open()
        return client

    yield create
    for client in clients:
        client.close()


def _transcript(tmp_path):
    return [json.loads(line) for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]


def _texts(events):
    return [update.content.text for _, update, _ in events if update.session_update == "agent_message_chunk"]


def test_stdio_lifecycle_and_typed_session_updates(bridge_factory, tmp_path):
    events = []
    client = bridge_factory(
        on_update=lambda session, update, *, replay=False: events.append((session, update, replay)))
    pid = client.pid
    mcp = McpServerStdio(name="evidence", command=sys.executable, args=["-m", "evidence"], env=[])
    session = client.new_session(
        cwd=str(tmp_path), mcp_servers=[mcp], additional_directories=[str(tmp_path / "assets")])
    assert session.session_id == "opaque/session:1"
    assert client.prompt(session.session_id, [text_block("events")]).stop_reason == "end_turn"
    assert {update.session_update for _, update, _ in events} == {
        "agent_message_chunk", "tool_call", "tool_call_update", "plan",
    }
    assert all(scope == session.session_id and replay is False for scope, _, replay in events)
    assert _texts(events) == ["started:events", "answer:events"]
    records = _transcript(tmp_path)
    capabilities = records[0]["capabilities"]
    assert not capabilities.get("terminal")
    assert not capabilities.get("fs", {}).get("readTextFile")
    assert not capabilities.get("fs", {}).get("writeTextFile")
    assert not capabilities.get("elicitation", {}).get("url")
    assert records[1]["cwd"] == str(tmp_path)
    assert records[1]["directories"] == [str(tmp_path / "assets")]
    assert records[1]["mcp"][0]["name"] == "evidence"
    assert records[1]["mcp"][0]["args"] == ["-m", "evidence"]
    client.close()
    client.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("resume", [False, True])
def test_reconnect_loads_opaque_session_without_replaying_a_prompt(bridge_factory, tmp_path, resume):
    first = bridge_factory()
    session = first.new_session(cwd=str(tmp_path)).session_id
    first.prompt(session, "first")
    first.close()
    events = []
    reloaded = bridge_factory(env={"PEER_RESUME": "1"} if resume else {},
        on_update=lambda scope, update, *, replay=False: events.append((scope, update, replay)))
    reloaded.load_session(session, cwd=str(tmp_path))
    reloaded.prompt(session, "second")
    assert [item["text"] for item in _transcript(tmp_path) if item["kind"] == "prompt"] == ["first", "second"]
    restored = next(item for item in _transcript(tmp_path) if item["kind"] in ("load", "resume"))
    assert restored["kind"] == ("resume" if resume else "load")
    if not resume:
        assert [(scope, update.content.text, replay) for scope, update, replay in events][0] == (
            session, "replayed answer", True)
    assert [update.content.text for _, update, replay in events if not replay] == ["started:second", "answer:second"]


def test_load_finishes_replay_delivery_before_next_prompt(bridge_factory, tmp_path):
    arrived = Event()
    delivered = Future()
    events = []

    def update(session, value, *, replay=False):
        events.append((session, value, replay))
        if replay:
            arrived.set()
            return delivered

    client = bridge_factory(on_update=update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = pool.submit(client.load_session, "opaque/session:1", cwd=str(tmp_path))
        try:
            assert arrived.wait(5)
            prompt = pool.submit(client.prompt, "opaque/session:1", "after load")
            assert not loaded.done()
            assert "started:after load" not in _texts(events)
        finally:
            delivered.set_result(None)
        loaded.result(5)
        assert prompt.result(5).stop_reason == "end_turn"
    assert [replay for _, _, replay in events] == [True, False, False]


def test_same_session_serializes_while_other_sessions_continue(bridge_factory, tmp_path):
    started = Event()
    events = []

    def receive(session, update, *, replay=False):
        events.append((session, update, replay))
        if getattr(getattr(update, "content", None), "text", None) == "started:blocked":
            started.set()

    client = bridge_factory(on_update=receive)
    first = client.new_session(cwd=str(tmp_path)).session_id
    second = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=3) as pool:
        blocked = pool.submit(client.prompt, first, "blocked")
        try:
            assert started.wait(5)
            queued = pool.submit(client.prompt, first, "queued")
            other = pool.submit(client.prompt, second, "other")
            assert other.result(5).stop_reason == "end_turn"
            assert not queued.done()
            assert "started:queued" not in _texts(events)
        finally:
            (tmp_path / "release").touch()
        assert blocked.result(5).stop_reason == "end_turn"
        assert queued.result(5).stop_reason == "end_turn"
    assert [item["text"] for item in _transcript(tmp_path)
        if item["kind"] == "prompt" and item["session"] == first] == ["blocked", "queued"]
    assert [(scope, update.content.text) for scope, update, _ in events
        if update.session_update == "agent_message_chunk" and update.content.text == "answer:other"] == [
            (second, "answer:other")]


@pytest.mark.parametrize("choice, expected", [
    ("allow-this", {"outcome": "selected", "optionId": "allow-this"}),
    ("deny-this", {"outcome": "selected", "optionId": "deny-this"}),
    (None, {"outcome": "cancelled"}),
    ("invented-approval", {"outcome": "cancelled"}),
])
def test_permission_uses_only_offered_choices(bridge_factory, tmp_path, choice, expected):
    requests, events = [], []

    def decide(request):
        requests.append(request)
        return choice

    client = bridge_factory(on_permission=decide,
        on_update=lambda scope, update, *, replay=False: events.append((scope, update, replay)))
    session = client.new_session(cwd=str(tmp_path)).session_id
    assert client.prompt(session, "permission").stop_reason == "end_turn"
    assert requests[0].session_id == session
    assert requests[0].tool_call.tool_call_id == "permission-1"
    response = next(text.removeprefix("permission:") for text in _texts(events) if text.startswith("permission:"))
    assert json.loads(response) == expected


@pytest.mark.parametrize("prompt, callback, notification", [
    ("permission", "on_permission", "while-permission-pending"),
    ("form", "on_elicitation", "while-form-pending"),
    ("form-request", "on_elicitation", "while-form-pending"),
])
def test_pending_interaction_does_not_block_events_and_cancel_cleans_the_turn(
    bridge_factory, tmp_path, prompt, callback, notification,
):
    asked, received = Event(), Event()
    decision = Future()
    scopes = []

    def permission(request, *, session_id=None):
        scopes.append(session_id)
        asked.set()
        return decision

    def update(session, value, *, replay=False):
        if getattr(getattr(value, "content", None), "text", None) == notification:
            received.set()

    client = bridge_factory(**{callback: permission}, on_update=update)
    session = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.prompt, session, prompt)
        try:
            assert asked.wait(5) and received.wait(5)
            assert not running.done()
            client.cancel(session)
            assert running.result(5).stop_reason == "cancelled"
        finally:
            if not running.done():
                client.close()
    assert decision.cancelled()
    if callback == "on_elicitation":
        assert scopes == [session]
    assert client.prompt(session, "next").stop_reason == "end_turn"


@pytest.mark.parametrize("scope", ["form", "form-request"])
@pytest.mark.parametrize("response", [
    AcceptElicitationResponse(action="accept", content={"answer": "chosen"}),
    DeclineElicitationResponse(action="decline"),
    CancelElicitationResponse(action="cancel"),
])
def test_form_elicitation_preserves_scope_schema_and_response(bridge_factory, tmp_path, scope, response):
    requests, events = [], []

    async def answer(request, *, session_id):
        requests.append((request, session_id))
        return response

    client = bridge_factory(on_elicitation=answer,
        on_update=lambda session, update, *, replay=False: events.append((session, update, replay)))
    session = client.new_session(cwd=str(tmp_path)).session_id
    assert client.prompt(session, scope).stop_reason == "end_turn"
    request, resolved_session = requests[0]
    assert resolved_session == session
    assert request.mode == "form"
    assert request.requested_schema.required == ["answer"]
    assert getattr(request, "session_id", session) == session
    if scope == "form-request":
        assert isinstance(request.request_id, int)
    text = next(text.removeprefix("form:") for text in _texts(events) if text.startswith("form:"))
    assert json.loads(text)["action"] == response.action
    if response.action == "accept":
        assert json.loads(text)["content"] == {"answer": "chosen"}
    capabilities = _transcript(tmp_path)[0]["capabilities"]["elicitation"]
    assert "form" in capabilities and not capabilities.get("url")


@pytest.mark.parametrize("prompt", ["bad_scope", "form-stale-request", "url"])
def test_unknown_interaction_scopes_and_url_forms_do_not_reach_user_callbacks(bridge_factory, tmp_path, prompt):
    requests, events = [], []
    client = bridge_factory(on_permission=lambda request: requests.append(request) or "allow-this",
        on_elicitation=lambda request, *, session_id: requests.append(request) or AcceptElicitationResponse(action="accept"),
        on_update=lambda scope, update, *, replay=False: events.append((scope, update, replay)))
    session = client.new_session(cwd=str(tmp_path)).session_id
    assert client.prompt(session, prompt).stop_reason == "end_turn"
    assert requests == []
    text = next(text.split(":", 1)[1] for text in _texts(events) if text.startswith(("permission:", "form:")))
    result = json.loads(text)
    assert result.get("outcome", result.get("action")) in ("cancelled", "cancel")


def test_cancel_waits_for_terminal_response_and_final_update_delivery(bridge_factory, tmp_path):
    started, final = Event(), Event()
    delivered = Future()

    def update(session, value, *, replay=False):
        text = getattr(getattr(value, "content", None), "text", None)
        if text == "started:wait":
            started.set()
        if text == "cancelled-final":
            final.set()
            return delivered

    client = bridge_factory(on_update=update)
    session = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(client.prompt, session, "wait")
        try:
            assert started.wait(5)
            cancelled = pool.submit(client.cancel, session)
            assert final.wait(5)
            assert not cancelled.done()
        finally:
            delivered.set_result(None)
        assert cancelled.result(5) is None
        assert running.result(5).stop_reason == "cancelled"


def test_slow_update_callback_preserves_chunk_tool_and_plan_order(bridge_factory, tmp_path):
    arrived = Event()
    delivered = Future()
    events = []

    def update(session, value, *, replay=False):
        events.append((session, value, replay))
        if getattr(getattr(value, "content", None), "text", None) == "started:events":
            arrived.set()
            return delivered

    client = bridge_factory(on_update=update)
    session = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        prompt = pool.submit(client.prompt, session, "events")
        try:
            assert arrived.wait(5)
            assert len(events) == 1
            assert not prompt.done()
        finally:
            delivered.set_result(None)
        assert prompt.result(5).stop_reason == "end_turn"
    assert [update.session_update for _, update, _ in events] == [
        "agent_message_chunk", "tool_call", "plan", "tool_call_update", "agent_message_chunk",
    ]


@pytest.mark.parametrize("stalled_send", [False, True])
def test_cancel_timeout_closes_connection_instead_of_reusing_unknown_turn(
    bridge_factory, tmp_path, monkeypatch, stalled_send,
):
    started = Event()
    client = bridge_factory(cancel_timeout=0.15, shutdown_timeout=0.1,
        on_update=lambda session, update, *, replay=False: started.set())
    session = client.new_session(cwd=str(tmp_path)).session_id
    text = "wait" if stalled_send else "ignore_cancel"
    if stalled_send:
        # Model a stalled SDK writer without replacing the actual peer connection.
        async def cancel(**kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(client._connection, "cancel", cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.prompt, session, text)
        try:
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                client.cancel(session)
            with pytest.raises((ConnectionError, RuntimeError)):
                running.result(5)
        finally:
            client.close()
    with pytest.raises((ConnectionError, RuntimeError)):
        client.prompt(session, "never resend")
    assert [item["text"] for item in _transcript(tmp_path) if item["kind"] == "prompt"] == [text]


def test_peer_eof_fails_without_automatically_replaying_side_effects(bridge_factory, tmp_path):
    client = bridge_factory()
    session = client.new_session(cwd=str(tmp_path)).session_id
    with pytest.raises(ConnectionError):
        client.prompt(session, "crash")
    client.close()
    client.close()
    assert [item["text"] for item in _transcript(tmp_path) if item["kind"] == "prompt"] == ["crash"]


def test_initialize_failure_cleans_adapter_without_accepting_sessions(bridge_factory, tmp_path):
    client = BridgeClient(sys.executable, args=[str(tmp_path / "peer.py")], cwd=str(tmp_path),
        env={"PEER_FAIL_INITIALIZE": "1"})
    with pytest.raises(RequestError):
        client.open()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.new_session(cwd=str(tmp_path))
    with pytest.raises(ProcessLookupError):
        os.kill(client.pid, 0)


def test_stalled_sdk_transport_shutdown_is_bounded_and_reaps_owned_adapter(
    bridge_factory, monkeypatch,
):
    client = bridge_factory(shutdown_timeout=0.1)
    pid = client.pid
    released = asyncio.Event()
    sdk_exit = client._context.__aexit__

    async def exit_context(*args):
        await released.wait()
        await sdk_exit(*args)

    monkeypatch.setattr(client._context, "__aexit__", exit_context)
    with ThreadPoolExecutor(max_workers=1) as pool:
        closed = pool.submit(client.close)
        try:
            assert closed.result(3) is None
        finally:
            if not closed.done():
                client._loop.call_soon_threadsafe(released.set)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_close_is_bounded_when_peer_child_keeps_stderr_open(bridge_factory, tmp_path):
    client = bridge_factory(shutdown_timeout=0.1)
    session = client.new_session(cwd=str(tmp_path)).session_id
    assert client.prompt(session, "stderr-child").stop_reason == "end_turn"
    pid = client.pid
    finished = tmp_path / "child-finished"
    with ThreadPoolExecutor(max_workers=1) as pool:
        closed = pool.submit(client.close)
        try:
            assert closed.result(1.5) is None
            assert not finished.exists()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert client._process._transport.get_pipe_transport(2).is_closing()
        finally:
            deadline = time.monotonic() + 3
            while not finished.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
    assert finished.exists()


def test_close_cancels_interaction_future_returned_after_io_loop_has_stopped(bridge_factory, tmp_path):
    asked, return_future, released = Event(), Event(), Event()
    decision = Future()
    decision.add_done_callback(lambda future: released.set())
    scopes = []

    def pending(request, *, session_id):
        scopes.append(session_id)
        asked.set()
        assert return_future.wait(5)
        return decision

    client = bridge_factory(on_elicitation=pending)
    session = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(client.prompt, session, "form")
        try:
            assert asked.wait(5)
            closed = pool.submit(client.close)
            assert closed.result(3) is None
        finally:
            return_future.set()
        assert released.wait(3)
        assert decision.cancelled()
        assert scopes == [session]
        with pytest.raises((ConnectionError, RuntimeError)):
            running.result(5)


@pytest.mark.parametrize("prompt, callback", [("permission", "on_permission"), ("form", "on_elicitation")])
def test_close_releases_pending_interactions_and_reaps_adapter(bridge_factory, tmp_path, prompt, callback):
    asked = Event()
    decision = Future()
    scopes = []

    def pending(request, *, session_id=None):
        scopes.append(session_id)
        asked.set()
        return decision

    client = bridge_factory(**{callback: pending})
    pid = client.pid
    session = client.new_session(cwd=str(tmp_path)).session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.prompt, session, prompt)
        try:
            assert asked.wait(5)
        finally:
            client.close()
        with pytest.raises((ConnectionError, RuntimeError)):
            running.result(5)
    assert decision.cancelled()
    if callback == "on_elicitation":
        assert scopes == [session]
    client.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
