from __future__ import annotations

import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event

import pytest

from gateway.config import PiHostConfig
from gateway.pi import PiClient, PiHostError


_PEER = r'''
import asyncio
import json
import os
import sys

requests = {}
tool_waiters = {}
host_config = {}

def emit(message):
    print(json.dumps(message), flush=True)

def response(command, data=None, error=None):
    emit({"type": "response", "id": command["id"], "success": error is None,
          "data": data or {}, "error": error})

def event(command, event):
    emit({"type": "event", "id": command["id"], "sessionId": command["sessionId"],
          "runId": command["runId"], "event": event})

async def prompt(command):
    try:
        text = command["message"]
        event(command, {"type": "text_delta", "delta": "start:" + command["sessionId"]})
        if text == "crash":
            await asyncio.sleep(0.1)
            os._exit(9)
        if text == "invalid":
            print("not JSON", flush=True)
            return
        if text == "wrong-owner":
            emit({"type": "event", "id": command["id"], "sessionId": "other", "event": {}})
            return
        if text in ("blocked", "event-delayed-usage"):
            await asyncio.Event().wait()
        if text == "flood":
            for _ in range(100):
                event(command, {"type": "text_delta", "delta": "x" * 512})
        if text in ("tool", "tool-delayed-usage", "tool-silent-abort"):
            waiter = asyncio.get_running_loop().create_future()
            call_id = command["runId"] + ":tool"
            tool_waiters[call_id] = waiter
            emit({"type": "tool_request", "id": command["id"], "sessionId": command["sessionId"],
                  "runId": command["runId"], "toolCallId": call_id, "name": "inspect",
                  "arguments": {"session": command["sessionId"]}})
            result = await waiter
            text = result["result"]["content"][0]["text"]
            if result.get("isError"):
                text = "error:" + text
        await asyncio.sleep(0.01)
        event(command, {"type": "state", "state": {"profile": command["profile"]["id"]}})
        response(command, {"text": text, "state": {"pid": os.getpid(), "profile": command["profile"]["id"]}})
    except asyncio.CancelledError:
        if command["message"] == "tool-silent-abort":
            await asyncio.Event().wait()
        if command["message"] in ("tool-delayed-usage", "event-delayed-usage"):
            await asyncio.sleep(0.03)
            event(command, {"type": "usage", "usage": {"input": 13, "output": 2}})
            event(command, {"type": "text_delta", "delta": "late text must be ignored"})
        response(command, error={"code": "aborted", "message": "Run cancelled"})
    finally:
        requests.pop(command["runId"], None)

async def main():
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    while line := await reader.readline():
        command = json.loads(line)
        kind = command["type"]
        if kind == "initialize":
            host_config.update(command["config"])
            os.write(2, b"ignored credentials and noisy stderr\n" * 2000)
            response(command, {"protocolVersion": int(os.environ.get("PEER_PROTOCOL", "1"))})
        elif kind == "prompt":
            requests[command["runId"]] = asyncio.create_task(prompt(command))
        elif kind == "tool_result":
            waiter = tool_waiters.pop(command["toolCallId"], None)
            if waiter and not waiter.done():
                waiter.set_result(command)
        elif kind == "abort":
            task = requests.get(command["runId"])
            if task:
                task.cancel()
            response(command)
        elif kind == "get_state":
            response(command, {"pid": os.getpid(), "active": list(requests),
                               "apiKey": os.environ.get("OPENAI_API_KEY"), "path": os.environ.get("PATH"),
                               "config": host_config})
        elif kind == "shutdown":
            for task in requests.values():
                task.cancel()
            response(command)
            return
        else:
            response(command, {"state": {"type": kind}})

asyncio.run(main())
'''


def _client(**kwargs):
    return PiClient(sys.executable, args=("-u", "-c", _PEER), startup_timeout=2,
                    cancel_timeout=0.3, shutdown_timeout=0.3, **kwargs)


def _prompt(client, session="conversation-a", run="run-a", message="echo", **kwargs):
    return client.prompt(session, run, {"id": session + ":profile", "revision": 1,
                                      "provider": "openai", "model": "fake", "apiKey": "private"},
                         message, **kwargs)


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.01)


def test_one_process_multiplexes_conversations_and_delivers_events_in_order():
    with _client() as client, ThreadPoolExecutor(max_workers=2) as executor:
        events = {"a": [], "b": []}
        first = executor.submit(_prompt, client, "a", "run-a", on_event=events["a"].append)
        second = executor.submit(_prompt, client, "b", "run-b", on_event=events["b"].append)
        a, b = first.result(3), second.result(3)
        assert a["state"]["pid"] == b["state"]["pid"] == client.pid
        assert a["state"]["profile"] == "a:profile"
        assert b["state"]["profile"] == "b:profile"
        assert events["a"] == [{"type": "text_delta", "delta": "start:a"},
                               {"type": "state", "state": {"profile": "a:profile"}}]
        assert events["b"][0]["delta"] == "start:b"
        assert client.request("confirm_turn", session_id="a", run_id="run-a", content="saved") == {
            "state": {"type": "confirm_turn"},
        }


def test_child_environment_removes_model_credentials_and_drains_stderr(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-secret")
    with _client(env={"OPENAI_API_KEY": "also-filtered"}) as client:
        state = client.request("get_state", session_id="a")
        assert state["apiKey"] is None
        assert state["path"] == os.environ["PATH"]
        _wait_for(lambda: client.stderr_bytes > 65536)


def test_partial_config_sends_complete_shared_defaults_to_host(tmp_path):
    overrides = {"sessionDir": str(tmp_path), "maxActiveRuns": 3}
    with _client(config=overrides) as client:
        received = client.request("get_state", session_id="a")["config"]
        assert received == {**PiHostConfig().runtime_options(), **overrides}


def test_tools_and_events_keep_the_callers_context_and_reader_remains_responsive():
    scope = ContextVar("pi-test-scope", default="missing")
    entered, release = Event(), Event()
    observed = []

    def tool(name, arguments, call_id):
        observed.append((scope.get(), name, arguments, call_id))
        entered.set()
        assert release.wait(2)
        return {"content": [{"type": "text", "text": scope.get()}]}

    def run(client):
        scope.set("user-a")
        return _prompt(client, message="tool", on_tool=tool,
                       on_event=lambda _event: observed.append(scope.get()))

    with _client() as client, ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, client)
        assert entered.wait(2)
        try:
            assert client.request("get_state", session_id="b")["pid"] == client.pid
        finally:
            release.set()
        assert future.result(3)["text"] == "user-a"
        assert observed == ["user-a", ("user-a", "inspect", {"session": "conversation-a"}, "run-a:tool"), "user-a"]


def test_async_callbacks_keep_the_callers_context():
    scope = ContextVar("pi-async-scope", default="missing")
    observed = []

    async def event(_event):
        await asyncio.sleep(0)
        observed.append(scope.get())

    async def tool(*_args):
        await asyncio.sleep(0)
        return {"content": [{"type": "text", "text": scope.get()}]}

    with _client() as client:
        token = scope.set("async-user")
        try:
            assert _prompt(client, message="tool", on_event=event, on_tool=tool)["text"] == "async-user"
        finally:
            scope.reset(token)
        assert observed == ["async-user", "async-user"]


def test_cancelled_sync_tool_holds_callback_capacity_until_it_really_finishes():
    entered, release, second_entered = Event(), Event(), Event()

    def blocked(*_args):
        entered.set()
        assert release.wait(3)
        return {"content": [{"type": "text", "text": "released"}]}

    def next_tool(*_args):
        second_entered.set()
        return {"content": [{"type": "text", "text": "next"}]}

    with _client(config={"maxModelRequests": 1}) as client, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_prompt, client, message="tool", on_tool=blocked, timeout=0.15)
        assert entered.wait(2)
        try:
            with pytest.raises(PiHostError) as error:
                first.result(2)
            assert error.value.code == "timeout"
            second = executor.submit(_prompt, client, "b", "run-b", "tool", on_tool=next_tool)
            assert not second_entered.wait(0.1)
            assert client.request("get_state")["pid"] == client.pid
        finally:
            release.set()
        assert second.result(2)["text"] == "next"


def test_close_fails_active_waiters_and_reaps_the_child():
    started = Event()
    client = _client()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_prompt, client, message="blocked", on_event=lambda _event: started.set())
        assert started.wait(2)
        client.close()
        with pytest.raises(PiHostError):
            future.result(2)
        assert client.pid is None


def test_cancel_one_run_preserves_the_other_conversation_and_process():
    stopped, started = Event(), Event()

    class StopRequested(Exception):
        pass

    def check():
        if stopped.is_set():
            raise StopRequested("user cancelled")

    with _client() as client, ThreadPoolExecutor(max_workers=1) as executor:
        pid = client.pid
        future = executor.submit(_prompt, client, message="blocked", check_control=check,
                                 on_event=lambda _event: started.set())
        assert started.wait(2)
        stopped.set()
        with pytest.raises(StopRequested, match="user cancelled"):
            future.result(2)
        assert _prompt(client, "b", "run-b")["state"]["pid"] == pid
        assert client.request("get_state")["active"] == []


def test_prompt_timeout_aborts_only_its_run():
    with _client() as client:
        pid = client.pid
        with pytest.raises(PiHostError) as error:
            _prompt(client, message="blocked", timeout=0.1)
        assert error.value.code == "timeout"
        assert _prompt(client, run="run-next")["state"]["pid"] == pid


def test_crash_fails_all_waiters_and_next_request_starts_a_fresh_host():
    started = Event()
    with _client() as client, ThreadPoolExecutor(max_workers=2) as executor:
        pid = client.pid
        waiting = executor.submit(_prompt, client, message="blocked", on_event=lambda _event: started.set())
        assert started.wait(2)
        crash = executor.submit(_prompt, client, "b", "crash-run", "crash")
        for future in (waiting, crash):
            with pytest.raises(PiHostError) as error:
                future.result(3)
            assert error.value.code == "host_exited"
        assert _prompt(client, run="new-run")["state"]["pid"] != pid


@pytest.mark.parametrize("message", ["invalid", "wrong-owner"])
def test_bad_protocol_fails_the_generation_and_can_restart(message):
    with _client() as client:
        with pytest.raises(PiHostError) as error:
            _prompt(client, message=message)
        assert error.value.code == "protocol_error"
        assert _prompt(client, run="next")["text"] == "echo"


def test_unsupported_protocol_releases_process_and_close_is_idempotent():
    client = _client(env={"PEER_PROTOCOL": "2"})
    with pytest.raises(PiHostError, match="Unsupported"):
        client.open()
    assert client.pid is None
    client.close()
    client.close()
    with pytest.raises(PiHostError) as error:
        client.request("get_state")
    assert error.value.code == "closed"


def test_tool_exceptions_remain_fatal_and_tool_errors_are_explicit_results():
    class ToolFailure(Exception):
        pass

    def failing(*_args):
        raise ToolFailure("cancel this business operation")

    def ordinary_error(*_args):
        return {"content": [{"type": "text", "text": "Unable to inspect"}], "isError": True}

    with _client() as client:
        with pytest.raises(ToolFailure, match="cancel this business operation"):
            _prompt(client, message="tool", on_tool=failing)
        _wait_for(lambda: client.request("get_state")["active"] == [])
        result = _prompt(client, run="next", message="tool", on_tool=ordinary_error)
        assert result["text"] == "error:Unable to inspect"


def test_oversized_tool_output_ends_the_run_without_sending_it():

    def oversized(*_args):
        return {"content": [{"type": "text", "text": "x" * 4096}]}

    with _client(config={"maxToolOutputBytes": 1024}) as client:
        with pytest.raises(PiHostError) as error:
            _prompt(client, message="tool", on_tool=oversized)
        assert error.value.code == "tool_output_too_large"
        _wait_for(lambda: client.request("get_state")["active"] == [])


@pytest.mark.parametrize("message", ["tool-delayed-usage", "event-delayed-usage"])
def test_callback_failure_drains_final_usage_and_preserves_original_exception(message):
    original = RuntimeError("original business failure")
    usage, text = [], []

    def event(value):
        if value["type"] == "usage":
            usage.append(value["usage"])
        elif message == "event-delayed-usage":
            raise original
        else:
            text.append(value.get("delta"))

    def tool(*_args):
        raise original

    with _client() as client:
        pid = client.pid
        with pytest.raises(RuntimeError) as error:
            _prompt(client, message=message, on_event=event, on_tool=tool)
        assert error.value is original
        assert usage == [{"input": 13, "output": 2}]
        assert "late text must be ignored" not in text
        assert _prompt(client, run="next")["state"]["pid"] == pid


def test_callback_failure_returns_original_error_when_abort_never_finishes():
    original = ValueError("tool failed")

    def tool(*_args):
        raise original

    with _client() as client:
        started = time.monotonic()
        with pytest.raises(ValueError) as error:
            _prompt(client, message="tool-silent-abort", on_tool=tool)
        assert error.value is original
        assert time.monotonic() - started < 1
        assert _prompt(client, "b", "run-b")["state"]["pid"] == client.pid


def test_oversized_input_rejected_without_stopping_host():
    with _client(config={"maxLineBytes": 1024}) as client:
        pid = client.pid
        with pytest.raises(PiHostError) as error:
            _prompt(client, message="x" * 2048)
        assert error.value.code == "message_too_large"
        assert _prompt(client)["state"]["pid"] == pid


def test_incoming_backpressure_is_bounded_and_does_not_kill_host():
    entered, release = Event(), Event()

    def blocked(_event):
        entered.set()
        assert release.wait(2)

    with _client(config={"maxQueueBytes": 4096}) as client, ThreadPoolExecutor(max_workers=1) as executor:
        pid = client.pid
        future = executor.submit(_prompt, client, message="flood", on_event=blocked)
        assert entered.wait(2)
        try:
            with pytest.raises(PiHostError) as error:
                future.result(2)
            assert error.value.code == "backpressure"
        finally:
            release.set()
        assert _prompt(client, run="next")["state"]["pid"] == pid


def test_close_without_start_is_safe():
    client = _client()
    client.close()
    client.close()
    assert client.pid is None
