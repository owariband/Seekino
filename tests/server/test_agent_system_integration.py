from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace

import pytest
from gateway.embedding import EmbeddingResult
from langchain_core.tools import BaseTool
from qdrant_client import QdrantClient
from sqlalchemy import delete

from server.agent.video_search.runtime import AgentRuntime
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.agent import AgentConversationRecord
from server.model.dto.parseDTO import PreparedEmbedding
from server.model.video import SeriesRecord
from server.repository.agent import AgentRepository
from server.repository.index_sync import IndexSyncRepository
from server.repository.library import LibraryRepository
from server.repository.video import VideoRepository
from server.service.agent_chat import AgentChatService
from server.service.index_sync import IndexSyncService
from server.service.library import LibraryService
from server.service.qdrant_mutation_lock import QdrantMutationLock
from server.service.video_retrieval import VideoRetrievalService
from server.storage.qdrant import QdrantVideoChunkStore


def _tool_map(tools: Sequence[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def _invoke(tool: BaseTool, arguments: dict) -> dict:
    return json.loads(tool.invoke(arguments))


def _read_item(tool: BaseTool, video_chunk_id: str) -> None:
    overview = _invoke(tool, {
        "video_chunk_id": video_chunk_id,
        "section": "overview",
    })
    for section in overview["sections"]:
        _invoke(tool, {
            "video_chunk_id": video_chunk_id,
            "section": section["name"],
            "start": 0,
            "end": section["total_chars"],
        })


@dataclass
class _QueryModel:
    video_chunk_id: str
    start_seconds: int
    end_seconds: int
    model_name: str = "ci-query"
    provider_name: str = "scripted"

    def run_structured(self, _prompt, *, tools, result_type, **_kwargs):
        tools_by_name = _tool_map(tools)
        result = _invoke(tools_by_name["retrieve"], {
            "query": "第一段动作",
            "top_k": 1,
            "channel_weights": {
                "transcript": 0.0,
                "semantic": 1.0,
                "visual": 0.0,
            },
        })
        assert result["items"][0]["video_chunk_id"] == self.video_chunk_id
        _read_item(tools_by_name["read_trieve_item"], self.video_chunk_id)
        accepted = _invoke(tools_by_name["update_draft_item"], {
            "video_chunk_id": self.video_chunk_id,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "claim": "第一段动作与问题匹配。",
        })
        assert accepted["accepted"] is True
        return result_type.model_validate({
            "plan_context": "检索并核验第一段动作。",
            "message": "已找到匹配片段。",
        })


@dataclass
class _EvaluatorModel:
    model_name: str = "ci-evaluator"
    provider_name: str = "scripted"

    def run_structured(self, _prompt, *, tools, result_type, **_kwargs):
        tools_by_name = _tool_map(tools)
        catalog = _invoke(tools_by_name["get_draft_catelog"], {"cursor": None})
        assert len(catalog["items"]) == 1
        _read_item(
            tools_by_name["read_draft_item"],
            catalog["items"][0]["video_chunk_id"],
        )
        return result_type.model_validate({
            "action": "complete",
            "advice": "证据完整。",
        })


@dataclass
class _ChatModel:
    expected_video_chunk_id: str
    model_name: str = "ci-chat"
    provider_name: str = "scripted"

    def run_agent_loop(
        self,
        _prompt,
        *,
        tools,
        handle_text,
        require_tool_call,
        check_control,
        **_kwargs,
    ):
        assert require_tool_call is True
        check_control()
        update = _tool_map(tools)["update_video_item"]
        accepted = _invoke(update, {
            "video_chunk_id": self.expected_video_chunk_id,
            "claim": "第一段动作与问题匹配。",
        })
        assert accepted["accepted"] is True
        handle_text("已找到第一段动作。")


class _QueryEmbedder:
    def embed_query(self, _query: str) -> EmbeddingResult:
        return EmbeddingResult(
            model="ci-embedding",
            modality="text",
            vector=[1.0, 0.0],
        )


@pytest.mark.skipif(
    not os.getenv("ANIVAG_TEST_DATABASE_URL"),
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_agent_runs_over_mysql_retrieval_and_persists_the_answer(
    prepared_import,
) -> None:
    database_url = os.environ["ANIVAG_TEST_DATABASE_URL"]
    engine = create_database_engine(
        database_url,
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    prepared = replace(
        prepared_import,
        workspace_id="agent-system-ci",
        chunks=tuple(
            replace(
                chunk,
                text_embedding=PreparedEmbedding(
                    vector=[1.0, 0.0] if index == 0 else [0.0, 1.0],
                    metadata={"model": "ci-embedding"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[1.0, 0.0] if index == 0 else [0.0, 1.0],
                    metadata={"model": "ci-embedding"},
                ),
            )
            for index, chunk in enumerate(prepared_import.chunks)
        ),
    )
    with session_factory.begin() as session:
        session.execute(
            delete(SeriesRecord).where(SeriesRecord.series_id == prepared.series_id)
        )

    video_repository = VideoRepository(session_factory)
    report = video_repository.import_video_parse(prepared)
    qdrant = QdrantClient(":memory:")
    store = QdrantVideoChunkStore(
        qdrant,
        collection_name="agent_system_ci_test",
        environment=DatabaseEnvironment.TEST,
        text_dimensions=2,
        vision_dimensions=2,
    )
    synced = IndexSyncService(
        IndexSyncRepository(session_factory),
        store,
        mutation_lock=QdrantMutationLock(
            engine,
            environment=DatabaseEnvironment.TEST,
            collection_name=store.collection_name,
        ),
    ).sync_due()
    assert synced.synced_count >= 2
    retrieval = VideoRetrievalService(
        video_repository,
        store,
        _QueryEmbedder(),
        channel_top_k=2,
        exact=True,
    )
    first = prepared.chunks[0].parse_result.window
    runtime = AgentRuntime(
        _QueryModel(
            report.video_chunk_ids[0],
            first.chunk_start_seconds,
            first.chunk_end_seconds,
        ),
        _EvaluatorModel(),
        _ChatModel(report.video_chunk_ids[0]),
        retrieval.search,
        max_loop_turns=1,
        max_model_calls_per_turn=3,
        max_tool_calls_per_turn=8,
        limits_enabled=True,
        execution_timeout_seconds=30,
    )
    service = AgentChatService(
        AgentRepository(session_factory),
        LibraryService(LibraryRepository(session_factory)),
        runtime,
    )
    conversation = service.create_conversation(
        workspace_id=prepared.workspace_id,
        title="CI Agent",
        idempotency_key="ci-agent-conversation",
    )

    try:
        completed = service.send_message(
            conversation.conversation_id,
            workspace_id=prepared.workspace_id,
            content="第一段发生了什么？",
            idempotency_key="ci-agent-message",
            series_ids=[prepared.series_id],
            top_k=1,
        )

        assert completed.assistant_message.status == "COMPLETED"
        assert completed.assistant_message.content == "已找到第一段动作。"
        search_result = completed.assistant_message.search_result
        assert search_result is not None
        assert search_result["hits"][0]["candidate"]["videoChunkId"] == (
            report.video_chunk_ids[0]
        )
        assert any(
            block["type"] == "video"
            and block["videoChunkId"] == report.video_chunk_ids[0]
            for block in search_result["renderBlocks"]
        )
        persisted = service.list_all_messages(
            conversation.conversation_id,
            workspace_id=prepared.workspace_id,
        )
        assert [message.status for message in persisted] == [
            "COMPLETED",
            "COMPLETED",
        ]
        assert persisted[1].search_result == search_result
    finally:
        service.close()
        with session_factory.begin() as session:
            session.execute(
                delete(AgentConversationRecord).where(
                    AgentConversationRecord.conversation_id
                    == conversation.conversation_id
                )
            )
            session.execute(
                delete(SeriesRecord).where(
                    SeriesRecord.series_id == prepared.series_id
                )
            )
        qdrant.close()
        engine.dispose()
