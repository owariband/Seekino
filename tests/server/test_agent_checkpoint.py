from __future__ import annotations

import os
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from gateway.agent_model.context import ContextWindowPolicy
from server.service.agent_checkpoint import create_agent_checkpoint_store
from tests.gateway.test_deepagents import ScriptedModel, _model, _run

TEST_DATABASE_URL = os.getenv("ANIVAG_TEST_DATABASE_URL")


def test_checkpoint_store_rejects_non_mysql_and_missing_database():
    with pytest.raises(ValueError, match=r"mysql\+pymysql"):
        create_agent_checkpoint_store("sqlite+pysqlite:///:memory:")
    with pytest.raises(ValueError, match="selected database"):
        create_agent_checkpoint_store("mysql+pymysql://localhost/")


def test_checkpoint_store_sets_up_and_closes_connection(monkeypatch):
    import pymysql
    from langgraph.checkpoint.mysql import pymysql as checkpoint_pymysql

    connection = Mock()
    saver = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(pymysql, "connect", connect)
    monkeypatch.setattr(checkpoint_pymysql, "PyMySQLSaver", lambda _connection: saver)

    store = create_agent_checkpoint_store(
        "mysql+pymysql://user:pass@localhost:3307/anivag_test?charset=utf8mb4",
        setup=True,
    )
    assert connect.call_args.kwargs == {
        "host": "localhost", "user": "user", "password": "pass",
        "database": "anivag_test", "port": 3307,
        "charset": "utf8mb4", "autocommit": True,
    }
    saver.setup.assert_called_once_with()
    store.close()
    store.close()
    connection.close.assert_called_once_with()


def test_checkpoint_store_uses_engine_connection_factory(monkeypatch):
    from sqlalchemy.engine import make_url
    from langgraph.checkpoint.mysql import pymysql as checkpoint_pymysql

    saver = Mock()
    engine = Mock()
    engine.url = make_url("mysql+pymysql://user:pass@localhost/anivag_test")
    engine.raw_connection = Mock()
    monkeypatch.setattr(checkpoint_pymysql, "PyMySQLSaver", Mock(return_value=saver))
    store = create_agent_checkpoint_store("unused", engine=engine, setup=True)
    checkpoint_pymysql.PyMySQLSaver.assert_called_once_with(engine.raw_connection)
    saver.setup.assert_called_once_with()
    store.close()
    engine.dispose.assert_not_called()


def test_checkpoint_store_closes_connection_when_setup_fails(monkeypatch):
    import pymysql
    from langgraph.checkpoint.mysql import pymysql as checkpoint_pymysql

    connection = Mock()
    saver = Mock()
    saver.setup.side_effect = RuntimeError("setup failed")
    monkeypatch.setattr(pymysql, "connect", Mock(return_value=connection))
    monkeypatch.setattr(checkpoint_pymysql, "PyMySQLSaver", lambda _connection: saver)
    with pytest.raises(RuntimeError, match="setup failed"):
        create_agent_checkpoint_store(
            "mysql+pymysql://user:pass@localhost/anivag_test", setup=True,
        )
    connection.close.assert_called_once_with()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_restores_native_tool_history_after_store_rebuild():
    thread = f"checkpoint-{uuid4()}"
    tool = StructuredTool.from_function(
        lambda query: f"fact:{query}", name="search", description="Search test evidence.",
    )
    store = create_agent_checkpoint_store(TEST_DATABASE_URL or "", setup=True)
    try:
        script = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "search", "args": {"query": "one"}, "id": "call-1"}]),
            AIMessage(content="Next answer"),
        ])
        model = _model(script, store=store)
        _run(model, thread=thread, message="a1", tools=[tool])
        model.confirm_turn(thread, "a1", "Approved native answer")
    finally:
        store.close()

    store = create_agent_checkpoint_store(TEST_DATABASE_URL or "")
    try:
        script = ScriptedModel(responses=[AIMessage(content="Next answer")])
        model = _model(script, store=store)
        _run(model, thread=thread, message="a2", tools=[tool])
        restored = script._calls[0]["messages"]
        assert any(isinstance(message, ToolMessage) and message.content == "fact:one" for message in restored)
        assert "Approved native answer" in [message.text for message in restored]
        model.delete_thread(thread)
        assert store.checkpointer.get_tuple({"configurable": {"thread_id": thread}}) is None
    finally:
        store.close()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_restores_compaction_and_offloaded_files_after_store_rebuild():
    thread = f"checkpoint-compact-{uuid4()}"
    policy = ContextWindowPolicy(
        context_window_tokens=8_000, output_reserve_tokens=500,
        safety_margin_tokens=500, auto_compact_tokens=1_500, compact_target_tokens=200,
    )
    history = [
        {"messageId": f"history-{index}", "role": "user" if index % 2 == 0 else "assistant",
         "content": "Earlier detailed facts " * 100}
        for index in range(10)
    ]
    store = create_agent_checkpoint_store(TEST_DATABASE_URL or "", setup=True)
    try:
        model = _model(ScriptedModel(responses=[AIMessage(content="After summary")]), store=store, policy=policy)
        _run(model, thread=thread, message="a1", history=history)
        model.confirm_turn(thread, "a1", "After summary")
    finally:
        store.close()

    store = create_agent_checkpoint_store(TEST_DATABASE_URL or "")
    try:
        saved = store.checkpointer.get_tuple({"configurable": {"thread_id": thread}})
        assert saved is not None
        next_script = ScriptedModel(responses=[AIMessage(content="Next answer")])
        model = _model(next_script, store=store, policy=policy)
        _run(model, thread=thread, message="a2")
        values = model.session_store.pending[thread].values
        assert any(path.startswith("/conversation_history/") for path in values["files"])
        assert "Summary: the user asked" in values["_summarization_event"]["summary_message"].text
        assert any("Summary: the user asked" in message.text for message in next_script._calls[0]["messages"])
        model.delete_thread(thread)
    finally:
        store.close()
