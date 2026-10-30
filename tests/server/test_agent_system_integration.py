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
from engine.parsev2.validation import parse_content_fingerprint
from engine.rag import (
    ChunkIndexingContract, EncoderContract, VIDEO_CLIP_PREPROCESS_REVISION,
    VideoClipInputContract,
)
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from langchain_core.tools import BaseTool
from sqlalchemy import delete

from agent.chat.video_search.runtime import AgentRuntime
from agent.chat.supervisor import SupervisorRuntime
from gateway.web_search import WebSearcher
from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.agent import AgentConversation
from server.model.dto.jobDTO import VideoParseLease
from server.model.job import VideoParseTask
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.agent import AgentRepository
from server.repository.library import LibraryRepository
from server.service.video_parse_persistence import VideoParsePersistenceService
from server.service.agent_chat import AgentChatService
from server.service.library import LibraryService
from server.service.video_parse_index_coordinator import VideoParseIndexCoordinator
from server.service.video_parse_media import VideoParseMediaService
from server.service.video_retrieval import VideoParseRetrievalService
from server.storage.elasticsearch import ElasticsearchVideoChunkStore
from server.utils.time import datetime_to_unix_time_ms


def _tool_map(tools: Sequence[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def _invoke(tool: BaseTool, arguments: dict) -> dict:
    return json.loads(tool.invoke(arguments))


def _read_item(tool: BaseTool, video_chunk_id: str) -> dict:
    parts, cursor = [], None
    while True:
        page = _invoke(tool, {"video_chunk_id": video_chunk_id, "cursor": cursor})
        parts.extend(item["text"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return {name: json.loads(value) for name, value in
                    (section.split("\n", 1) for section in "".join(parts).split("\n\n"))}


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
        evidence = _read_item(tools_by_name["read_retrieval_evidence"], self.video_chunk_id)
        assert evidence["evidence.metadata"]["startSeconds"] == self.start_seconds
        assert evidence["evidence.metadata"]["endSeconds"] == self.end_seconds
        return result_type.model_validate({
            "plan_context": "检索并核验第一段动作。",
            "message": "已找到匹配片段。",
        })


@dataclass
class _ChatModel:
    expected_video_chunk_id: str
    model_name: str = "ci-chat"
    provider_name: str = "scripted"

    def run_agent(self, _prompt, *, tools, user_message, handle_text, check_control, **_kwargs):
        available = _tool_map(tools)
        reports = _invoke(available["query_video"], {"query": user_message})["reports"]
        identifier, = reports[0]["evidence_references"]
        assert identifier == self.expected_video_chunk_id
        evidence = _read_item(available["read_retrieval_evidence"], identifier)["evidence.metadata"]
        check_control()
        accepted = _invoke(available["submit_video_item"], {
            "video_chunk_id": identifier, "claim": "第一段动作与问题匹配。",
            "start_seconds": evidence["startSeconds"] / 1000,
            "end_seconds": evidence["endSeconds"] / 1000,
        })
        assert accepted["accepted"] is True
        handle_text("已找到第一段动作。")

    def confirm_turn(self, *_):
        pass

    def discard_turn(self, *_):
        pass

    def delete_thread(self, *_):
        pass


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
    not (os.getenv("SEEKINO_TEST_DATABASE_URL") and os.getenv("SEEKINO_TEST_ELASTICSEARCH_URL")),
    reason="SEEKINO_TEST_DATABASE_URL and SEEKINO_TEST_ELASTICSEARCH_URL are required",
)
def test_agent_runs_over_mysql_and_es_retrieval_and_persists_the_answer(tmp_path) -> None:
    engine = create_database_engine(
        os.environ["SEEKINO_TEST_DATABASE_URL"],
        environment=DatabaseEnvironment.TEST,
    )
    session_factory = create_session_factory(engine)
    series_id, video_id, task_id = (str(uuid4()) for _ in range(3))
    tenant_id = "agent-system-ci"
    es_client = Elasticsearch(
        os.environ["SEEKINO_TEST_ELASTICSEARCH_URL"],
        api_key=os.getenv("SEEKINO_TEST_ELASTICSEARCH_API_KEY") or None,
        request_timeout=15,
    )
    store = ElasticsearchVideoChunkStore(
        es_client, index_name=f"seekino_test_{uuid4().hex}",
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
            session.add(Series(
                id=series_id, tenant_id=tenant_id,
                name="Agent ES integration", status="INDEXING", character_bible=[],
            ))
            session.flush()
            session.add(Video(
                id=video_id, series_id=series_id, episode_order=1,
                upload_sequence=1, filename="episode.mp4",
                source_uri=str(source), hash="a" * 64,
                size_bytes=source.stat().st_size, duration_ms=10,
                upload_status="COMPLETED", parse_status="QUEUED",
                upload_progress=100, parse_progress=0,
            ))
            session.flush()
            session.add(VideoParseTask(
                id=task_id, video_id=video_id, pipeline_version="video-parse-v2",
                status="PREPARING", parse_config=config, chunk_count=1, version=1,
                next_execute_time=now, attempt_count=1, max_attempts=3,
                lease_owner=video_lease.worker_id, lease_version=1,
                lease_expire_time=video_lease.lease_expire_time,
                heartbeat_time=now, start_time=now, processing_duration_ms=0,
                processing_timing_complete=True, is_test=True, is_deleted=False,
                detail={"_worker_runtime_v2": {"accepted": True}},
                create_time=datetime_to_unix_time_ms(now), update_time=datetime_to_unix_time_ms(now),
            ))
        repository = VideoParsePersistenceService(session_factory)
        queue = MySqlVideoParseQueue(session_factory, video_concurrency=3)
        chunks = repository.ensure_video_chunks(video_lease, [(0, 0, 10)])
        prepared = repository.ensure_parse_tasks(
            video_lease, [chunk.video_chunk_id for chunk in chunks], [(0, 1)],
        )
        repository.activate_video_task(video_lease)
        child_id = prepared.chunk_parse_task_ids[0]
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
        refine_input = repository.load_interval_members(refine_lease)
        repository.commit_interval_refinement(
            refine_lease, interval_content={"summary": "unchanged"}, member_updates={},
            input_versions={member.chunk_parse_task_id: member.version for member in refine_input},
            input_fingerprints={member.chunk_parse_task_id: parse_content_fingerprint(member.content)
                                for member in refine_input},
            input_contents={member.chunk_parse_task_id: member.content for member in refine_input},
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
            _QueryModel(chunk_id, 0, 10), retrieval.search, max_model_calls_per_turn=3,
            max_tool_calls_per_turn=16, limits_enabled=True, execution_timeout_seconds=30,
            retrieval_channel="lexical",
        )
        service = AgentChatService(
            AgentRepository(session_factory),
            LibraryService(LibraryRepository(session_factory, execution_mode="v2")),
            SupervisorRuntime(runtime, _ChatModel(chunk_id), WebSearcher(),
                max_web_results=5, web_timeout_seconds=12, execution_timeout_seconds=30,
                max_tool_result_bytes=16_384, max_prompt_chars=200_000),
        )
        conversation = service.create_conversation(
            tenant_id=tenant_id, title="CI Agent",
            idempotency_key=f"ci-agent-conversation-{task_id}",
        )
        completed = service.send_message(
            conversation.conversation_id, tenant_id=tenant_id,
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
            conversation.conversation_id, tenant_id=tenant_id,
        )
        assert [message.status for message in persisted] == ["COMPLETED", "COMPLETED"]
        assert persisted[1].search_result == search_result
    finally:
        if service is not None:
            service.close()
        try:
            with session_factory.begin() as session:
                if conversation is not None:
                    session.execute(delete(AgentConversation).where(
                        AgentConversation.id == conversation.conversation_id
                    ))
                session.execute(delete(Series).where(Series.id == series_id))
        finally:
            try:
                if es_client.indices.exists(index=store.index_name):
                    es_client.indices.delete(index=store.index_name)
            finally:
                es_client.close()
                engine.dispose()
