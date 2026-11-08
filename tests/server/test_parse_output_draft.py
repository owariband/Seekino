"""Parse output drafts: conservative size saving, isolation and failure behavior."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from gateway.model_events import ModelTextDelta, emit_model_text
from gateway.usage import (
    ModelCallCancelledError,
    capture_model_usage,
    model_call_context,
    track_model_call,
)
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_session_factory,
)
from server.model.dto.parseOutputDraftDTO import ParseOutputDraftState
from server.model.parse_output_draft import ParseModelOutputDraft
from server.repository.parse_output_draft import (
    ParseOutputDraftConflictError,
    ParseOutputDraftRepository,
)
from server.service.parse_output_draft import ParseOutputDraftService
from server.service.parse_usage import ParseUsageService

_TENANT = "tenant-a"
_OTHER_TENANT = "tenant-b"
_TASK = "00000000-0000-4000-8000-000000000001"
_VIDEO = "00000000-0000-4000-8000-000000000002"
_AT = datetime(2026, 11, 7, 12, 0, 0, 123_000, tzinfo=UTC)


def _context(**changes):
    return {
        "tenant_id": _TENANT, "video_parse_task_id": _TASK, "run_id": _TASK,
        "task_type": "video_parse", "category": "parse", "stage": "understand", "sequence_no": 3,
        "lease_version": 7, "actor_user_id": "user-a",
        **changes,
    }


def _delta(call_id: str, text: str, sequence: int = 1) -> ModelTextDelta:
    return ModelTextDelta(text, call_id, sequence, 0.0, 0.0, None, True)


def _run_call(
    service: ParseOutputDraftService, parts: list[str], *, context=None,
    cancel: bool = False, error: Exception | None = None,
) -> str:
    with service.capture(tenant_id=_TENANT, video_task_id=_TASK, actor_user_id="user-a"), model_call_context(context or _context()):
        with track_model_call(provider="provider", model="model", operation="understand") as tracker:
            for sequence, text in enumerate(parts, 1):
                emit_model_text(_delta(tracker.call_id, text, sequence))
            if cancel:
                tracker.cancel()
                raise ModelCallCancelledError("cancelled by test")
            if error is not None:
                raise error
            return tracker.call_id


def _state(**changes) -> ParseOutputDraftState:
    values = {
        "call_id": str(uuid4()), "tenant_id": _TENANT, "video_task_id": _TASK,
        "actor_user_id": "user-a", "text": "a" * 1024, "text_bytes": 1024,
        "status": "streaming",
        "metadata": {"provider": "provider", "model": "model", "operation": "understand",
                    "stage": "understand"},
        "started_at": _AT,
    }
    if "text" in changes and "text_bytes" not in changes:
        changes["text_bytes"] = len(changes["text"].encode("utf-8"))
    values.update(changes)
    return ParseOutputDraftState(**values)


class _RecordingDraftRepository:
    def __init__(self, failing_attempts=()):
        self.failing_attempts = set(failing_attempts)
        self.attempts = 0
        self.saves = []
        self.rows = {}

    def save(self, state):
        self.attempts += 1
        self.saves.append(state)
        if self.attempts in self.failing_attempts:
            raise RuntimeError("private database credential")
        self.rows[state.call_id] = state
        return state


class _UsageRepository:
    def record_call(self, state):
        return state


@pytest.fixture
def draft_repository():
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    ParseModelOutputDraft.__table__.create(engine)
    yield ParseOutputDraftRepository(create_session_factory(engine)), engine
    engine.dispose()


def _parse_usage(service: ParseOutputDraftService) -> ParseUsageService:
    return ParseUsageService(_UsageRepository(), drafts=service)


def _capture(usage: ParseUsageService, *, tenant_id=_TENANT, track_id=_TASK, sequence_no=0):
    lease = SimpleNamespace(video_parse_task_id=track_id, lease_version=1)
    task_input = SimpleNamespace(
        tenant_id=tenant_id, series_id="series",
        task=SimpleNamespace(video_id=_VIDEO, detail={}),
    )
    return usage.capture_video(
        lease, task_input, sequence_no=sequence_no, category="parse",
    )


def test_1023_bytes_stay_in_memory_and_1024_bytes_save_once():
    repository = _RecordingDraftRepository()
    call_id = _run_call(ParseOutputDraftService(repository), ["a" * 1023])

    assert [state.status for state in repository.saves] == ["completed"]
    assert repository.rows[call_id].text_bytes == 1023

    repository = _RecordingDraftRepository()
    call_id = _run_call(ParseOutputDraftService(repository), ["a" * 1024])

    assert [state.status for state in repository.saves] == ["streaming", "completed"]
    assert repository.saves[0].text == "a" * 1024
    assert repository.saves[0].text_bytes == 1024
    assert repository.rows[call_id].status == "completed"


def test_chinese_threshold_uses_utf8_bytes_without_truncation():
    repository = _RecordingDraftRepository()
    prefix, last = "中" * 341, "中"  # 1023 then 1026 UTF-8 bytes
    call_id = _run_call(ParseOutputDraftService(repository), [prefix, last])

    assert repository.saves[0].status == "streaming"
    assert repository.saves[0].text == prefix + last
    assert repository.saves[0].text_bytes == 1026
    assert repository.rows[call_id].text == prefix + last
    assert repository.rows[call_id].text.encode("utf-8") == (prefix + last).encode("utf-8")


def test_time_alone_does_not_flush_a_small_draft():
    repository = _RecordingDraftRepository()
    service = ParseOutputDraftService(repository)
    with service.capture(tenant_id=_TENANT, video_task_id=_TASK, actor_user_id="user-a"), model_call_context(_context()):
        with track_model_call(provider="p", model="m", operation="understand") as tracker:
            emit_model_text(_delta(tracker.call_id, "small"))
            time.sleep(0.02)
            assert repository.saves == []
    assert [state.status for state in repository.saves] == ["completed"]


def test_threshold_save_and_exit_save_keep_the_remainder_and_terminal_status():
    repository = _RecordingDraftRepository()
    call_id = _run_call(
        ParseOutputDraftService(repository), ["a" * 1024, "tail"],
    )

    assert [state.status for state in repository.saves] == ["streaming", "completed"]
    assert repository.saves[0].text == "a" * 1024
    assert repository.saves[1].text == "a" * 1024 + "tail"
    assert repository.rows[call_id].text == "a" * 1024 + "tail"


def test_empty_model_output_does_not_insert_a_row():
    repository = _RecordingDraftRepository()
    call_id = _run_call(ParseOutputDraftService(repository), [])

    assert repository.saves == [] and repository.rows == {}
    assert call_id


def test_one_delta_over_the_threshold_triggers_one_save():
    repository = _RecordingDraftRepository()
    call_id = _run_call(ParseOutputDraftService(repository), ["b" * 5_000])

    assert [state.status for state in repository.saves] == ["streaming", "completed"]
    assert repository.saves[0].text_bytes == repository.rows[call_id].text_bytes == 5_000


def test_cancelled_and_failed_calls_keep_their_prefix_without_success():
    repository = _RecordingDraftRepository()
    service = ParseOutputDraftService(repository)
    with pytest.raises(ModelCallCancelledError):
        _run_call(service, ["partial"], cancel=True)
    assert repository.saves[-1].status == "cancelled"
    assert repository.saves[-1].text == "partial"

    repository = _RecordingDraftRepository()
    service = ParseOutputDraftService(repository)
    with pytest.raises(RuntimeError, match="provider failed"):
        _run_call(service, ["partial"], error=RuntimeError("provider failed"))
    assert repository.saves[-1].status == "failed"
    assert repository.saves[-1].text == "partial"
    assert all(state.status != "completed" for state in repository.saves)


def test_middle_save_failure_disables_retries_and_allows_one_final_attempt(caplog):
    repository = _RecordingDraftRepository(failing_attempts={1})
    call_id = _run_call(
        ParseOutputDraftService(repository), ["a" * 1024] + ["b" * 1024] * 10,
    )

    assert repository.attempts == 2
    assert [state.status for state in repository.saves] == ["streaming", "completed"]
    assert repository.rows[call_id].text == "a" * 1024 + "b" * 1024 * 10
    assert "draft save failed" in caplog.text and "RuntimeError" in caplog.text
    assert "private database credential" not in caplog.text
    assert "a" * 30 not in caplog.text


def test_final_save_failure_does_not_change_the_model_result():
    repository = _RecordingDraftRepository(failing_attempts={1, 2})
    call_id = _run_call(ParseOutputDraftService(repository), ["a" * 2048])

    assert call_id and repository.attempts == 2
    assert repository.rows == {}


def test_nested_capture_scopes_do_not_double_save(draft_repository):
    repository, _engine = draft_repository
    usage = _parse_usage(ParseOutputDraftService(repository))
    with _capture(usage):
        with _capture(usage):
            with track_model_call(provider="p", model="m", operation="understand") as tracker:
                emit_model_text(_delta(tracker.call_id, "第一段正文"))
                call_id = tracker.call_id
    row = repository.get(tenant_id=_TENANT, video_task_id=_TASK, call_id=call_id)
    assert row is not None and row.text == "第一段正文" and row.status == "completed"


def test_parse_capture_through_to_thread_isolates_calls_and_tenants():
    repository = _RecordingDraftRepository()
    usage = _parse_usage(ParseOutputDraftService(repository))

    def invoke(index: int) -> None:
        with track_model_call(
            provider="provider", model=f"model-{index}", operation="understand",
        ) as tracker:
            emit_model_text(_delta(tracker.call_id, f"正文-{index}"))

    async def run(index: int) -> None:
        with _capture(
            usage, tenant_id=f"tenant-{index}", track_id=_TASK, sequence_no=index,
        ):
            await asyncio.to_thread(invoke, index)

    async def execute() -> None:
        await asyncio.gather(run(0), run(1))

    asyncio.run(execute())
    rows = list(repository.rows.values())
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {"tenant-0", "tenant-1"}
    assert {row.text for row in rows} == {"正文-0", "正文-1"}
    assert {row.status for row in rows} == {"completed"}


def test_scoped_read_and_immutable_owner(draft_repository):
    repository, _engine = draft_repository
    state = _state()
    repository.save(state)

    assert repository.get(
        tenant_id=_TENANT, video_task_id=_TASK, call_id=state.call_id,
    ).text == state.text
    assert repository.get(
        tenant_id=_OTHER_TENANT, video_task_id=_TASK, call_id=state.call_id,
    ) is None
    assert repository.get(
        tenant_id=_TENANT, video_task_id=str(uuid4()), call_id=state.call_id,
    ) is None
    with pytest.raises(ParseOutputDraftConflictError):
        repository.save(_state(
            call_id=state.call_id, tenant_id=_OTHER_TENANT,
        ))
    assert repository.get(
        tenant_id=_TENANT, video_task_id=_TASK, call_id=state.call_id,
    ).tenant_id == _TENANT


def test_terminal_rows_reject_older_or_different_snapshots(draft_repository):
    repository, _engine = draft_repository
    state = _state(text="x" * 2048)
    repository.save(state)
    repository.save(_state(
        call_id=state.call_id, text="x" * 2048, status="completed",
    ))
    with pytest.raises(ParseOutputDraftConflictError, match="terminal"):
        repository.save(_state(
            call_id=state.call_id, text="x" * 2049, status="streaming",
        ))
    with pytest.raises(ParseOutputDraftConflictError, match="terminal"):
        repository.save(_state(
            call_id=state.call_id, text="x" * 2048, status="failed",
        ))

    streaming = _state(text="y" * 4096)
    repository.save(streaming)
    with pytest.raises(ParseOutputDraftConflictError, match="older"):
        repository.save(_state(call_id=streaming.call_id, text="y" * 1000))


def test_repository_validates_identity_and_utf8_byte_count(draft_repository):
    repository, _engine = draft_repository
    with pytest.raises(ValueError, match="canonical UUID"):
        repository.save(_state(call_id="not-a-uuid"))
    with pytest.raises(ValueError, match="tenant_id"):
        repository.save(_state(tenant_id=""))
    with pytest.raises(ValueError, match="text_bytes"):
        _state(text="中", text_bytes=1)


def test_repository_sql_touches_only_the_draft_table(draft_repository):
    repository, engine = draft_repository
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        state = _state()
        repository.save(state)
        repository.get(tenant_id=_TENANT, video_task_id=_TASK, call_id=state.call_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements
    assert all("parse_model_output_draft" in statement for statement in statements)
    assert not any(
        "video_parse_task" in statement or "parse_model_call" in statement
        for statement in statements
    )


@pytest.mark.parametrize(("profile", "stream"), [
    ("kimi", True), ("kimi", False), ("deepseek", True), ("deepseek", False),
])
def test_real_gateway_mock_stream_persists_one_completed_draft(monkeypatch, draft_repository, profile, stream):
    from gateway.config import ModelBinding
    from gateway.multimodal import TextInput
    from tests.gateway.test_model_streaming import Answer, _setup

    repository, _engine = draft_repository
    usage = _parse_usage(ParseOutputDraftService(repository))
    gateway, _requests = _setup(monkeypatch, profile)
    binding = ModelBinding(
        source="selected", model="test-model", max_output_tokens=1024, stream=stream,
    )
    calls = []
    try:
        with capture_model_usage(calls.append), _capture(usage):
            model = gateway.create_generative_model(binding)
            try:
                result = model.generate(
                    [TextInput("answer")], schema_name="answer", result_type=Answer,
                )
            finally:
                model.close()
    finally:
        gateway.close()

    assert result.answer == "ok" and len(calls) == 1
    row = repository.get(
        tenant_id=_TENANT, video_task_id=_TASK, call_id=calls[0].call_id,
    )
    assert row is not None and row.status == "completed"
    assert row.text == '{"answer":"ok"}' and row.text_bytes == 15


@pytest.mark.parametrize("changes", [
    {"tenant_id": "another-tenant"},
    {"video_parse_task_id": "another-task", "run_id": "another-task"},
    {"run_id": "another-run"},
    {"actor_user_id": "another-actor"},
    {"task_type": "chat_retrieval"},
    {"assistant_message_id": "chat-message"},
])
def test_call_context_cannot_replace_the_server_owner(changes):
    repository = _RecordingDraftRepository()
    _run_call(ParseOutputDraftService(repository), ["x" * 2048], context=_context(**changes))
    assert repository.saves == []


def test_refine_generations_and_calls_keep_separate_drafts():
    repository = _RecordingDraftRepository()
    service = ParseOutputDraftService(repository)
    first = _run_call(service, ["generation-one"], context=_context(refine_generation=1))
    second = _run_call(service, ["generation-two"], context=_context(refine_generation=2))
    assert first != second
    assert repository.rows[first].metadata["refine_generation"] == 1
    assert repository.rows[second].metadata["refine_generation"] == 2
    assert repository.rows[first].text == "generation-one"


def test_different_nested_owners_restore_the_outer_capture():
    repository = _RecordingDraftRepository()
    usage = _parse_usage(ParseOutputDraftService(repository))
    with _capture(usage):
        with _capture(usage, tenant_id=_OTHER_TENANT):
            with track_model_call(provider="p", model="m", operation="generate") as inner:
                emit_model_text(_delta(inner.call_id, "inner"))
        with track_model_call(provider="p", model="m", operation="generate") as outer:
            emit_model_text(_delta(outer.call_id, "outer"))
    assert repository.rows[inner.call_id].tenant_id == _OTHER_TENANT
    assert repository.rows[outer.call_id].tenant_id == _TENANT
    assert len(repository.saves) == 2


def test_existing_prefix_cannot_be_replaced_by_unrelated_text(draft_repository):
    repository, _engine = draft_repository
    state = _state(text="original")
    repository.save(state)
    with pytest.raises(ParseOutputDraftConflictError, match="saved prefix"):
        repository.save(_state(call_id=state.call_id, text="unrelated longer text"))


@pytest.mark.parametrize("legacy_control", [False, True], ids=["local-control", "old-durable-control"])
def test_real_sse_checks_draft_thresholds_and_lease_access_counts(
    monkeypatch, draft_repository, legacy_control,
):
    import json

    import httpx
    from openai import OpenAI

    from gateway.streaming.chat import request_chat_completion
    from tests.server.test_video_parse_model_calls import _Repository, _manager

    repository, _engine = draft_repository
    usage = _parse_usage(ParseOutputDraftService(repository))
    durable_checks, saved = [], []
    reservations = _Repository()
    manager = _manager(reservations, check_active=lambda: durable_checks.append(True))
    save = repository.save

    def record_save(state):
        saved.append(state)
        return save(state)

    monkeypatch.setattr(repository, "save", record_save)
    parts = ["x" * 32] * 64 + ["尾"]
    call_id = None

    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            for index, piece in enumerate([*parts, None]):
                payload = {
                    "id": "draft", "object": "chat.completion.chunk", "created": 0, "model": "offline",
                    "choices": [{"index": 0, "delta": {"content": piece},
                                 "finish_reason": "stop" if piece is None else None}],
                }
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
                if piece is None:
                    continue
                row = repository.get(tenant_id=_TENANT, video_task_id=_TASK, call_id=call_id)
                expected_saved = min(index + 1, 64) // 32 * 1024
                if not expected_saved:
                    assert row is None
                else:
                    assert row.text_bytes == expected_saved and row.status == "streaming"
                if index == 0:
                    assert manager.heartbeat() is True
                    assert len(reservations.renewed) == 1
            yield b"data: [DONE]\n\n"

    def respond(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Wire())

    with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        with _capture(usage), manager.capture():
            with track_model_call(provider="offline", model="offline", operation="generate") as tracker:
                call_id = tracker.call_id
                response = request_chat_completion(
                    client.chat.completions, stream=True, usage_call=tracker,
                    check_control=manager.check_active if legacy_control else manager.raise_if_cancelled,
                    model="offline", messages=[{"role": "user", "content": "offline"}], timeout=10,
                )
    assert response.choices[0].message.content == "".join(parts)
    assert [(state.text_bytes, state.status) for state in saved] == [
        (1024, "streaming"), (2048, "streaming"), (2051, "completed"),
    ]
    # One admission + one explicit periodic pulse; the old callback adds 69 checks.
    assert len(durable_checks) == (71 if legacy_control else 2)
    assert len(reservations.reserved) == len(reservations.released) == 1
