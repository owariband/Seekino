from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch
from engine.rag import (
    ChunkIndexingContract, EncoderContract, VIDEO_CLIP_PREPROCESS_REVISION,
    VideoClipInputContract,
)
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from langchain_core.tools import BaseTool
from sqlalchemy import delete

from agent.chat.video_search.runtime import AgentRuntime
from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.agent import AgentConversationRecord
from server.model.dto.jobDTO import VideoParseLease
from server.model.job import VideoParseTaskRecord
from server.model.video import SeriesRecord, VideoRecord
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.agent import AgentRepository
from server.repository.library import LibraryRepository
from server.repository.video_parse import VideoParseRepository
from server.service.agent_chat import AgentChatService
from server.service.library import LibraryService
from server.service.video_parse_index_coordinator import VideoParseIndexCoordinator
from server.service.video_parse_media import VideoParseMediaService
from server.service.video_retrieval import VideoParseRetrievalService
from server.storage.elasticsearch import ElasticsearchVideoChunkStore


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
                "lexical": 0.0,
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
    def embed_text(self, _text: str) -> EmbeddingResult:
        return EmbeddingResult(model="ci-embedding", modality="text", vector=[1.0, 0.0])

    def embed_video(self, _path: Path) -> EmbeddingResult:
        return EmbeddingResult(model="ci-embedding", modality="video", vector=[0.0, 1.0])

    def embed_query(self, _query: str) -> EmbeddingResult:
        return EmbeddingResult(
            model="ci-embedding",
            modality="text",
            vector=[1.0, 0.0],
        )


@pytest.mark.skipif(
    not (os.getenv("ANIVAG_TEST_DATABASE_URL") and os.getenv("ANIVAG_TEST_ELASTICSEARCH_URL")),
    reason="ANIVAG_TEST_DATABASE_URL and ANIVAG_TEST_ELASTICSEARCH_URL are required",
)
def test_agent_runs_over_mysql_and_es_retrieval_and_persists_the_answer(tmp_path) -> None:
    engine = create_database_engine(
        os.environ["ANIVAG_TEST_DATABASE_URL"],
        environment=DatabaseEnvironment.TEST,
    )
    session_factory = create_session_factory(engine)
    series_id, video_id, task_id = (str(uuid4()) for _ in range(3))
    workspace_id = "agent-system-ci"
    es_client = Elasticsearch(
        os.environ["ANIVAG_TEST_ELASTICSEARCH_URL"],
        api_key=os.getenv("ANIVAG_TEST_ELASTICSEARCH_API_KEY") or None,
        request_timeout=15,
    )
    store = ElasticsearchVideoChunkStore(
        es_client, index_name=f"anivag_test_{uuid4().hex}",
        environment=DatabaseEnvironment.TEST, semantic_dimensions=2, vision_dimensions=2,
    )
    service = None
    conversation = None
    try:
        create_schema(engine)
        store.ensure_index()
        source = tmp_path / "episode.mp4"
        source.write_bytes(b"deterministic-source")
        config = default_video_parse_v2_config().model_dump(mode="json")
        config.update(chunk_duration=10, interval_duration=10, batch_max_size=1)
        config["models"]["embedding"].update(model="ci-embedding", encoder_model=None, dimensions=2)
        now = datetime.now(UTC) - timedelta(seconds=1)
        video_lease = VideoParseLease(
            video_parse_task_id=task_id, video_id=video_id, worker_id="agent-test-prepare",
            lease_version=1, lease_expire_time=now + timedelta(minutes=10),
            attempt_count=1, max_attempts=3, parse_config=config,
        )
        with session_factory.begin() as session:
            session.add(SeriesRecord(
                series_id=series_id, workspace_id=workspace_id,
                name="Agent ES integration", status="INDEXING", character_bible=[],
            ))
            session.flush()
            session.add(VideoRecord(
                video_id=video_id, series_id=series_id, episode_order=1,
                upload_sequence=1, original_filename="episode.mp4",
                original_source_uri=str(source), content_hash="a" * 64,
                total_bytes=source.stat().st_size, duration_seconds=10,
                upload_status="COMPLETED", parse_status="QUEUED",
                upload_progress=100, parse_progress=0,
            ))
            session.flush()
            session.add(VideoParseTaskRecord(
                id=task_id, video_id=video_id, pipeline_version="video-parse-v2",
                status="PREPARING", parse_config=config, chunk_count=1, version=1,
                next_execute_time=now, attempt_count=1, max_attempts=3,
                lease_owner=video_lease.worker_id, lease_version=1,
                lease_expire_time=video_lease.lease_expire_time,
                heartbeat_time=now, start_time=now, processing_duration_ms=0,
                processing_timing_complete=True, is_test=True, is_deleted=False,
                detail={"_worker_runtime_v2": {"accepted": True}},
                create_time=now, update_time=now,
            ))
        repository = VideoParseRepository(session_factory)
        queue = MySqlVideoParseQueue(session_factory)
        chunks = repository.ensure_video_chunks(video_lease, [(0, 0, 10)])
        prepared = repository.ensure_parse_tasks(
            video_lease, [chunk.video_chunk_id for chunk in chunks], [(0, 1)],
        )
        repository.activate_video_task(video_lease)
        child_id = prepared.chunk_parse_task_ids[0]
        assert queue.dispatch_chunk(child_id, video_lease)
        chunk_lease = queue.claim_chunk(
            "agent-test-understand", lease_duration=timedelta(minutes=10),
            video_parse_task_id=task_id,
        )
        assert chunk_lease is not None and chunk_lease.chunk_parse_task_id == child_id
        repository.commit_chunk_result(chunk_lease, content={
            "persons": [],
            "timeline": [{
                "event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
                "description": "第一段动作", "participants": [], "visible_persons": [],
            }],
        }, detail={"fixture": "agent-system-ci"})
        refine_lease = queue.claim_interval_refine(
            "agent-test-refine", lease_duration=timedelta(minutes=10),
            video_parse_task_id=task_id,
        )
        assert refine_lease is not None
        repository.commit_interval_refinement(
            refine_lease, interval_content={"summary": "unchanged"}, member_updates={},
        )
        index_lease = queue.claim_interval_index(
            "agent-test-index", lease_duration=timedelta(minutes=10),
            video_parse_task_id=task_id,
        )
        assert index_lease is not None
        encoder = EncoderContract(
            provider="ark", model="ci-embedding",
            encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION, dimensions=2,
        )
        embedder = _QueryEmbedder()

        def write_media(command):
            Path(command[-1]).write_bytes(b"deterministic-prepared-media")

        VideoParseIndexCoordinator(
            repository, store, embedder, media_resolver=Path,
            media_service=VideoParseMediaService(tmp_path / "media", command_runner=write_media),
            contract=ChunkIndexingContract(
                retrieval_contract_id="agent-ci-retrieval-v1",
                semantic_encoder=encoder, vision_encoder=encoder,
                visual_input_contract=VideoClipInputContract(
                    fps=1, max_height=config["visual_input"]["max_height"],
                    mime_type="video/mp4", preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                ),
            ),
        ).index_interval(index_lease)
        assert repository.aggregate_task(video_lease).status == "COMPLETE"
        retrieval = VideoParseRetrievalService(
            repository, store, embedder, semantic_encoder=encoder,
            expected_index_generation=1, channel_top_k=2, exact=True,
        )
        chunk_id = chunks[0].video_chunk_id
        runtime = AgentRuntime(
            _QueryModel(chunk_id, 0, 10), _EvaluatorModel(), _ChatModel(chunk_id),
            retrieval.search, max_loop_turns=1, max_model_calls_per_turn=3,
            max_tool_calls_per_turn=8, limits_enabled=True, execution_timeout_seconds=30,
            retrieval_channel="lexical",
        )
        service = AgentChatService(
            AgentRepository(session_factory),
            LibraryService(LibraryRepository(session_factory, execution_mode="v2")),
            runtime,
        )
        conversation = service.create_conversation(
            workspace_id=workspace_id, title="CI Agent",
            idempotency_key=f"ci-agent-conversation-{task_id}",
        )
        completed = service.send_message(
            conversation.conversation_id, workspace_id=workspace_id,
            content="第一段发生了什么？", idempotency_key="ci-agent-message",
            series_ids=[series_id], top_k=1,
        )
        assert completed.assistant_message.status == "COMPLETED"
        assert completed.assistant_message.content == "已找到第一段动作。"
        search_result = completed.assistant_message.search_result
        assert search_result is not None
        assert search_result["hits"][0]["candidate"]["videoChunkId"] == chunk_id
        assert any(
            block["type"] == "video" and block["videoChunkId"] == chunk_id
            for block in search_result["renderBlocks"]
        )
        persisted = service.list_all_messages(
            conversation.conversation_id, workspace_id=workspace_id,
        )
        assert [message.status for message in persisted] == ["COMPLETED", "COMPLETED"]
        assert persisted[1].search_result == search_result
    finally:
        if service is not None:
            service.close()
        try:
            with session_factory.begin() as session:
                if conversation is not None:
                    session.execute(delete(AgentConversationRecord).where(
                        AgentConversationRecord.conversation_id == conversation.conversation_id
                    ))
                session.execute(delete(SeriesRecord).where(SeriesRecord.series_id == series_id))
        finally:
            try:
                if es_client.indices.exists(index=store.index_name):
                    es_client.indices.delete(index=store.index_name)
            finally:
                es_client.close()
                engine.dispose()
