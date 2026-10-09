from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.api import agent as agent_api
from server.api import jobs as jobs_api
from server.config.parse_profile import default_video_parse_v2_config
from server.config.sse import SSEConfig
from server.model.dto.agentDTO import AgentMessageState
from server.model.dto.jobDTO import (
    ParseJobDetail,
    ParseJobState,
    VideoParseTaskDetail,
    VideoParseTaskState,
    VideoChunkCheckpointState,
)
from server.repository.agent import AgentConversationNotFoundError
from server.utils.time import timedelta_to_ms

_NOW = 1_789_454_400_000
_CONVERSATION_ID = "conversation-1"
_TENANT_ID = "workspace-1"


class _Request:
    def __init__(self, disconnect_after: int) -> None:
        self.disconnect_after = disconnect_after
        self.checks = 0
        self.app = SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(auth=None)))
        self.state = SimpleNamespace()
        self.cookies = {}
        self.headers = {}
        self.method = "GET"

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
        series_ids=("series-1",),
        sequence_no=sequence_no,
        role="ASSISTANT" if is_assistant else "USER",
        status=status,
        content=None if status == "PROCESSING" else f"message {sequence_no}",
        model_name="chat-model-v1" if is_assistant else None,
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
        tenant_id: str,
        owner_user_id: str = "local-user",
    ) -> list[AgentMessageState]:
        index = min(len(self.calls), len(self.snapshots) - 1)
        self.calls.append((conversation_id, tenant_id))
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
                _Request(1), service, _CONVERSATION_ID, _TENANT_ID, SSEConfig()
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
    assert all(message["seriesIds"] == ["series-1"] for message in payload["messages"])
    assert service.calls == [(_CONVERSATION_ID, _TENANT_ID)]


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
        last_error={"code": "PROCESSING_FAILED", "message": "model unavailable"} if final_status == "FAILED" else None,
        version=processing.version + 1,
        updated_at=_NOW + timedelta_to_ms(timedelta(seconds=2)),
    )
    service = _AgentService([[user, processing], [user, processing], [user, final]])

    events = asyncio.run(
        _collect(
            agent_api._agent_message_events(
                _Request(3), service, _CONVERSATION_ID, _TENANT_ID, SSEConfig()
            )
        )
    )

    assert len(events) == 2
    payloads = [_payload(event) for event in events]
    assert all(payload["type"] == "messages" and isinstance(payload["occurredAt"], int) for payload in payloads)
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
                _Request(polls), service, _CONVERSATION_ID, _TENANT_ID, config
            )
        )
    )

    assert len([event for event in events if event.startswith("event: messages")]) == 1
    assert events.count(": keep-alive\n\n") == 1
    assert clock.sleeps == [poll_seconds] * (polls - 1)


def test_agent_sse_ends_normally_when_conversation_is_deleted(monkeypatch) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)

    class _DeletedConversationService:
        @staticmethod
        def list_all_messages(conversation_id: str, *, tenant_id: str, owner_user_id: str = "local-user"):
            raise AgentConversationNotFoundError(
                f"conversationId {conversation_id!r} does not exist in {tenant_id!r}"
            )

    events = asyncio.run(
        _collect(
            agent_api._agent_message_events(
                _Request(1),
                _DeletedConversationService(),
                _CONVERSATION_ID,
                _TENANT_ID,
                SSEConfig(),
            )
        )
    )

    assert events == []


def _job_detail() -> ParseJobDetail:
    return ParseJobDetail(
        job=ParseJobState(
            job_id="job-1",
            tenant_id=_TENANT_ID,
            version=1,
            series_id="series-1",
            video_id="video-1",
            series_name="Test Series",
            episode_order=1,
            original_filename="episode.mp4",
            pipeline_version="m1",
            duration_seconds=140,
            status="RUNNING",
            stage="PARSING",
            progress=30,
            attempt_count=1,
            max_attempts=3,
            available_at=_NOW,
            lease_owner="worker-1",
            lease_version=1,
            lease_expires_at=_NOW + timedelta_to_ms(timedelta(minutes=5)),
            heartbeat_at=_NOW,
            started_at=_NOW,
            completed_at=None,
            processing_duration_ms=0,
            processing_timing_complete=True,
            last_error=None,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        checkpoints=(),
    )


def _checkpoint(
    sequence_no: int,
    start_seconds: int | None,
    end_seconds: int | None,
    stage: str,
) -> VideoChunkCheckpointState:
    return VideoChunkCheckpointState(
        job_id="job-1",
        sequence_no=sequence_no,
        video_chunk_id=f"chunk-{sequence_no}",
        stage=stage,
        chunk_start_seconds=start_seconds,
        chunk_end_seconds=end_seconds,
    )


def test_job_projection_keeps_semantic_parse_progress_across_later_stages() -> None:
    detail = replace(
        _job_detail(),
        job=replace(_job_detail().job, stage="TEXT_EMBEDDING"),
        checkpoints=(
            _checkpoint(0, 0, 60, "TEXT_EMBEDDED"),
            _checkpoint(1, 60, 120, "PARSED"),
            _checkpoint(2, 120, 140, "SPLIT_READY"),
        ),
    )

    payload = jobs_api.project_job(detail).model_dump(by_alias=True)

    assert payload["progress"] == 30
    assert payload["overallProgress"] == 85
    assert payload["stageProgress"] == 42
    assert payload["completedDurationSeconds"] == 120
    assert payload["completedThroughSeconds"] == 120
    assert payload["totalDurationSeconds"] == 140
    assert payload["completedChunkCount"] == 2
    assert payload["totalChunkCount"] == 3
    assert payload["currentChunk"] == {
        "sequenceNo": 2,
        "startSeconds": 120,
        "endSeconds": 140,
    }
    assert payload["checkpoints"][2]["chunkStartSeconds"] == 120
    assert payload["checkpoints"][2]["chunkEndSeconds"] == 140


def test_job_projection_does_not_guess_from_incomplete_checkpoint_ranges() -> None:
    detail = replace(
        _job_detail(),
        checkpoints=(
            _checkpoint(0, 0, 60, "PARSED"),
            _checkpoint(1, None, None, "SPLIT_READY"),
        ),
    )

    payload = jobs_api.project_job(detail).model_dump(by_alias=True)

    assert payload["overallProgress"] is None
    assert payload["stageProgress"] is None
    assert payload["completedDurationSeconds"] is None
    assert payload["completedThroughSeconds"] is None
    assert payload["completedChunkCount"] is None
    assert payload["currentChunk"] is None


def test_succeeded_job_reports_complete_even_for_legacy_checkpoints() -> None:
    detail = replace(
        _job_detail(),
        job=replace(
            _job_detail().job,
            status="SUCCEEDED",
            stage="COMPLETED",
            progress=100,
        ),
        checkpoints=(_checkpoint(0, None, None, "INDEXED"),),
    )

    payload = jobs_api.project_job(detail).model_dump(by_alias=True)

    assert payload["overallProgress"] == 100
    assert payload["stageProgress"] == 100
    assert payload["completedDurationSeconds"] == 140
    assert payload["completedThroughSeconds"] == 140
    assert payload["completedChunkCount"] == payload["totalChunkCount"] == 1


class _JobService:
    def __init__(self, snapshots: list[ParseJobDetail]) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    def get(self, job_id: str, *, tenant_id: str) -> ParseJobDetail:
        index = min(len(self.calls), len(self.snapshots) - 1)
        self.calls.append((job_id, tenant_id))
        return self.snapshots[index]


def _video_parse_task_detail(
    *,
    status: str = "PARSING",
) -> VideoParseTaskDetail:
    now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    return VideoParseTaskDetail(
        task=VideoParseTaskState(
            video_parse_task_id="video-parse-task-1",
            video_id="video-1",
            pipeline_version="video-parse-v2",
            status=status,
            parse_config=default_video_parse_v2_config().model_dump(mode="json"),
            chunk_count=2,
            version=1,
            next_execute_time=now,
            attempt_count=1,
            max_attempts=3,
            lease_owner="worker-1" if status == "PARSING" else None,
            lease_version=1,
            lease_expire_time=(now + timedelta(minutes=5) if status == "PARSING" else None),
            heartbeat_time=now if status == "PARSING" else None,
            start_time=now,
            finish_time=now if status == "COMPLETE" else None,
            processing_duration_ms=1_000,
            processing_timing_complete=True,
            last_error=None,
            is_test=False,
            detail={
                "source": "upload",
                "_worker_runtime_v2": {"active": {"private": True}},
            },
            create_time=now,
            update_time=now,
        ),
        tenant_id=_TENANT_ID,
        series_id="series-1",
        series_name="Test Series",
        episode_order=1,
        original_filename="episode.mp4",
        duration_seconds=120,
        upload_session_id="upload-1",
        is_latest=True,
    )


class _VideoParseTaskService:
    def __init__(self, snapshots: list[VideoParseTaskDetail]) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    def get_video_parse_task(
        self,
        video_parse_task_id: str,
        *,
        tenant_id: str,
    ) -> VideoParseTaskDetail:
        index = min(len(self.calls), len(self.snapshots) - 1)
        self.calls.append((video_parse_task_id, tenant_id))
        return self.snapshots[index]


def test_video_parse_task_sse_ignores_heartbeat_and_closes_on_terminal(
    monkeypatch,
) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    initial = _video_parse_task_detail()
    heartbeat = replace(
        initial,
        task=replace(
            initial.task,
            heartbeat_time=initial.task.heartbeat_time + timedelta(seconds=1),
            lease_expire_time=initial.task.lease_expire_time + timedelta(minutes=1),
            detail={
                "source": "upload",
                "_worker_runtime_v2": {"active": {"private": False}},
            },
            update_time=initial.task.update_time + timedelta(seconds=1),
        ),
    )
    terminal = replace(
        heartbeat,
        task=replace(
            heartbeat.task,
            status="COMPLETE",
            lease_owner=None,
            lease_expire_time=None,
            heartbeat_time=None,
            finish_time=heartbeat.task.update_time + timedelta(seconds=1),
            update_time=heartbeat.task.update_time + timedelta(seconds=1),
        ),
    )
    service = _VideoParseTaskService([heartbeat, terminal])

    events = asyncio.run(
        _collect(
            jobs_api._video_parse_task_events(
                _Request(3),
                service,
                initial.task.video_parse_task_id,
                _TENANT_ID,
                initial,
                SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=15),
            )
        )
    )

    assert len(events) == 2
    assert all(event.startswith("event: video-parse-task\n") for event in events)
    assert [_payload(event)["status"] for event in events] == ["PARSING", "COMPLETE"]
    assert all(_payload(event)["detail"] == {"source": "upload"} for event in events)
    assert _payload(events[-1])["canReparse"] is True
    assert service.calls == [
        (initial.task.video_parse_task_id, _TENANT_ID),
        (initial.task.video_parse_task_id, _TENANT_ID),
    ]
    assert clock.sleeps == [0.5, 0.5]


def test_video_parse_task_projection_hides_internal_failure_detail() -> None:
    detail = _video_parse_task_detail()
    failed = replace(
        detail,
        task=replace(
            detail.task,
            status="INDEXING_FAILED",
            last_error={
                "code": "ES_BULK_REJECTED",
                "message": "private key at https://internal/provider",
            },
        ),
    )

    payload = jobs_api.project_video_parse_task(failed).model_dump(
        mode="json", by_alias=True,
    )

    assert payload["lastError"] == {
        "code": "PROCESSING_FAILED",
        "message": "检索索引发布失败，请重试",
    }
    assert "internal/provider" not in str(payload)


def test_video_parse_task_actions_wait_for_active_prefix_lease() -> None:
    detail = _video_parse_task_detail()
    terminal = replace(
        detail,
        task=replace(detail.task, status="COMPLETE"),
    )

    assert jobs_api.project_video_parse_task(terminal).can_reparse is True
    fenced = replace(
        terminal,
        has_active_prefix_lease=True,
        has_active_prefix_suffix_lease=True,
    )
    view = jobs_api.project_video_parse_task(fenced)

    assert view.can_retry is False
    assert view.can_reparse is False

    later_prefix = replace(
        terminal,
        task=replace(terminal.task, status="PARSING_FAILED"),
        has_active_prefix_suffix_lease=True,
    )
    later_view = jobs_api.project_video_parse_task(later_prefix)
    assert later_view.can_retry is True
    assert later_view.can_reparse is False

    executing = replace(
        terminal,
        task=replace(terminal.task, status="PARSING_FAILED"),
        has_active_execution_lease=True,
    )
    executing_view = jobs_api.project_video_parse_task(executing)
    assert executing_view.can_retry is False
    assert executing_view.can_reparse is True


def test_unaccepted_pending_video_parse_task_can_be_taken_over() -> None:
    pending = replace(
        _video_parse_task_detail(status="PENDING"),
        task=replace(
            _video_parse_task_detail(status="PENDING").task,
            detail={"_worker_runtime_v2": {"accepted": False}},
        ),
        pending_takeover_enabled=True,
    )

    assert jobs_api.project_video_parse_task(pending).can_reparse is True
    accepted = replace(
        pending,
        task=replace(
            pending.task,
            detail={"_worker_runtime_v2": {"accepted": True}},
        ),
    )
    assert jobs_api.project_video_parse_task(accepted).can_reparse is False


@pytest.mark.parametrize(
    "lease_fences",
    [
        {
            "has_active_prefix_lease": True,
            "has_active_prefix_suffix_lease": True,
        },
        {"has_active_execution_lease": True},
    ],
)
def test_video_parse_task_sse_closes_at_terminal_status(
    monkeypatch,
    lease_fences,
) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    terminal = replace(
        _video_parse_task_detail(status="PARSING_FAILED"),
        **lease_fences,
    )
    service = _VideoParseTaskService([terminal])

    events = asyncio.run(_collect(jobs_api._video_parse_task_events(
        _Request(2),
        service,
        terminal.task.video_parse_task_id,
        _TENANT_ID,
        terminal,
        SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=15),
    )))

    assert len(events) == 1
    assert _payload(events[0])["status"] == "PARSING_FAILED"
    assert service.calls == []
    assert clock.sleeps == []


def test_job_sse_ignores_heartbeat_but_emits_progress(monkeypatch) -> None:
    clock = _Clock()
    _patch_runtime(monkeypatch, jobs_api, clock)
    initial = _job_detail()
    heartbeat = replace(
        initial,
        job=replace(
            initial.job,
            heartbeat_at=_NOW + timedelta_to_ms(timedelta(seconds=1)),
            lease_expires_at=_NOW + timedelta_to_ms(timedelta(minutes=6)),
            updated_at=_NOW + timedelta_to_ms(timedelta(seconds=1)),
        ),
    )
    progress = replace(heartbeat, job=replace(heartbeat.job, progress=40))
    service = _JobService([heartbeat, progress])
    config = SSEConfig(poll_interval_seconds=0.5, keepalive_interval_seconds=15)

    events = asyncio.run(
        _collect(
            jobs_api._job_events(
                _Request(3), service, initial.job.job_id, _TENANT_ID, initial, config
            )
        )
    )

    assert len(events) == 2
    assert all(event.startswith("event: job\n") for event in events)
    assert [_payload(event)["progress"] for event in events] == [30, 40]
    assert service.calls == [(initial.job.job_id, _TENANT_ID)] * 2
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
                _Request(5), service, initial.job.job_id, _TENANT_ID, initial, config
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
                _Request(100), service, terminal.job.job_id, _TENANT_ID,
                terminal if already_terminal else detail, SSEConfig(),
            )
        )
    )

    assert len(events) == (1 if already_terminal else 2)
    assert _payload(events[-1])["status"] == terminal_status
    assert service.calls == (
        [] if already_terminal else [(terminal.job.job_id, _TENANT_ID)]
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
         "messageId": "message-1", "sequence": 1, "occurredAt": 1789473600000, "delta": "用户正文"},
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
    assert _payload(events[1])["occurredAt"] == 1789473600000
    assert "hidden" not in "".join(events)


def test_answer_stream_disconnect_only_detaches_consumer(monkeypatch):
    clock = _Clock()
    _patch_runtime(monkeypatch, agent_api, clock)
    execution = _AnswerExecution([], clock)
    events = asyncio.run(_collect(agent_api._answer_events(_Request(1), execution, SSEConfig())))
    assert events == [] and execution.detached
