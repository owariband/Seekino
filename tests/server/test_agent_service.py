from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import pytest

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.agent import AgentConversationRecord, AgentMessageRecord
from server.repository.agent import (
    AgentConflictError,
    AgentConversationNotFoundError,
    AgentRepository,
)
from server.repository.library import LibraryRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.agent import AgentConversationService, AgentModelInvocationError
from server.service.library import LibraryService


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]] = field(default_factory=list)

    def search(self, query: str, *, workspace_id: str, series_ids, top_k: int):
        scope = list(series_ids)
        self.calls.append((query, scope, top_k, workspace_id))
        return VideoChunkSearchResult(
            query=query,
            series_ids=scope,
            agent_reply=AgentReply(message="底层检索完成。", result_count=0),
            hits=[],
        )


@dataclass
class _ChatModelStub:
    model_name: str = "doubao-chat-test"
    provider_name: str = "ark"
    calls: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def generate(self, inputs, *, schema_name: str, result_type):
        self.calls.append(schema_name)
        self.prompts.append(inputs[0].text)
        payloads = {
            "anivag_retrieval_intent": {
                "retrieval_query": "第一次改写",
                "top_k": 8,
            },
            "anivag_chat_answer": {
                "message": "目前没有足够的候选画面，建议补充人物或动作。",
                "selected_video_chunk_ids": [],
            },
        }
        return result_type.model_validate(payloads[schema_name])


@dataclass
class _EvaluatorModelStub:
    model_name: str = "doubao-evaluator-test"
    provider_name: str = "ark"
    first_corrected_query: str | None = "加入哭泣动作的第二次改写"
    approve_after_revision: bool = True
    calls: int = 0
    prompts: list[str] = field(default_factory=list)

    def generate(self, inputs, *, schema_name: str, result_type):
        assert schema_name == "anivag_answer_evaluation"
        self.calls += 1
        self.prompts.append(inputs[0].text)
        approved = self.calls > 1 and self.approve_after_revision
        return result_type.model_validate(
            {
                "approved": approved,
                "feedback": (
                    "回答与证据一致，可以交付。"
                    if approved
                    else "补充哭泣动作并收紧回答。"
                ),
                "corrected_query": (
                    self.first_corrected_query if self.calls == 1 else None
                ),
            }
        )


@pytest.mark.parametrize(
    ("corrected_query", "expected_retrieval_queries"),
    [
        (
            "加入哭泣动作的第二次改写",
            ["第一次改写", "加入哭泣动作的第二次改写"],
        ),
        (None, ["第一次改写"]),
    ],
)
def test_langgraph_reviews_draft_once_and_persists_message_model_name(
    tmp_path,
    corrected_query,
    expected_retrieval_queries,
) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'agent.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(
        workspace_id="local-default",
        name="Test Series",
    )
    retrieval = _RetrievalStub()
    chat_model = _ChatModelStub()
    evaluator_model = _EvaluatorModelStub(first_corrected_query=corrected_query)
    service = AgentConversationService(
        AgentRepository(session_factory),
        library,
        retrieval,  # type: ignore[arg-type]
        chat_model,
        evaluator_model,
    )

    session = service.create_session(
        workspace_id="local-default",
        name="画面札记",
        idempotency_key="session-key",
    )
    conversation = service.create_conversation(
        session.session_id,
        workspace_id="local-default",
        title="测试对话",
        series_ids=[series.series_id],
        idempotency_key="conversation-key",
    )
    first = service.send_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="她为什么哭？",
        idempotency_key="message-key",
    )
    repeated = service.send_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="她为什么哭？",
        idempotency_key="message-key",
    )
    messages = service.list_messages(
        conversation.conversation_id,
        workspace_id="local-default",
    )

    assert [call[0] for call in retrieval.calls] == expected_retrieval_queries
    assert all(call[3] == "local-default" for call in retrieval.calls)
    assert evaluator_model.calls == 2
    assert chat_model.calls == [
        "anivag_retrieval_intent",
        "anivag_chat_answer",
        "anivag_chat_answer",
    ]
    assert chat_model.prompts[0].count("她为什么哭？") == 1
    assert "Draft Answer" in evaluator_model.prompts[0]
    assert "补充哭泣动作并收紧回答。" in chat_model.prompts[2]
    assert repeated.assistant_message.message_id == first.assistant_message.message_id
    assert len(messages) == 2
    assert messages[0].model_name is None
    assert messages[1].model_name == "doubao-chat-test"
    assert messages[1].status == "COMPLETED"
    assert messages[1].retrieval_query == (
        corrected_query or "第一次改写"
    )
    assert messages[1].search_result is not None
    assert messages[1].search_result["agentReply"]["message"].startswith("目前没有")

    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="她为什么哭？",
            top_k=7,
            idempotency_key="message-key",
        )


def test_langgraph_does_not_release_a_twice_rejected_answer(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'agent-rejected.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(
        workspace_id="local-default",
        name="Review Gate Series",
    )
    retrieval = _RetrievalStub()
    chat_model = _ChatModelStub()
    evaluator_model = _EvaluatorModelStub(
        first_corrected_query=None,
        approve_after_revision=False,
    )
    service = AgentConversationService(
        AgentRepository(session_factory),
        library,
        retrieval,  # type: ignore[arg-type]
        chat_model,
        evaluator_model,
    )
    session = service.create_session(
        workspace_id="local-default",
        name="审核门禁测试",
        idempotency_key="rejected-session",
    )
    conversation = service.create_conversation(
        session.session_id,
        workspace_id="local-default",
        title="审核门禁测试",
        series_ids=[series.series_id],
        idempotency_key="rejected-conversation",
    )

    with pytest.raises(
        AgentModelInvocationError,
        match="rejected the Chat Agent answer after one revision",
    ):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="查找一个画面",
            idempotency_key="rejected-message",
        )

    messages = service.list_messages(
        conversation.conversation_id,
        workspace_id="local-default",
    )
    assert [message.status for message in messages] == ["COMPLETED", "FAILED"]
    assert len(retrieval.calls) == 1
    assert chat_model.calls == [
        "anivag_retrieval_intent",
        "anivag_chat_answer",
        "anivag_chat_answer",
    ]
    assert evaluator_model.calls == 2


@dataclass
class _FailingChatModel:
    model_name: str = "doubao-chat-failing"
    provider_name: str = "ark"

    def generate(self, _inputs, *, schema_name: str, result_type):
        raise RuntimeError("private upstream failure")


def test_agent_failure_is_persisted_with_the_answer_model_name(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'agent-failure.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(
        workspace_id="local-default",
        name="Failure Series",
    )
    service = AgentConversationService(
        AgentRepository(session_factory),
        library,
        _RetrievalStub(),  # type: ignore[arg-type]
        _FailingChatModel(),
        _EvaluatorModelStub(),
    )
    session = service.create_session(
        workspace_id="local-default",
        name="失败状态测试",
        idempotency_key="failure-session",
    )
    conversation = service.create_conversation(
        session.session_id,
        workspace_id="local-default",
        title="失败状态测试",
        series_ids=[series.series_id],
        idempotency_key="failure-conversation",
    )

    with pytest.raises(
        AgentModelInvocationError,
        match="Chat Agent retrieval intent resolver",
    ):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="查找一个画面",
            idempotency_key="failure-message",
        )

    messages = service.list_messages(
        conversation.conversation_id,
        workspace_id="local-default",
    )
    assert [message.status for message in messages] == ["COMPLETED", "FAILED"]
    assert messages[1].model_name == "doubao-chat-failing"
    assert messages[1].content == "这次检索没有完成，请稍后重试。"
    assert messages[1].last_error == (
        "AgentModelInvocationError: Chat Agent retrieval intent resolver model "
        "'doubao-chat-failing' failed"
    )
    assert "private upstream failure" not in messages[1].last_error


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
    service = AgentConversationService(
        repository,
        library,
        _RetrievalStub(),  # type: ignore[arg-type]
        _ChatModelStub(),
        _EvaluatorModelStub(),
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
