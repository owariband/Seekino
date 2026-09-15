from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.repository.agent import AgentConflictError, AgentRepository
from server.repository.library import LibraryRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.agent import AgentConversationService, AgentModelInvocationError
from server.service.library import LibraryService


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int]] = field(default_factory=list)

    def search(self, query: str, *, series_ids, top_k: int):
        scope = list(series_ids)
        self.calls.append((query, scope, top_k))
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
            "anivag_chat_retrieval_plan": {
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
    calls: int = 0

    def generate(self, _inputs, *, schema_name: str, result_type):
        assert schema_name == "anivag_retrieval_evaluation"
        self.calls += 1
        return result_type.model_validate(
            {
                "corrected_query": "加入哭泣动作的第二次改写" if self.calls == 1 else None,
                "public_summary": "已检查 query 与候选证据。",
            }
        )


def test_langgraph_agent_retries_once_and_persists_message_model_name(tmp_path) -> None:
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
    evaluator_model = _EvaluatorModelStub()
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

    assert [call[0] for call in retrieval.calls] == [
        "第一次改写",
        "加入哭泣动作的第二次改写",
    ]
    assert evaluator_model.calls == 2
    assert chat_model.calls == [
        "anivag_chat_retrieval_plan",
        "anivag_chat_answer",
    ]
    assert chat_model.prompts[0].count("她为什么哭？") == 1
    assert repeated.assistant_message.message_id == first.assistant_message.message_id
    assert len(messages) == 2
    assert messages[0].model_name is None
    assert messages[1].model_name == "doubao-chat-test"
    assert messages[1].status == "COMPLETED"
    assert messages[1].retrieval_query == "加入哭泣动作的第二次改写"
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

    with pytest.raises(AgentModelInvocationError, match="Chat Agent planner"):
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
        "AgentModelInvocationError: Chat Agent planner model "
        "'doubao-chat-failing' failed"
    )
    assert "private upstream failure" not in messages[1].last_error
