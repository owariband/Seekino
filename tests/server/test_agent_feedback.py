from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy import event

from server.constants.identity import LOCAL_USER_ID
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.agent import AgentMessageFeedback
from server.repository.agent import AgentRepository
from tests.server.test_api import _client


def _seed_assistant_on_repository(
    repository: AgentRepository,
    *,
    status: str = "COMPLETED",
    owner_user_id: str = LOCAL_USER_ID,
):
    conversation = repository.create_conversation(
        conversation_id=str(uuid4()),
        tenant_id="local-default",
        title="feedback test",
        idempotency_key=str(uuid4()),
        owner_user_id=owner_user_id,
    )
    now = 1_763_000_000_000
    started = repository.start_message(
        conversation_id=conversation.conversation_id,
        tenant_id="local-default",
        content="测试问题",
        model_name="deepseek-flash",
        requested_top_k=8,
        requested_run_options={
            "max_model_calls_per_turn": 1,
            "max_tool_calls_per_turn": 1,
        },
        idempotency_key=str(uuid4()),
        series_ids=[],
        requested_series_ids=[],
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        now=now,
        owner_user_id=owner_user_id,
    )
    if status == "COMPLETED":
        repository.complete_assistant_message(
            started.assistant_message.message_id,
            content="回答",
            search_result=None,
            now=now + 1,
            render_blocks=[],
        )
    elif status == "FAILED":
        repository.fail_assistant_message(
            started.assistant_message.message_id,
            error="model failed",
            now=now + 1,
            public_code="AGENT_MODEL_ERROR",
            public_message="回答失败",
        )
    return conversation, started


def _seed_assistant(client, **kwargs):
    return _seed_assistant_on_repository(
        client.app.state.services.agent._repository,
        **kwargs,
    )


def _params(conversation_id: str, message_id: str) -> dict[str, str]:
    return {
        "tenantId": "local-default",
        "conversationId": conversation_id,
        "messageId": message_id,
    }


def _feedback_rows(client, message_id: str) -> int:
    repository: AgentRepository = client.app.state.services.agent._repository
    with repository._session_factory() as session:
        return int(session.scalar(
            select(func.count()).select_from(AgentMessageFeedback).where(
                AgentMessageFeedback.message_id == message_id,
            )
        ) or 0)


def test_feedback_upsert_messages_and_clear(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    conversation, started = _seed_assistant(client)
    params = _params(
        conversation.conversation_id,
        started.assistant_message.message_id,
    )

    response = client.put(
        "/api/agent/messages/feedback",
        params=params,
        json={
            "rating": "DOWN",
            "reasons": ["WRONG_EVIDENCE"],
            "comment": "  片段不对  ",
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["rating"] == "DOWN"
    assert created["reasons"] == ["WRONG_EVIDENCE"]
    assert created["comment"] == "片段不对"
    assert created["createdAt"] == created["updatedAt"]

    listed = client.get(
        "/api/agent/messages/list",
        params={"tenantId": "local-default", "conversationId": conversation.conversation_id},
    )
    assert listed.status_code == 200
    assistant = next(
        item for item in listed.json()["items"]
        if item["messageId"] == started.assistant_message.message_id
    )
    assert assistant["feedback"]["rating"] == "DOWN"
    assert assistant["feedback"]["reasons"] == ["WRONG_EVIDENCE"]

    updated = client.put(
        "/api/agent/messages/feedback",
        params=params,
        json={"rating": "UP"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rating"] == "UP"
    assert updated.json()["reasons"] == []
    assert _feedback_rows(client, started.assistant_message.message_id) == 1

    cleared = client.delete("/api/agent/messages/feedback", params=params)
    assert cleared.status_code == 204
    assert _feedback_rows(client, started.assistant_message.message_id) == 0

    loaded = client.get(
        "/api/agent/messages/get",
        params={**params, "messageId": started.assistant_message.message_id},
    )
    assert loaded.status_code == 200
    assert loaded.json()["assistantMessage"]["feedback"] is None


def test_feedback_rejects_processing_non_assistant_and_foreign_owner(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)

    processing, processing_started = _seed_assistant(client, status="PROCESSING")
    processing_params = _params(
        processing.conversation_id,
        processing_started.assistant_message.message_id,
    )
    assert client.put(
        "/api/agent/messages/feedback",
        params=processing_params,
        json={"rating": "DOWN"},
    ).status_code == 409

    completed, completed_started = _seed_assistant(client)
    user_params = _params(
        completed.conversation_id,
        completed_started.user_message.message_id,
    )
    assert client.put(
        "/api/agent/messages/feedback",
        params=user_params,
        json={"rating": "DOWN"},
    ).status_code == 400

    foreign, foreign_started = _seed_assistant(client, owner_user_id="other-user")
    foreign_params = _params(
        foreign.conversation_id,
        foreign_started.assistant_message.message_id,
    )
    assert client.put(
        "/api/agent/messages/feedback",
        params=foreign_params,
        json={"rating": "DOWN"},
    ).status_code == 404


def test_failed_feedback_requires_down_and_failure_reason_is_failed_only(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)

    failed, failed_started = _seed_assistant(client, status="FAILED")
    failed_params = _params(
        failed.conversation_id,
        failed_started.assistant_message.message_id,
    )
    assert client.put(
        "/api/agent/messages/feedback",
        params=failed_params,
        json={"rating": "UP"},
    ).status_code == 400
    reported = client.put(
        "/api/agent/messages/feedback",
        params=failed_params,
        json={"rating": "DOWN", "reasons": ["FAILURE"]},
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["reasons"] == ["FAILURE"]

    completed, completed_started = _seed_assistant(client)
    completed_params = _params(
        completed.conversation_id,
        completed_started.assistant_message.message_id,
    )
    assert client.put(
        "/api/agent/messages/feedback",
        params=completed_params,
        json={"rating": "DOWN", "reasons": ["FAILURE"]},
    ).status_code == 400
    assert client.put(
        "/api/agent/messages/feedback",
        params=completed_params,
        json={"rating": "UP", "reasons": ["IRRELEVANT"]},
    ).status_code == 400
    assert client.put(
        "/api/agent/messages/feedback",
        params=completed_params,
        json={"rating": "DOWN", "reasons": ["NOT_A_REASON"]},
    ).status_code == 422


def test_deleting_conversation_cascades_feedback(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'feedback-cascade.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    create_schema(engine)
    repository = AgentRepository(create_session_factory(engine))
    conversation, started = _seed_assistant_on_repository(repository)
    repository.upsert_message_feedback(
        conversation.conversation_id,
        "local-default",
        message_id=started.assistant_message.message_id,
        rating="DOWN",
        reasons=["WRONG_EVIDENCE"],
        comment=None,
        now=1_763_000_000_000,
    )
    with repository._session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AgentMessageFeedback)
        ) == 1

    repository.delete_conversation(
        conversation.conversation_id,
        "local-default",
        owner_user_id=LOCAL_USER_ID,
    )
    with repository._session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AgentMessageFeedback)
        ) == 0
    engine.dispose()
