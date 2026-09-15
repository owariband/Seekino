from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from server.api import agent as agent_api
from server.api import jobs as jobs_api
from server.config.sse import SSEConfig
from server.repository.agent import AgentMessageState
from server.repository.job import ParseJobState
from server.service.job import ParseJobDetail

_NOW = datetime(2026, 9, 15, 12)
_CONVERSATION_ID = "conversation-1"
_WORKSPACE_ID = "workspace-1"


class _Request:
    def __init__(self, disconnect_after: int) -> None:
        self.disconnect_after = disconnect_after
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks >= self.disconnect_after


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _patch_runtime(monkeypatch, module, clock: _Clock) -> None:
    async def run_directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(module, "run_in_threadpool", run_directly)


async def _collect(events) -> list[str]:
    return [event async for event in events]


def _payload(event: str) -> dict:
    return json.loads(event.split("\ndata: ", 1)[1].strip())


def _message(sequence_no: int, *, status: str = "COMPLETED") -> AgentMessageState:
    is_assistant = sequence_no % 2 == 1
    return AgentMessageState(
        message_id=f"message-{sequence_no}",
        conversation_id=_CONVERSATION_ID,
        sequence_no=sequence_no,
        role="ASSISTANT" if is_assistant else "USER",
        status=status,
        content=None if status == "PROCESSING" else f"message {sequence_no}",
        model_name="chat-model-v1" if is_assistant else None,
        reply_to_message_id=f"message-{sequence_no - 1}" if is_assistant else None,
        retrieval_query=None,
        search_result=None,
        last_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _AgentService:
    def __init__(
        self,
        snapshots: list[list[AgentMessageState]],
        *,
        clock: _Clock | None = None,
        query_seconds: float = 0,
    ) -> None:
        self.snapshots = snapshots
        self.clock = clock
        self.query_seconds = query_seconds
        self.calls: list[tuple[str, str]] = []

    def list_all_messages(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
    ) -> list[AgentMessageState]:
        index = min(len(self.calls), len(self.snapshots) - 1)
        self.calls.append((conversation_id, workspace_id))
        if self.clock is not None:
            self.clock.now += self.query_seconds
        return self.snapshots[index]


def test_agent_sse_initial_snapshot_includes_messages_after_500(monkeypatch) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    service = _AgentService([[_message(index) for index in range(502)]])

    events = asyncio.run(
        _collect(
            agent_api._agent_message_events(
                _Request(1), service, _CONVERSATION_ID, _WORKSPACE_ID, SSEConfig()
            )
        )
    )

    assert len(events) == 1
    assert events[0].startswith("event: messages\n")
    payload = _payload(events[0])
    assert payload["conversationId"] == _CONVERSATION_ID
    assert len(payload["messages"]) == 502
    assert payload["messages"][-1]["sequenceNo"] == 501
    assert payload["messages"][-1]["modelName"] == "chat-model-v1"
    assert payload["messages"][0]["modelName"] is None
    assert service.calls == [(_CONVERSATION_ID, _WORKSPACE_ID)]


@pytest.mark.parametrize("final_status", ["COMPLETED", "FAILED"])
def test_agent_sse_emits_same_sequence_status_updates(monkeypatch, final_status) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    user = _message(0)
    processing = _message(1, status="PROCESSING")
    final = replace(
        processing,
        status=final_status,
        content="verified answer" if final_status == "COMPLETED" else None,
        last_error="model unavailable" if final_status == "FAILED" else None,
        updated_at=_NOW + timedelta(seconds=2),
    )
    service = _AgentService([[user, processing], [user, processing], [user, final]])

    events = asyncio.run(
        _collect(
            agent_api._agent_message_events(
                _Request(3), service, _CONVERSATION_ID, _WORKSPACE_ID, SSEConfig()
            )
        )
    )

    assert len(events) == 2
    payloads = [_payload(event) for event in events]
    assert [item["messages"][1]["status"] for item in payloads] == [
        "PROCESSING",
        final_status,
    ]
    assert all(len(item["messages"]) == 2 for item in payloads)
    assert all(item["messages"][1]["sequenceNo"] == 1 for item in payloads)
    assert payloads[-1]["messages"][1]["modelName"] == "chat-model-v1"
    assert payloads[-1]["messages"][1]["lastError"] == final.last_error


@pytest.mark.parametrize(
    ("poll_seconds", "query_seconds", "polls"),
    [(4.0, 0.0, 4), (0.25, 4.0, 3)],
)
def test_agent_sse_keepalive_uses_elapsed_time_not_poll_count(
    monkeypatch, poll_seconds, query_seconds, polls
) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    service = _AgentService([[_message(0)]], clock=clock, query_seconds=query_seconds)
    config = SSEConfig(
        poll_interval_seconds=poll_seconds,
        keepalive_interval_seconds=7.0,
    )

    events = asyncio.run(
        _collect(
            agent_api._agent_message_events(
                _Request(polls), service, _CONVERSATION_ID, _WORKSPACE_ID, config
            )
        )
    )

    assert len([event for event in events if event.startswith("event: messages")]) == 1
    assert events.count(": keep-alive\n\n") == 1
    assert clock.sleeps == [poll_seconds] * (polls - 1)


def _job_detail() -> ParseJobDetail:
    return ParseJobDetail(
        job=ParseJobState(
            job_id="job-1",
            series_id="series-1",
            video_id="video-1",
            series_name="Test Series",
            episode_order=1,
            original_filename="episode.mp4",
            pipeline_version="m1",
            status="RUNNING",
            stage="PARSING",
            progress=30,
            attempt_count=1,
            max_attempts=3,
            available_at=_NOW,
            lease_owner="worker-1",
            lease_version=1,
            lease_expires_at=_NOW + timedelta(minutes=5),
            heartbeat_at=_NOW,
            started_at=_NOW,
            completed_at=None,
            last_error=None,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        checkpoints=(),
    )


class _JobService:
    def __init__(self, snapshots: list[ParseJobDetail]) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    def get(self, job_id: str, *, workspace_id: str) -> ParseJobDetail:
        index = min(len(self.calls), len(self.snapshots) - 1)
        self.calls.append((job_id, workspace_id))
        return self.snapshots[index]


def test_job_sse_ignores_heartbeat_but_emits_progress(monkeypatch) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    initial = _job_detail()
    heartbeat = replace(
        initial,
        job=replace(
            initial.job,
            heartbeat_at=_NOW + timedelta(seconds=1),
            lease_expires_at=_NOW + timedelta(minutes=6),
            updated_at=_NOW + timedelta(seconds=1),
        ),
    )
    progress = replace(heartbeat, job=replace(heartbeat.job, progress=40))
    service = _JobService([heartbeat, progress])
    config = SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=15)

    events = asyncio.run(
        _collect(
            jobs_api._job_events(
                _Request(3), service, initial.job.job_id, _WORKSPACE_ID, initial, config
            )
        )
    )

    assert len(events) == 2
    assert all(event.startswith("event: job\n") for event in events)
    assert [_payload(event)["progress"] for event in events] == [30, 40]
    assert service.calls == [(initial.job.job_id, _WORKSPACE_ID)] * 2
    assert clock.sleeps == [0.5, 0.5]


def test_job_sse_keepalive_uses_configured_elapsed_time(monkeypatch) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    initial = _job_detail()
    service = _JobService([initial])
    config = SSEConfig(poll_interval_seconds=2.0, keepalive_interval_seconds=5.0)

    events = asyncio.run(
        _collect(
            jobs_api._job_events(
                _Request(5), service, initial.job.job_id, _WORKSPACE_ID, initial, config
            )
        )
    )

    assert len([event for event in events if event.startswith("event: job")]) == 1
    assert events.count(": keep-alive\n\n") == 1
    assert clock.sleeps == [2.0] * 4


@pytest.mark.parametrize("terminal_status", ["SUCCEEDED", "FAILED"])
@pytest.mark.parametrize("already_terminal", [True, False])
def test_job_sse_emits_terminal_snapshot_then_closes(
    monkeypatch, terminal_status, already_terminal
) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    detail = _job_detail()
    terminal = replace(
        detail,
        job=replace(
            detail.job,
            status=terminal_status,
            progress=100 if terminal_status == "SUCCEEDED" else 30,
            completed_at=_NOW,
        ),
    )
    service = _JobService([terminal])

    events = asyncio.run(
        _collect(
            jobs_api._job_events(
                _Request(100), service, terminal.job.job_id, _WORKSPACE_ID,
                terminal if already_terminal else detail, SSEConfig(),
            )
        )
    )

    assert len(events) == (1 if already_terminal else 2)
    assert _payload(events[-1])["status"] == terminal_status
    assert service.calls == (
        [] if already_terminal else [(terminal.job.job_id, _WORKSPACE_ID)]
    )
    assert clock.sleeps == ([] if already_terminal else [1.0])


class _AnswerExecution:
    def __init__(self, events, clock):
        self.events = list(events)
        self.clock = clock
        self.detached = False

    def next_event(self, timeout):
        from queue import Empty
        self.clock.now += timeout
        event = self.events.pop(0) if self.events else None
        if event == "idle":
            raise Empty
        return event

    def detach(self):
        self.detached = True


def test_answer_stream_whitelists_events_keeps_alive_and_detaches(monkeypatch):
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    execution = _AnswerExecution([
        {"type": "internal.plan", "private": "hidden"},
        "idle", "idle",
        {"type": "answer.delta", "conversationId": _CONVERSATION_ID,
         "messageId": "message-1", "sequence": 1, "delta": "用户正文"},
        None,
    ], clock)
    events = asyncio.run(_collect(agent_api._answer_events(
        _Request(100), execution,
        SSEConfig(poll_interval_seconds=0.1, keepalive_interval_seconds=2),
    )))
    assert execution.detached
    assert events[0] == ": keep-alive\n\n"
    assert len(events) == 2 and events[1].startswith("event: answer.delta\nid: message-1:1\n")
    assert _payload(events[1])["delta"] == "用户正文"
    assert "hidden" not in "".join(events)


def test_answer_stream_disconnect_only_detaches_consumer(monkeypatch):
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    execution = _AnswerExecution([], clock)
    events = asyncio.run(_collect(agent_api._answer_events(_Request(1), execution, SSEConfig())))
    assert events == [] and execution.detached
