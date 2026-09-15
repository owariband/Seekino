from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty
from threading import Event
from uuid import uuid4

import pytest

from server.agent.runtime import AgentRuntime
from server.agent.state import AgentModelInvocationError
from server.database import (
    DatabaseEnvironment, create_database_engine, create_schema, create_session_factory,
)
from server.model.agent import AgentConversationRecord, AgentMessageRecord
from server.repository.agent import (
    AgentConflictError, AgentConversationNotFoundError, AgentMessageInProgressError,
    AgentRepository,
)
from server.repository.library import LibraryRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.agent_chat import AgentChatService
from server.service.library import LibraryService


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]] = field(default_factory=list)

    def search(self, query, *, workspace_id, series_ids, top_k):
        self.calls.append((query, list(series_ids), top_k, workspace_id))
        return VideoChunkSearchResult(
            query=query, series_ids=list(series_ids),
            agent_reply=AgentReply(message="底层检索完成。", result_count=0), hits=[],
        )


@dataclass
class _QueryModel:
    model_name: str = "query-test"
    queries: tuple[str | None, ...] = ("第一次改写",)
    prompts: list[str] = field(default_factory=list)

    def run_query(self, prompt, *, tools, result_type, max_model_calls, **_kwargs):
        self.prompts.append(prompt)
        number = json.loads(prompt.split("\n\n", 1)[1])["turn_number"]
        query = self.queries[min(number - 1, len(self.queries) - 1)]
        if query is not None:
            tools[0].invoke({"query": query, "top_k": 8})
        return result_type.model_validate({
            "plan_context": f"第 {number} 轮检索并核验动作。",
            "message": f"第 {number} 轮待审核草稿。", "selected_video_chunk_ids": [],
        })


@dataclass
class _EvaluatorModel:
    model_name: str = "evaluator-test"
    approve_after: int = 1
    calls: int = 0

    def generate(self, _inputs, *, schema_name, result_type):
        assert schema_name == "anivag_answer_evaluation"
        self.calls += 1
        return result_type.model_validate({
            "action": "complete" if self.calls >= self.approve_after else "continue",
            "advice": "补充摔杯子的动作，重新核验。",
        })


@dataclass
class _ChatModel:
    model_name: str = "chat-test"
    prompts: list[str] = field(default_factory=list)
    release: Event | None = None
    fail: bool = False

    def stream_text(self, inputs):
        self.prompts.append(inputs[0].text)
        yield "目前没有足够的候选画面，"
        if self.release is not None:
            assert self.release.wait(5), "test did not release provider"
        if self.fail:
            raise RuntimeError("private upstream failure")
        yield "建议补充人物或动作。"


@pytest.fixture
def build_service(tmp_path):
    resources = []

    def build(*, query=None, evaluator=None, chat=None):
        engine = create_database_engine(
            f"sqlite+pysqlite:///{tmp_path / f'agent-{len(resources)}.sqlite'}",
            environment=DatabaseEnvironment.TEST,
        )
        create_schema(engine)
        factory = create_session_factory(engine)
        repository = AgentRepository(factory)
        library = LibraryService(LibraryRepository(factory))
        retrieval = _RetrievalStub()
        query, evaluator, chat = query or _QueryModel(), evaluator or _EvaluatorModel(), chat or _ChatModel()
        service = AgentChatService(
            repository, library, AgentRuntime(query, evaluator, chat, retrieval.search),
        )
        resources.append((service, engine))
        series = library.create_series(workspace_id="local-default", name="Test Series")
        session = service.create_session(workspace_id="local-default", name="画面札记", idempotency_key="session")
        conversation = service.create_conversation(
            session.session_id, workspace_id="local-default", title="测试对话",
            series_ids=[series.series_id], idempotency_key="conversation",
        )
        return service, conversation, retrieval, repository, query, evaluator, chat

    yield build
    for service, engine in resources:
        service.close()
        engine.dispose()


def _send(service, conversation, *, key="message", content="寻找摔杯子的片段"):
    return service.send_message(
        conversation.conversation_id, workspace_id="local-default",
        content=content, idempotency_key=key,
    )


def _collect(execution):
    events = []
    while True:
        try:
            event = execution.next_event(timeout=1)
        except Empty:
            continue
        if event is None:
            return events
        events.append(event)


@pytest.mark.parametrize("second_query", ["补充摔杯子", None])
def test_reviewed_chat_is_persisted_and_idempotent(build_service, second_query):
    service, conversation, retrieval, _, query, evaluator, chat = build_service(
        query=_QueryModel(queries=("第一次改写", second_query)),
        evaluator=_EvaluatorModel(approve_after=2),
    )
    first = _send(service, conversation)
    repeated = _send(service, conversation)
    messages = service.list_messages(conversation.conversation_id, workspace_id="local-default")
    assert first.assistant_message.message_id == repeated.assistant_message.message_id
    assert len(messages) == 2
    assert [m.status for m in messages] == ["COMPLETED", "COMPLETED"]
    assert messages[0].model_name is None
    assert messages[1].model_name == "chat-test"
    assert messages[1].content == "目前没有足够的候选画面，建议补充人物或动作。"
    assert messages[1].search_result["agentReply"]["message"] == messages[1].content
    assert [call[0] for call in retrieval.calls] == ["第一次改写"] + ([second_query] if second_query else [])
    assert evaluator.calls == 2 and len(chat.prompts) == 1
    context = json.loads(query.prompts[1].split("\n\n", 1)[1])
    assert context["original_plan"] == "第 1 轮检索并核验动作。"
    assert context["turns"][0]["query_draft"]["message"] == "第 1 轮待审核草稿。"
    assert context["turns"][0]["review"]["advice"] == "补充摔杯子的动作，重新核验。"
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(conversation.conversation_id, workspace_id="local-default", content="寻找摔杯子的片段", top_k=7, idempotency_key="message")


def test_third_continue_persists_failure_and_never_calls_chat(build_service):
    service, conversation, _, _, _, evaluator, chat = build_service(evaluator=_EvaluatorModel(approve_after=4))
    with pytest.raises(AgentModelInvocationError, match="after 3 turns"):
        _send(service, conversation)
    assert evaluator.calls == 3 and not chat.prompts
    messages = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")
    assert [m.status for m in messages] == ["COMPLETED", "FAILED"]
    assert messages[1].search_result is None and messages[1].model_name == "chat-test"


def test_next_kick_reads_only_completed_conversation_history(build_service):
    service, conversation, _, _, query, _, _ = build_service()
    first = _send(service, conversation)
    _send(service, conversation, key="second", content="再找一段")
    context = json.loads(query.prompts[1].split("\n\n", 1)[1])
    assert context["turn_number"] == 1 and context["turns"] == []
    assert context["inputs"]["history"] == [
        {"role": "USER", "content": first.user_message.content},
        {"role": "ASSISTANT", "content": first.assistant_message.content},
    ]
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(conversation.conversation_id, workspace_id="other-workspace", content="隔离", idempotency_key="other")


def test_live_delta_precedes_commit_and_disconnect_does_not_cancel(build_service):
    release = Event()
    service, conversation, _, _, _, _, _ = build_service(chat=_ChatModel(release=release))
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找到杯子", idempotency_key="stream", stream=True)
    try:
        events = [execution.next_event(timeout=2) for _ in range(3)]
        assert [event["type"] for event in events] == ["answer.started", "answer.evidence", "answer.delta"]
        assert all(event["conversationId"] == conversation.conversation_id for event in events)
        assert [event["sequence"] for event in events] == [1, 2, 3]
        assert not execution.future.done()
        assert service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1].status == "PROCESSING"
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, workspace_id="local-default", content="找到杯子", idempotency_key="stream")
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, workspace_id="local-default", content="另一个问题", idempotency_key="other")
        execution.detach()
        assert not execution.cancellation.is_set()
    finally:
        release.set()
    assert execution.result().assistant_message.status == "COMPLETED"


def test_completed_event_observes_committed_snapshot(build_service):
    service, conversation, _, _, _, _, _ = build_service()
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    assert events[-1]["type"] == "answer.completed"
    completed = execution.result().assistant_message
    persisted = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1]
    assert persisted == completed
    assert events[-1]["assistantMessage"]["status"] == "COMPLETED"
    assert [event["type"] for event in events].count("answer.evidence") == 1


@pytest.mark.parametrize("failure", ["chat", "commit"])
def test_failure_has_no_completed_event_and_does_not_save_partial_text(build_service, monkeypatch, failure):
    service, conversation, _, repository, _, _, _ = build_service(chat=_ChatModel(fail=failure == "chat"))
    if failure == "commit":
        def fail_commit(*_args, **_kwargs):
            raise RuntimeError("database commit failed")
        monkeypatch.setattr(repository, "complete_assistant_message", fail_commit)
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    with pytest.raises((RuntimeError, AgentModelInvocationError)):
        execution.result()
    assert events[-1]["type"] == "answer.failed"
    assert "answer.completed" not in [event["type"] for event in events]
    failed = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1]
    assert failed.status == "FAILED" and failed.search_result is None
    assert "候选画面" not in failed.content
    assert "private upstream failure" not in events[-1]["message"]


def test_shutdown_requests_stop_and_waits_before_resources_close(build_service):
    release = Event()
    service, conversation, _, _, _, _, _ = build_service(chat=_ChatModel(release=release))
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="shutdown", stream=True)
    try:
        for _ in range(3):
            execution.next_event(timeout=2)
        execution.cancellation.set()
    finally:
        release.set()
    service.close()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        execution.result()
    assert service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1].status == "FAILED"
    with pytest.raises(AgentMessageInProgressError, match="shutting down"):
        _send(service, conversation, key="late")


@pytest.mark.parametrize("final_status", ["COMPLETED", "FAILED"])
def test_agent_complete_snapshot_reads_after_500_and_same_sequence_updates(
    tmp_path, final_status
) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'long-conversation.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    repository = AgentRepository(session_factory)
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(workspace_id="workspace-1", name="Long Conversation")
    service = AgentChatService(
        repository,
        library,
        AgentRuntime(_QueryModel(), _EvaluatorModel(), _ChatModel(), _RetrievalStub().search),
    )
    session = service.create_session(
        workspace_id="workspace-1", name="History", idempotency_key="session"
    )
    conversation = service.create_conversation(
        session.session_id,
        workspace_id="workspace-1",
        title="History",
        series_ids=[series.series_id],
        idempotency_key="conversation",
    )
    with session_factory.begin() as db_session:
        db_session.add_all(
            AgentMessageRecord(
                message_id=str(uuid4()),
                conversation_id=conversation.conversation_id,
                sequence_no=index,
                role="USER" if index % 2 == 0 else "ASSISTANT",
                status="COMPLETED",
                content=f"history {index}",
                model_name=None if index % 2 == 0 else "historical-model",
            )
            for index in range(500)
        )
        record = db_session.get(AgentConversationRecord, conversation.conversation_id)
        assert record is not None
        record.next_message_sequence = 500

    now = datetime(2026, 9, 15, 12)
    started = repository.start_message(
        conversation_id=conversation.conversation_id,
        workspace_id="workspace-1",
        content="new question",
        model_name="current-model",
        requested_top_k=8,
        idempotency_key="new-message",
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        now=now,
    )
    page = service.list_messages(
        conversation.conversation_id, workspace_id="workspace-1", limit=500
    )
    snapshot = service.list_all_messages(
        conversation.conversation_id, workspace_id="workspace-1"
    )
    assert len(page) == 500 and page[-1].sequence_no == 499
    assert len(snapshot) == 502
    assert snapshot[-1].sequence_no == 501
    assert snapshot[-1].status == "PROCESSING"
    assert snapshot[-1].model_name == "current-model"

    if final_status == "COMPLETED":
        repository.complete_assistant_message(
            started.assistant_message.message_id,
            content="answer",
            retrieval_query="query",
            search_result={"hits": []},
            now=now,
        )
    else:
        repository.fail_assistant_message(
            started.assistant_message.message_id, error="failed", now=now
        )
    final = service.list_all_messages(
        conversation.conversation_id, workspace_id="workspace-1"
    )
    assert len(final) == 502
    assert final[-1].message_id == snapshot[-1].message_id
    assert final[-1].sequence_no == 501
    assert final[-1].status == final_status
    assert final[-1].model_name == "current-model"
    with pytest.raises(AgentConversationNotFoundError):
        service.list_all_messages(
            conversation.conversation_id, workspace_id="another-workspace"
        )
