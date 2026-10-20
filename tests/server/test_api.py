from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from io import BytesIO
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from langchain_core.tools import BaseTool
from PIL import Image
from sqlalchemy import select

from engine.common.config import VideoParseRuntimeConfig
from gateway.config import load_config
from gateway.agent_model.errors import ModelInputTooLargeError
from server.api.app import create_app
from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from server.api.agent import _agent_message_view, _agent_render_blocks, _agent_search_evidence_view
from server.api.jobs import _job_event_fingerprint, _job_view
from server.bootstrap import ApplicationServices
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.retrieval import RetrievalConfig
from server.utils.logging import RequestIdFilter, request_id_context
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import (
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoChunkCheckpoint,
    VideoChunkCheckpointStage,
)
from server.model.dto.agentDTO import AgentMessageState
from server.model.video import Series, Video
from server.model.upload import UploadSession
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.agent import AgentRepository
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.repository.video import VideoRepository
from server.schema import (
    AgentReply,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from server.service.agent_chat import AgentChatService
from server.service.agent_attachments import AgentAttachmentService
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore


def _read_all(tool, video_chunk_id: str, *, collection: str = "retrieval") -> dict:
    overview = json.loads(tool.invoke({
        "collection": collection, "reference": video_chunk_id,
        "section": "overview",
    }))
    sections = {}
    for item in overview["sections"]:
        name, total = item["name"], item["total_chars"]
        start = 0
        content = []
        while start < total:
            end = total
            while True:
                page = json.loads(tool.invoke({
                    "collection": collection, "reference": video_chunk_id,
                    "section": name,
                    "start": start,
                    "end": end,
                }))
                if "error" not in page:
                    break
                assert page["error"]["code"] == "RANGE_TOO_LARGE"
                end = page["error"]["suggested_end"]
                assert start < end < total
            assert page["start"] == start
            assert page["end"] == end
            content.append(page["content"])
            start = end
        sections[name] = json.loads("".join(content))
    return sections


def _write_canvas(tools_by_name, operation: str, fields: dict) -> dict:
    collection = "draft" if "draft" in operation else "approved"
    current = json.loads(tools_by_name["get_context_canvas"].invoke({"collection": collection}))
    receipt = json.loads(tools_by_name["write_context_canvas"].invoke({
        "batch_id": uuid4().hex, "expected_revision": current["revision"],
        "operations": [{"operation": operation, **fields}],
    }))
    assert "error" not in receipt
    return receipt["results"][0]


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]]

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        series_ids,
        top_k: int,
        lexical_weight: float | None = None,
        text_weight: float | None = None,
        vision_weight: float | None = None,
    ):
        if any(
            weight is not None
            for weight in (lexical_weight, text_weight, vision_weight)
        ):
            assert None not in (lexical_weight, text_weight, vision_weight)
            assert lexical_weight + text_weight + vision_weight == pytest.approx(1.0)
        scope = list(series_ids)
        self.calls.append((query, scope, top_k, tenant_id))
        return VideoChunkSearchResult(
            query=query,
            series_ids=scope,
            searched_video_ids=[],
            agent_reply=AgentReply(
                message="这次没有找到符合当前检索范围的候选片段。",
                result_count=0,
            ),
            hits=[],
        )


def test_agent_render_blocks_preserves_valid_blocks_and_drops_invalid_stored_blocks(
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="server.api.agent")
    state = AgentMessageState(
        message_id="assistant",
        conversation_id="conversation",
        sequence_no=1,
        role="ASSISTANT",
        status="COMPLETED",
        content="兼容完整正文",
        series_ids=("series",),
        model_name="chat-model",
        search_result={
            "renderBlocks": [
                {"type": "markdown", "markdown": "第一段"},
                {
                    "type": "video",
                    "videoChunkId": "known-2",
                    "startSeconds": 12,
                    "endSeconds": 18,
                },
                {"type": "video", "videoChunkId": "unknown"},
                {"type": "markdown", "markdown": "第二段"},
                {"type": "video", "videoChunkId": "known-1"},
            ],
        },
        last_error=None,
        created_at=1_789_603_200_000,
        updated_at=1_789_603_200_000,
    )
    projected = SimpleNamespace(hits=[
        SimpleNamespace(
            candidate=SimpleNamespace(video_chunk_id="known-1"),
            evidence=SimpleNamespace(start_seconds=10, end_seconds=20),
        ),
        SimpleNamespace(
            candidate=SimpleNamespace(video_chunk_id="known-2"),
            evidence=SimpleNamespace(start_seconds=10, end_seconds=20),
        ),
    ])

    blocks = _agent_render_blocks(state, projected)

    assert [block.model_dump(mode="json", by_alias=True) for block in blocks] == [
        {"type": "markdown", "markdown": "第一段"},
        {
            "type": "video",
            "videoChunkId": "known-2",
            "startSeconds": 12,
            "endSeconds": 18,
        },
        {"type": "markdown", "markdown": "第二段"},
    ]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("event=agent_invalid_video_render_block ")
    ]
    assert len(warnings) == 2
    assert "video_chunk_id=unknown" in warnings[0]
    assert "action=drop_unknown_chunk" in warnings[0]
    assert "video_chunk_id=known-1" in warnings[1]
    assert "proposed_start_seconds=None proposed_end_seconds=None" in warnings[1]
    assert "evidence_start_seconds=10 evidence_end_seconds=20" in warnings[1]
    assert "action=drop_invalid_range" in warnings[1]
    legacy = _agent_render_blocks(
        replace(state, search_result={}),
        projected,
    )
    assert [block.type for block in legacy] == ["markdown"]


def test_canonical_render_blocks_preserve_inline_tools_without_video_evidence(caplog):
    state = AgentMessageState(
        message_id="assistant", conversation_id="conversation", sequence_no=1,
        role="ASSISTANT", status="PROCESSING", content="旧文字投影", series_ids=(),
        model_name="chat", search_result=None, last_error=None, created_at=1, updated_at=1,
        render_blocks=(
            {"type": "markdown", "markdown": "先查资料。"},
            {"type": "tool", "callId": "call-1", "tool": "search_web", "status": "inProgress", "query": "MyGO"},
            {"type": "markdown", "markdown": "找到结果。"},
            {"type": "tool", "callId": "invalid", "tool": "unknown", "status": "completed"},
        ),
    )
    blocks = _agent_render_blocks(state, None)
    assert [block.model_dump(mode="json", by_alias=True) for block in blocks] == list(state.render_blocks[:3])
    wire = _agent_message_view(state).model_dump(mode="json", by_alias=True)
    assert wire["renderBlocks"] == list(state.render_blocks[:3])
    nullable = replace(state, render_blocks=({**state.render_blocks[1], "query": None},))
    tool = _agent_message_view(nullable).model_dump(mode="json", by_alias=True)["renderBlocks"][0]
    assert tool == {**state.render_blocks[1], "query": None}
    assert "drop_invalid_tool" in caplog.text
    assert _agent_render_blocks(replace(state, render_blocks=()), None) == []
    legacy = _agent_render_blocks(replace(state, render_blocks=None), None)
    assert legacy[0].markdown == "旧文字投影"


@pytest.mark.parametrize(("start_seconds", "end_seconds"), [
    (15, 15),
    (15, 14),
    (9, 18),
    (12, 21),
    (True, 18),
])
def test_agent_render_blocks_drops_invalid_known_chunk_range(
    caplog, start_seconds, end_seconds,
) -> None:
    caplog.set_level(logging.WARNING, logger="server.api.agent")
    state = AgentMessageState(
        message_id="assistant",
        conversation_id="conversation",
        sequence_no=1,
        role="ASSISTANT",
        status="COMPLETED",
        content="正文",
        series_ids=("series",),
        model_name="chat-model",
        search_result={
            "renderBlocks": [{
                "type": "video",
                "videoChunkId": "known",
                "startSeconds": start_seconds,
                "endSeconds": end_seconds,
            }],
        },
        last_error=None,
        created_at=1_789_603_200_000,
        updated_at=1_789_603_200_000,
    )
    projected = SimpleNamespace(hits=[SimpleNamespace(
        candidate=SimpleNamespace(video_chunk_id="known"),
        evidence=SimpleNamespace(start_seconds=10, end_seconds=20),
    )])

    blocks = _agent_render_blocks(state, projected)

    assert [block.model_dump(mode="json", by_alias=True) for block in blocks] == [{
        "type": "markdown",
        "markdown": "正文",
    }]
    warning = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("event=agent_invalid_video_render_block ")
    )
    assert "conversation_id=conversation message_id=assistant" in warning
    assert "video_chunk_id=known" in warning
    assert "evidence_start_seconds=10 evidence_end_seconds=20" in warning
    assert "action=drop_invalid_range" in warning


@pytest.mark.parametrize(("sources", "neighbor_change", "accepted"), [
    (["left", "right"], {}, True),
    (None, {}, False),
    (["left", "missing"], {}, False),
    (["left", "right"], {"videoId": "different-video"}, False),
    (["left", "right"], {"startSeconds": 21}, False),
])
def test_agent_render_blocks_validates_internal_sources_without_exposing_them(
    sources, neighbor_change, accepted,
):
    common = {"tenantId": "tenant", "seriesId": "series", "videoId": "video", "durationSeconds": 30}
    evidence = [
        {**common, "videoChunkId": "left", "sequenceNo": 0, "startSeconds": 10, "endSeconds": 20},
        {**common, "videoChunkId": "right", "sequenceNo": 1, "startSeconds": 20, "endSeconds": 30, **neighbor_change},
    ]
    raw = {"type": "video", "videoChunkId": "left", "startSeconds": 15, "endSeconds": 25}
    state = AgentMessageState(
        message_id="assistant", conversation_id="conversation", sequence_no=1,
        role="ASSISTANT", status="COMPLETED", content="正文", series_ids=("series",),
        model_name="test", last_error=None, created_at=1, updated_at=1,
        search_result={
            "renderBlocks": [{**raw, "sourceVideoChunkIds": sources}],
            "hits": [{"candidate": {"videoChunkId": item["videoChunkId"]}, "evidence": item} for item in evidence],
        },
    )
    projected = SimpleNamespace(hits=[SimpleNamespace(
        candidate=SimpleNamespace(video_chunk_id=item["videoChunkId"]),
        evidence=SimpleNamespace(start_seconds=item["startSeconds"], end_seconds=item["endSeconds"]),
    ) for item in evidence])

    blocks = _agent_render_blocks(state, projected)

    assert [block.model_dump(mode="json", by_alias=True) for block in blocks if block.type == "video"] == (
        [raw] if accepted else []
    )


@dataclass
class _AgentModelStub:
    model_name: str
    provider_name: str = "test"

    def run_structured(
        self, _prompt, *, tools, result_type, max_model_calls, **_kwargs,
    ):
        tools_by_name = {tool.name: tool for tool in tools}
        tools_by_name["retrieve"].invoke({
            "query": "她为什么哭",
            "top_k": 5,
            "channel_weights": {
                "lexical": 0.1, "semantic": 0.7, "visual": 0.2,
            },
        })
        return result_type.model_validate({
            "plan_context": "召回哭泣的片段并检查画面和对白。",
            "message": "没有找到可以核验的候选画面。",
        })

    def run_agent(self, prompt, *, tools, user_message, handle_text, check_control, **kwargs):
        available = {tool.name: tool for tool in tools}
        report = json.loads(available["query_video"].invoke({"query": user_message}))
        self.run_agent_loop(
            prompt + "\n\n" + json.dumps(report), tools=tools,
            max_model_calls=kwargs.get("max_model_calls"), handle_text=handle_text,
            request_timeout_seconds=kwargs.get("request_timeout_seconds"),
            check_control=check_control,
        )

    def run_agent_loop(self, prompt, *, tools, handle_text, check_control=None, **_):
        available = {tool.name: tool for tool in tools}
        for report in json.loads(prompt.rsplit("\n\n", 1)[1])["reports"]:
            for item in report.get("draft_items", []):
                check_control and check_control()
                receipt = json.loads(available["submit_video_item"].invoke({
                    name: item[name] for name in ("video_chunk_id", "start_seconds", "end_seconds", "claim")
                }))
                assert "error" not in receipt
            handle_text(report.get("message", "没有找到可以核验的候选画面。"))

    def confirm_turn(self, *_):
        pass

    def discard_turn(self, *_):
        pass

    def delete_thread(self, *_):
        pass


class _ParseConsumerStub:
    running = True
    failure = None

    def start(self) -> None:
        self.running = True

    def close(self) -> None:
        self.running = False


def _client(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'api.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    artifact_store = LocalArtifactStore(
        tmp_path / "artifacts",
        environment=DatabaseEnvironment.TEST,
    )
    library = LibraryService(LibraryRepository(session_factory), artifact_store)
    agent_repository = AgentRepository(session_factory)
    attachments = AgentAttachmentService(agent_repository, artifact_store, config=ImageUploadConfig())
    job_repository = ParseJobRepository(session_factory)
    retrieval = _RetrievalStub([])
    agent = AgentChatService(
        agent_repository,
        library,
        SupervisorRuntime(
            AgentRuntime(_AgentModelStub("query-model-v1"), retrieval.search, retrieval_channel="lexical"),
            _AgentModelStub("chat-model-v1"), SimpleNamespace(),
            max_web_results=5, web_timeout_seconds=12, execution_timeout_seconds=60,
            max_tool_result_bytes=16_384, max_prompt_chars=200_000,
        ),
        attachments=attachments,
    )
    services = ApplicationServices(
        library=library,
        uploads=UploadService(
            UploadRepository(session_factory),
            artifact_store,
            config=UploadServiceConfig(
                preferred_segment_size_bytes=4,
                maximum_upload_size_bytes=1024,
                session_ttl=timedelta(hours=1),
                completion_stale_after=timedelta(minutes=5),
                pipeline_version="pipeline-v1",
                max_job_attempts=3,
            ),
            probe_duration=lambda _path: 12,
            parse_defaults=ParseConfig.from_defaults(
                runtime=VideoParseRuntimeConfig(), gateway=load_config(),
                upload=UploadServiceConfig(), images=ImageUploadConfig(),
                embedding_model=None, embedding_dimensions=1024, embedding_fps=None,
            ),
        ),
        jobs=ParseJobService(job_repository, library),
        media=MediaService(library, artifact_store),
        retrieval=retrieval,  # type: ignore[arg-type]
        agent=agent,
        agent_attachments=attachments,
        parse_consumer=_ParseConsumerStub(),  # type: ignore[arg-type]
    )
    return TestClient(create_app(services)), retrieval, job_repository


def _retain_historical_v1_job(repository, completed: dict) -> str:
    """Seed legacy rows for compatibility checks; upload completion is V2-only."""
    assert completed["jobId"] is None
    job_id = str(uuid4())
    with repository._session_factory.begin() as session:
        task = session.get(VideoParseTask, completed["videoParseTaskId"])
        video = session.get(Video, completed["videoId"])
        session.add(SeriesParseJob(
            id=job_id, series_id=video.series_id, video_id=video.id,
            pipeline_version="pipeline-v1", status="QUEUED", stage="QUEUED",
            progress=0, attempt_count=0, max_attempts=3, lease_version=0,
            next_execute_time=int(task.next_execute_time.timestamp() * 1000),
        ))
        session.flush()
        upload = session.scalar(select(UploadSession).where(UploadSession.initial_task_id == task.id))
        upload.job_id = job_id
    return job_id


def _create_series(
    client: TestClient,
    *,
    tenant_id: str = "local-default",
    name: str = "Test Series",
    ready: bool = True,
) -> str:
    response = client.post(
        "/api/series/create",
        json={
            "tenantId": tenant_id,
            "name": name,
            "description": "API fixture",
        },
    )
    assert response.status_code == 201, response.text
    series_id = response.json()["seriesId"]
    if ready:
        with client.app.state.services.library._repository._session_factory.begin() as session:
            session.get(Series, series_id).status = "READY"
    return series_id


def test_api_paths_name_create_list_get_and_upload_actions(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    openapi_paths = client.app.openapi()["paths"]
    paths = set(openapi_paths)

    assert {
        "/healthz",
        "/api/system/runtime-config",
        "/api/auth/me",
        "/api/auth/guest",
        "/api/auth/email/send",
        "/api/auth/email/verify",
        "/api/auth/logout",
        "/api/series/create",
        "/api/series/list",
        "/api/series/public",
        "/api/series/get",
        "/api/series/update",
        "/api/videos/update_episode",
        "/api/videos/parse/export",
        "/api/videos/parse/export/options",
        "/api/series/images/upload",
        "/api/uploads/create",
        "/api/uploads/parse-config",
        "/api/uploads/get",
        "/api/uploads/list",
        "/api/uploads/delete",
        "/api/uploads/retry-parse",
        "/api/uploads/reparse",
        "/api/uploads/segments/upload",
        "/api/uploads/complete",
        "/api/jobs/list",
        "/api/jobs/get",
        "/api/jobs/video-parse/get",
        "/api/jobs/video-parse/list",
        "/api/jobs/video-parse/events",
        "/api/usage/overview",
        "/api/jobs/summary",
        "/api/jobs/events",
        "/api/videos/content",
        "/api/video-chunks/get",
        "/api/media/images/content",
        "/api/search",
        "/api/agent/conversations/create",
        "/api/agent/conversations/list",
        "/api/agent/conversations/get",
        "/api/agent/conversations/delete",
        "/api/agent/attachments/upload",
        "/api/agent/attachments/content",
        "/api/agent/messages/list",
        "/api/agent/messages/get",
        "/api/agent/messages/options",
        "/api/agent/global-settings",
        "/api/agent/messages/send",
        "/api/agent/messages/cancel",
        "/api/agent/messages/events",
    } == paths
    assert {
        "/api/series",
        "/api/series/{series_id}",
        "/api/series/{series_id}/uploads",
        "/api/series/{series_id}/uploads/create",
        "/api/uploads/{upload_session_id}",
        "/api/uploads/{upload_session_id}/segments/{segment_number}",
        "/api/uploads/{upload_session_id}/segments/{segment_number}/upload",
        "/api/uploads/{upload_session_id}/complete",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/get/{job_id}",
        "/api/jobs/{job_id}/events",
        "/api/videos/{video_id}/content",
    }.isdisjoint(paths)
    assert all("{" not in path and "}" not in path for path in paths)
    for path, method in (
        ("/api/agent/sessions/create", "POST"),
        ("/api/agent/sessions/list", "GET"),
        ("/api/agent/sessions/get", "GET"),
    ):
        assert client.request(method, path).status_code == 404
    assert all(
        route.endpoint.__module__.startswith("server.api.")
        for route in client.app.routes
        if isinstance(route, APIRoute)
    )

    query_identifiers = {
        ("/api/series/get", "get"): {"seriesId"},
        ("/api/series/images/upload", "post"): {"seriesId", "tenantId"},
        ("/api/uploads/create", "post"): {"seriesId"},
        ("/api/uploads/get", "get"): {"uploadSessionId"},
        ("/api/uploads/delete", "post"): {"uploadSessionId", "tenantId"},
        ("/api/uploads/retry-parse", "post"): {
            "uploadSessionId", "tenantId", "expectedJobVersion",
        },
        ("/api/uploads/reparse", "post"): {
            "uploadSessionId", "tenantId", "expectedJobVersion",
        },
        ("/api/uploads/segments/upload", "put"): {
            "uploadSessionId",
            "segmentNumber",
        },
        ("/api/uploads/complete", "post"): {"uploadSessionId"},
        ("/api/jobs/get", "get"): {"jobId"},
        ("/api/jobs/video-parse/get", "get"): {
            "videoParseTaskId", "tenantId",
        },
        ("/api/jobs/video-parse/list", "get"): {"tenantId"},
        ("/api/jobs/video-parse/events", "get"): {
            "videoParseTaskId", "tenantId",
        },
        ("/api/usage/overview", "get"): {"tenantId"},
        ("/api/jobs/events", "get"): {"jobId"},
        ("/api/videos/content", "get"): {"videoId"},
        ("/api/agent/conversations/get", "get"): {"conversationId"},
        ("/api/agent/conversations/delete", "post"): {
            "conversationId", "tenantId",
        },
        ("/api/agent/messages/list", "get"): {"conversationId"},
        ("/api/agent/messages/send", "post"): {"conversationId"},
        ("/api/agent/messages/cancel", "post"): {"conversationId", "tenantId", "messageId"},
        ("/api/agent/messages/events", "get"): {"conversationId"},
    }
    for (path, method), identifiers in query_identifiers.items():
        parameters = openapi_paths[path][method]["parameters"]
        actual = {
            parameter["name"]
            for parameter in parameters
            if parameter["in"] == "query" and parameter["required"]
        }
        assert identifiers <= actual

    schemas = client.app.openapi()["components"]["schemas"]
    assert "AgentSessionView" not in schemas
    assert "AgentSessionCreateRequest" not in schemas
    assert "sessionId" not in schemas["AgentConversationView"]["properties"]
    assert "tenantId" in schemas["AgentConversationView"]["required"]
    assert "seriesIds" not in schemas["AgentConversationView"]["properties"]
    assert "seriesIds" not in schemas["AgentConversationCreateRequest"]["properties"]
    assert "seriesIds" in schemas["AgentMessageView"]["required"]
    assert "seriesIds" in schemas["AgentMessageSendRequest"]["properties"]
    assert "extraContext" in schemas["AgentMessageSendRequest"]["properties"]
    assert "seriesId" in schemas["VideoChunkEvidenceView"]["required"]
    conversation_create_parameters = openapi_paths[
        "/api/agent/conversations/create"
    ]["post"]["parameters"]
    assert {parameter["name"] for parameter in conversation_create_parameters} == {
        "Idempotency-Key"
    }
    conversation_list_parameters = openapi_paths[
        "/api/agent/conversations/list"
    ]["get"]["parameters"]
    assert {parameter["name"] for parameter in conversation_list_parameters} == {
        "tenantId", "limit", "cursor", "seriesId", "query"
    }
    assert openapi_paths["/api/agent/conversations/delete"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AgentConversationView"
    }
    assert {
        "uploadSessionId",
        "seriesName",
        "episodeOrder",
        "originalFilename",
        "overallProgress",
        "stageProgress",
        "completedDurationSeconds",
        "completedThroughSeconds",
        "totalDurationSeconds",
        "completedChunkCount",
        "totalChunkCount",
        "currentChunk",
        "canReparse",
    } <= set(schemas["ParseJobView"]["required"])
    parse_progress = schemas["VideoSummary"]["properties"]["parseProgress"]
    assert "parseProgress" in schemas["VideoSummary"]["required"]
    assert {item.get("type") for item in parse_progress["anyOf"]} == {
        "integer",
        "null",
    }
    assert "agentReply" in schemas["SearchResultView"]["required"]
    assert "agentReply" not in schemas["AgentSearchEvidenceView"]["properties"]
    assert "query" not in schemas["AgentSearchEvidenceView"]["properties"]
    assert "query" in schemas["SearchResultView"]["required"]
    assert "retrievalTrace" in schemas["AgentSearchEvidenceView"]["required"]
    assert schemas["AgentMessageView"]["properties"]["searchResult"]["anyOf"][0] == {
        "$ref": "#/components/schemas/AgentSearchEvidenceView",
    }
    assert "selectionReason" in schemas["SearchHitView"]["required"]
    assert {
        "speaker",
        "text",
        "startSeconds",
        "endSeconds",
    } <= set(schemas["TranscriptCueView"]["required"])
    assert "modelName" in schemas["AgentMessageView"]["required"]
    assert "replyToMessageId" not in schemas["AgentMessageView"]["properties"]
    assert "retrievalQuery" not in schemas["AgentMessageView"]["properties"]


def _background_png() -> bytes:
    image_bytes = BytesIO()
    Image.new("RGB", (32, 16), color="red").save(image_bytes, format="PNG")
    return image_bytes.getvalue()


def test_parse_config_route_returns_defaults_and_rejects_false_frame_cap(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=load_config(),
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024,
        embedding_fps=1.0,
    )
    client.app.state.services.uploads._parse_defaults = defaults
    response = client.get("/api/uploads/parse-config")
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["window"]["chunkDurationSeconds"] == 60
    assert response.json()["window"]["contextOverlapSeconds"] == 3
    assert "chunkDurationMs" not in response.json()["window"]
    assert response.json()["vision"]["fps"] == 1.0

    invalid = response.json()
    invalid["vision"]["inputMode"] = "video"
    invalid["vision"]["maxFramesPerWindow"] = 12
    rejected = client.post(
        "/api/uploads/create?seriesId=invalid",
        headers={"Idempotency-Key": "invalid-parse-config"},
        json={
            "tenantId": "local-default", "episodeOrder": 1,
            "originalFilename": "ep.mp4",
            "mediaType": "video/mp4", "totalSizeBytes": 10,
            "parseConfig": invalid,
        },
    )
    assert rejected.status_code == 422

    series_id = _create_series(client)
    selected = response.json()
    selected["window"]["analysisMaxHeight"] = 480
    created = client.post(
        f"/api/uploads/create?seriesId={series_id}",
        headers={"Idempotency-Key": "configured-parse-upload"},
        json={
            "tenantId": "local-default", "episodeOrder": 1,
            "originalFilename": "ep.mp4",
            "mediaType": "video/mp4", "totalSizeBytes": 10,
            "parseConfig": selected,
        },
    )
    assert created.status_code == 201
    assert created.json()["parseConfig"]["visual_input"]["max_height"] == 480
    restored = client.get(
        "/api/uploads/get",
        params={"tenantId": "local-default", "uploadSessionId": created.json()["uploadSessionId"]},
    )
    assert restored.json()["parseConfig"] == created.json()["parseConfig"]


@pytest.mark.parametrize("entry", ["profile", "v2", "reparse"])
@pytest.mark.parametrize("field,value", [
    ("batch_max_size", 0),
    ("batch_max_size", True),
    ("batch_max_size", 2.5),
    ("dispatch_interval_seconds", -1),
    ("dispatch_interval_seconds", 1e308),
    ("retry_initial_delay_seconds", 0),
    ("retry_max_delay_seconds", 0.5),
    ("retry_max_delay_seconds", 1e300),
])
def test_parse_execution_values_are_rejected_at_every_http_entry(
    tmp_path: Path, entry: str, field: str, value: object,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=load_config(),
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024,
        embedding_fps=1.0,
    )
    if entry == "profile":
        config = defaults.model_dump(mode="json")
        config["execution"][field] = value
        body_key = "parseConfig"
    else:
        config = defaults.to_video_parse_v2().model_dump(mode="json")
        target = config if field in {"batch_max_size", "dispatch_interval_seconds"} else config["execution"]
        target[field] = value
        body_key = "videoParseConfig"
    if entry == "reparse":
        rejected = client.post(
            "/api/uploads/reparse",
            params={"tenantId": "local-default", "uploadSessionId": str(uuid4()), "expectedJobVersion": 1},
            json={body_key: config},
        )
    else:
        rejected = client.post(
            "/api/uploads/create?seriesId=invalid",
            headers={"Idempotency-Key": "invalid-execution"},
            json={
                "tenantId": "local-default", "episodeOrder": 1,
                "originalFilename": "ep.mp4", "mediaType": "video/mp4",
                "totalSizeBytes": 10, body_key: config,
            },
        )
    assert rejected.status_code == 422, rejected.text


def test_upload_api_preserves_explicit_execution_values_and_normalizes_old_profile(
    tmp_path: Path,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(batch_max_size=7, dispatch_interval_seconds=0.25),
        gateway=load_config(), upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024, embedding_fps=1.0,
    )
    client.app.state.services.uploads._parse_defaults = defaults
    series_id = _create_series(client)
    original = defaults.model_dump(mode="json", by_alias=True)
    original["execution"]["batchMaxSize"] = 6
    original["execution"]["dispatchIntervalSeconds"] = 0.5
    original["execution"]["retryInitialDelaySeconds"] = 2.5
    original["execution"]["retryMaxDelaySeconds"] = 12.5
    for episode_order, legacy in ((1, False), (2, True)):
        selected = json.loads(json.dumps(original))
        if legacy:
            for field in ("batchMaxSize", "dispatchIntervalSeconds", "retryInitialDelaySeconds", "retryMaxDelaySeconds"):
                selected["execution"].pop(field)
        payload = {
            "tenantId": "local-default", "episodeOrder": episode_order,
            "originalFilename": "ep.mp4", "mediaType": "video/mp4",
            "totalSizeBytes": 10, "parseConfig": selected,
        }
        for _attempt in range(2):
            created = client.post(
                f"/api/uploads/create?seriesId={series_id}",
                headers={"Idempotency-Key": f"execution-{episode_order}"}, json=payload,
            )
            assert created.status_code == 201, created.text
            execution = created.json()["parseConfig"]["execution"]
            expected = (defaults if legacy else ParseConfig.model_validate(original)).to_video_parse_v2()
            assert execution == expected.execution.model_dump(mode="json")


def test_series_background_upload_save_and_read_back(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    picture = _background_png()
    uploaded = client.post(
        "/api/series/images/upload",
        params={"tenantId": "local-default", "seriesId": series_id},
        content=picture,
        headers={"Content-Type": "image/png"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["imageId"]
    fetched_image = client.get(asset["url"])
    assert fetched_image.status_code == 200
    assert fetched_image.content == picture
    assert fetched_image.headers["content-type"] == "image/png"
    content = f"# 角色\n\n![角色参考](asset://{asset['imageId']})\n\n红色衣服。"
    updated = client.post(
        "/api/series/update",
        json={
            "tenantId": "local-default", "seriesId": series_id,
            "importantExtraData": {"content": content},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["importantExtraData"] == {"format": "markdown", "content": content}
    assert updated.json()["description"] == "API fixture"
    loaded = client.get("/api/series/get", params={"tenantId": "local-default", "seriesId": series_id})
    assert loaded.json()["importantExtraData"] == updated.json()["importantExtraData"]
    repeat_upload = client.post(
        "/api/series/images/upload",
        params={"tenantId": "local-default", "seriesId": series_id}, content=picture,
    )
    assert repeat_upload.json()["imageId"] == asset["imageId"]


def test_series_background_json_patch_and_clear(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    request_scope = {"tenantId": "local-default", "seriesId": series_id}
    json_text = '{  "customKey": [{"trait": "任意字段"}], "enabled": true }'
    saved = client.post(
        "/api/series/update", json={
            **request_scope, "importantExtraData": {"format": "json", "content": json_text},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["importantExtraData"]["content"] == json_text
    description_only = client.post("/api/series/update", json={**request_scope, "description": "  新介绍  "})
    assert description_only.json()["description"] == "新介绍"
    assert description_only.json()["importantExtraData"] == saved.json()["importantExtraData"]
    omitted = client.post("/api/series/update", json=request_scope)
    assert omitted.json()["importantExtraData"] == saved.json()["importantExtraData"]
    cleared_description = client.post("/api/series/update", json={**request_scope, "description": None})
    assert cleared_description.json()["description"] is None
    assert cleared_description.json()["importantExtraData"] == saved.json()["importantExtraData"]
    cleared_background = client.post("/api/series/update", json={**request_scope, "importantExtraData": None})
    assert cleared_background.status_code == 200
    assert cleared_background.json()["importantExtraData"] is None
    assert cleared_background.json()["description"] is None


def test_series_transcript_language_hint_create_update_and_clear(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    created = client.post(
        "/api/series/create",
        json={
            "tenantId": "local-default",
            "name": "Japanese Series",
            "transcriptLanguageHint": "ja-jp",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["transcriptLanguageHint"] == "ja-JP"
    scope = {
        "tenantId": "local-default",
        "seriesId": created.json()["seriesId"],
    }

    omitted = client.post("/api/series/update", json=scope)
    assert omitted.json()["transcriptLanguageHint"] == "ja-JP"
    updated = client.post(
        "/api/series/update",
        json={**scope, "transcriptLanguageHint": "zh-hans"},
    )
    assert updated.json()["transcriptLanguageHint"] == "zh-Hans"
    rejected = client.post(
        "/api/series/update",
        json={**scope, "transcriptLanguageHint": "not a language"},
    )
    assert rejected.status_code == 400
    assert client.get("/api/series/get", params=scope).json()[
        "transcriptLanguageHint"
    ] == "zh-Hans"
    cleared = client.post(
        "/api/series/update",
        json={**scope, "transcriptLanguageHint": None},
    )
    assert cleared.json()["transcriptLanguageHint"] is None


@pytest.mark.parametrize("document,expected_status", [
    ({"format": "json", "content": "{bad json"}, 400),
    ({"format": "json", "content": '{"number": NaN}'}, 400),
    ({"format": "unsupported", "content": "background"}, 422),
    ({"format": "markdown", "content": 42}, 422),
    ({"format": "markdown", "content": "background", "characters": []}, 422),
    ({"content": "![missing](asset://missingImage)"}, 400),
])
def test_series_background_rejects_invalid_document_without_overwriting(tmp_path, document, expected_status):
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    result = client.post("/api/series/update", json={
        "tenantId": "local-default", "seriesId": series_id, "description": "must not persist",
        "importantExtraData": document,
    })
    assert result.status_code == expected_status, result.text
    loaded = client.get("/api/series/get", params={"tenantId": "local-default", "seriesId": series_id})
    assert loaded.json()["description"] == "API fixture"
    assert loaded.json()["importantExtraData"] is None


def test_series_background_image_scope_and_missing_file(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    first_id = _create_series(client, name="First Series")
    other_id = _create_series(client, name="Other Series")
    foreign_id = _create_series(client, tenant_id="other-workspace", name="Foreign Series")
    image = client.post(
        "/api/series/images/upload", content=_background_png(),
        params={"tenantId": "local-default", "seriesId": first_id},
    ).json()
    for series_id, tenant_id, status in ((other_id, "local-default", 400), (foreign_id, "local-default", 404)):
        result = client.post("/api/series/update", json={
            "tenantId": tenant_id, "seriesId": series_id,
            "importantExtraData": {"content": f"asset://{image['imageId']}"},
        })
        assert result.status_code == status
    assert client.get(image["url"].replace("tenantId=local-default", "tenantId=other-workspace")).status_code == 404
    store = client.app.state.services.media._artifact_store
    store.resolve_series_image(image["imageId"], series_id=first_id).unlink()
    missing = client.post("/api/series/update", json={
        "tenantId": "local-default", "seriesId": first_id,
        "importantExtraData": {"content": f"asset://{image['imageId']}"},
    })
    assert missing.status_code == 400


def test_series_background_rejects_image_without_model_variant(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    uploaded = client.post(
        "/api/series/images/upload", content=_background_png(),
        params={"tenantId": "local-default", "seriesId": series_id},
    ).json()
    store = client.app.state.services.media._artifact_store
    store.resolve_series_image(uploaded["imageId"], series_id=series_id, variant="model").unlink()
    assert store.resolve_series_image(uploaded["imageId"], series_id=series_id).is_file()
    result = client.post("/api/series/update", json={
        "tenantId": "local-default", "seriesId": series_id, "description": "must not save",
        "importantExtraData": {"content": f"asset://{uploaded['imageId']}"},
    })
    assert result.status_code == 400
    loaded = client.get("/api/series/get", params={"tenantId": "local-default", "seriesId": series_id}).json()
    assert loaded["description"] == "API fixture"
    assert loaded["importantExtraData"] is None


def test_series_background_upload_validates_scope_body_and_limit(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    client.app.state.services.media.image_upload_config = ImageUploadConfig(maximum_upload_size_bytes=128)
    scope = {"tenantId": "local-default", "seriesId": series_id}
    assert client.post("/api/series/images/upload", params=scope, content=b"").status_code == 400
    assert client.post("/api/series/images/upload", params=scope, content=b"not an image").status_code == 400
    assert client.post("/api/series/images/upload", params=scope, content=b"x" * 129).status_code == 400
    streamed = client.post("/api/series/images/upload", params=scope, content=iter([b"x" * 100, b"x" * 100]))
    assert streamed.status_code == 400
    assert client.post("/api/series/images/upload", params={**scope, "tenantId": "other"}, content=b"x" * 129).status_code == 404


def test_agent_conversation_send_and_list_messages(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    conversation_response = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={
            "tenantId": "local-default",
            "title": "寻找哭泣场景",
        },
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation_id = conversation_response.json()["conversationId"]
    assert conversation_response.json()["tenantId"] == "local-default"
    assert "sessionId" not in conversation_response.json()
    assert "seriesIds" not in conversation_response.json()
    replayed = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={
            "tenantId": "local-default",
            "title": "寻找哭泣场景",
        },
    )
    assert replayed.status_code == 201
    assert replayed.json() == conversation_response.json()
    conflicting = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={"tenantId": "local-default", "title": "另一个标题"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "CONFLICT"

    sent = client.post(
        "/api/agent/messages/send",
        params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "message-1"},
        json={
            "tenantId": "local-default",
            "content": "她为什么哭？",
            "seriesIds": [series_id],
            "topK": 5,
            "extraContext": {
                "playback": {
                    "seriesId": series_id,
                    "videoId": "video-1",
                    "episodeOrder": 1,
                    "positionSeconds": 122,
                },
            },
        },
    )
    oversized = client.post(
        "/api/agent/messages/send",
        params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "oversized-extra-context"},
        json={
            "tenantId": "local-default",
            "content": "这里在讲什么？",
            "extraContext": {"note": "字" * 16_385},
        },
    )
    listed = client.get(
        "/api/agent/messages/list",
        params={"tenantId": "local-default", "conversationId": conversation_id},
    )
    listed_conversations = client.get("/api/agent/conversations/list", params={"tenantId": "local-default"})
    messages_after_user = client.get(
        "/api/agent/messages/list",
        params={"tenantId": "local-default", "conversationId": conversation_id, "afterSequenceNo": 0},
    )

    assert sent.status_code == 200, sent.text
    assert oversized.status_code == 422
    stored = client.app.state.services.agent.list_all_messages(
        conversation_id,
        tenant_id="local-default",
    )
    assert stored[0].extra_context == {
        "playback": {
            "seriesId": series_id,
            "videoId": "video-1",
            "episodeOrder": 1,
            "positionSeconds": 122,
        },
    }
    assert listed.status_code == 200
    assert [item["role"] for item in listed.json()["items"]] == ["USER", "ASSISTANT"]
    assert listed.json()["items"][0]["modelName"] is None
    assert listed.json()["items"][1]["modelName"] == "chat-model-v1"
    assert listed.json()["items"][1]["status"] == "COMPLETED"
    assert all(message["seriesIds"] == [series_id] for message in listed.json()["items"])
    assert listed.json()["items"][1]["searchResult"]["seriesIds"] == [series_id]
    assert "agentReply" not in listed.json()["items"][1]["searchResult"]
    assert len(listed_conversations.json()["items"]) == 1
    assert listed_conversations.json()["items"][0]["messageCount"] == 2
    assert [item["role"] for item in messages_after_user.json()["items"]] == ["ASSISTANT"]


def test_agent_attachment_upload_reads_bytes_and_enforces_scope_and_limits(tmp_path):
    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client)
    scope = {"tenantId": "local-default", "conversationId": conversation_id}
    data = _background_png()
    uploaded = client.post("/api/agent/attachments/upload", params=scope, content=data)
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    assert attachment["mediaType"] == "image/png"
    assert attachment["sizeBytes"] == len(data)
    assert attachment["mediaUri"].startswith("artifact://test/")
    content = client.get(attachment["previewUrl"])
    assert content.status_code == 200 and content.content == data
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["content-type"] == "image/png"

    service = client.app.state.services.agent_attachments
    service.image_upload_config = ImageUploadConfig(maximum_upload_size_bytes=128)
    for body in (b"", b"not an image", b"x" * 129):
        assert client.post("/api/agent/attachments/upload", params=scope, content=body).status_code == 400
    assert client.post("/api/agent/attachments/upload", params=scope,
                       content=iter([b"x" * 100, b"x" * 100])).status_code == 400
    assert client.post("/api/agent/attachments/upload", params={**scope, "tenantId": "other"},
                       content=b"x" * 129).status_code == 404
    assert client.get("/api/agent/attachments/content", params={
        **scope, "mediaUri": "file:///tmp/unmanaged.png",
    }).status_code == 400
    assert client.get("/api/agent/attachments/content", params={
        **scope, "tenantId": "other", "mediaUri": attachment["mediaUri"],
    }).status_code == 404


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_attachment_send_reaches_video_runtime_in_json_and_sse(tmp_path, accept):
    client, retrieval, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    scope = {"tenantId": "local-default", "conversationId": conversation_id}
    uploaded = client.post("/api/agent/attachments/upload", params=scope, content=_background_png()).json()
    response = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Accept": accept, "Idempotency-Key": "attachment-consumed"},
        json={"tenantId": "local-default", "content": "这个表情包出自哪一集？",
              "attachments": [{"mediaUri": uploaded["mediaUri"]}]},
    )
    assert response.status_code == 200, response.text
    completed = _answer_payloads(response.text)[-1] if accept == "text/event-stream" else response.json()
    assert completed["assistantMessage"]["status"] == "COMPLETED"
    assert completed["userMessage"]["attachments"] == [uploaded]
    assert len(retrieval.calls) == 1
    assert len(client.get("/api/agent/messages/list", params=scope).json()["items"]) == 2
    assert client.get(uploaded["previewUrl"]).status_code == 200


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_vision_attachment_send_and_follow_up_keep_images_in_json_and_sse_history(tmp_path, accept):
    import base64

    client, retrieval, _ = _client(tmp_path)
    service = client.app.state.services.agent

    class VisionModel:
        model_name = "vision-supervisor"
        supports_vision_input = True

        def __init__(self):
            self.turns = []

        def run_agent(self, _prompt, *, handle_text, history, **kwargs):
            self.turns.append((kwargs.get("image_urls"), history))
            handle_text("这是一张红色图片。")

        def confirm_turn(self, *_args):
            pass

        def discard_turn(self, *_args):
            pass

        def delete_thread(self, *_args):
            pass

    model = VisionModel()
    service._runtime = SupervisorRuntime(
        service._runtime._video_runtime, model, SimpleNamespace(),
        max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=16_384, max_prompt_chars=200_000,
    )
    service._runtimes = [service._runtime]
    try:
        conversation_id = _create_agent_conversation(client)
        scope = {"tenantId": "local-default", "conversationId": conversation_id}
        original = _background_png()
        uploaded = client.post("/api/agent/attachments/upload", params=scope, content=original)
        assert uploaded.status_code == 200, uploaded.text
        attachment = uploaded.json()
        request = {
            "params": {"conversationId": conversation_id},
            "headers": {"Accept": accept, "Idempotency-Key": "vision-image"},
            "json": {"tenantId": "local-default", "content": "这张图片是什么颜色？",
                     "attachments": [{"mediaUri": attachment["mediaUri"]}]},
        }
        response = client.post("/api/agent/messages/send", **request)
        assert response.status_code == 200, response.text
        if accept == "text/event-stream":
            events = _answer_payloads(response.text)
            assert events[0]["type"] == "answer.started" and events[-1]["type"] == "answer.completed"
            assert events[0]["userMessage"]["attachments"] == [attachment]
            first = events[-1]
        else:
            first = response.json()
        assert first["userMessage"]["attachments"] == [attachment]
        assert first["assistantMessage"]["status"] == "COMPLETED"
        assert first["assistantMessage"]["attachments"] == []
        assert len(model.turns[0][0]) == 1
        assert base64.b64decode(model.turns[0][0][0].split(",", 1)[1]) == original

        follow_up = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Accept": accept, "Idempotency-Key": "vision-follow-up"},
            json={"tenantId": "local-default", "content": "刚才的图片呢？"},
        )
        assert follow_up.status_code == 200, follow_up.text
        assert model.turns[1][0] is None
        assert model.turns[1][1][0]["imageUrls"] == model.turns[0][0]
        listed = client.get("/api/agent/messages/list", params=scope).json()["items"]
        assert len(listed) == 4 and listed[0]["attachments"] == [attachment]
        replay = client.post("/api/agent/messages/send", **request)
        assert replay.status_code == 200, replay.text
        assert len(model.turns) == 2 and not retrieval.calls
        assert client.get(attachment["previewUrl"]).content == original
    finally:
        service.close()


@pytest.mark.parametrize("missing", [None, "original", "manifest"])
def test_saved_attachments_survive_get_list_and_stream_projection(tmp_path, missing, caplog):
    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client)
    scope = {"tenantId": "local-default", "conversationId": conversation_id}
    attachment = client.post("/api/agent/attachments/upload", params=scope, content=_background_png()).json()
    service = client.app.state.services.agent
    options = service.message_options()
    references = [{"mediaUri": attachment["mediaUri"]}]
    pair = service._repository.start_message(
        conversation_id=conversation_id, tenant_id="local-default", content="已保存的图片问题",
        model_name="saved", requested_top_k=options.top_k.default_value,
        requested_run_options={
            "max_model_calls_per_turn": options.max_model_calls_per_turn.default_value,
            "max_tool_calls_per_turn": options.max_tool_calls_per_turn.default_value,
        },
        idempotency_key="saved-attachment", series_ids=[], requested_series_ids=[],
        user_message_id=str(uuid4()), assistant_message_id=str(uuid4()), now=1_789_603_200_000,
        attachments=references, source_tenant_id="separate-video-catalog",
    )
    service._repository.complete_assistant_message(
        pair.assistant_message.message_id, content="已保存的回答", search_result=None,
        now=1_789_603_201_000,
    )
    if missing:
        path = client.app.state.services.media._artifact_store.resolve(attachment["mediaUri"])
        (path if missing == "original" else path.parent / "manifest.json").unlink()
        assert client.get(attachment["previewUrl"]).status_code == 410
    expected = attachment if missing != "manifest" else {
        "mediaUri": attachment["mediaUri"], "mediaType": None, "sizeBytes": None, "previewUrl": None,
    }
    listed = client.get("/api/agent/messages/list", params=scope)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["attachments"] == [expected]
    assert listed.json()["items"][1]["attachments"] == []
    loaded = client.get("/api/agent/messages/get", params={**scope, "idempotencyKey": "saved-attachment"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["userMessage"]["attachments"] == [expected]
    replay = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "saved-attachment"},
        json={"tenantId": "local-default", "sourceTenantId": "separate-video-catalog",
              "content": "已保存的图片问题", "attachments": references},
    )
    assert replay.status_code == 200, replay.text
    events = _answer_payloads(replay.text)
    assert [event["type"] for event in events] == ["answer.started", "answer.completed"]
    assert all(event["userMessage"]["attachments"] == [expected] for event in events)
    if missing == "manifest":
        assert "agent_attachment_unavailable" in caplog.text
        assert attachment["mediaUri"] in caplog.text


def test_agent_conversation_delete_is_workspace_scoped_and_removes_messages(
    tmp_path: Path,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    sent = client.post(
        "/api/agent/messages/send",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
        headers={"Idempotency-Key": "message-before-delete"},
        json={"tenantId": "local-default", "content": "找画面"},
    )
    assert sent.status_code == 200, sent.text

    outside = client.post(
        "/api/agent/conversations/delete",
        params={
            "tenantId": "another-workspace",
            "conversationId": conversation_id,
        },
    )
    assert outside.status_code == 404

    deleted = client.post(
        "/api/agent/conversations/delete",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["conversationId"] == conversation_id
    assert deleted.json()["messageCount"] == 2
    assert client.get(
        "/api/agent/conversations/get",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 404
    assert client.get(
        "/api/agent/messages/list",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 404
    assert client.post(
        "/api/agent/conversations/delete",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 404


def test_agent_conversation_delete_rejects_processing_answer(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    conversation_id = _create_agent_conversation(client)
    service = client.app.state.services.agent
    service._repository.start_message(
        conversation_id=conversation_id,
        tenant_id="local-default",
        content="找画面",
        model_name=service.model_name,
        requested_top_k=service.default_top_k,
        requested_run_options={
            "max_model_calls_per_turn": 6,
            "max_tool_calls_per_turn": 24,
        },
        idempotency_key="processing-delete",
        series_ids=[series_id],
        requested_series_ids=[series_id],
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        now=1_789_603_200_000,
    )

    response = client.post(
        "/api/agent/conversations/delete",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_IN_PROGRESS"
    assert client.get(
        "/api/agent/conversations/get",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 200
    assert client.get(
        "/api/agent/messages/list",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 200
    assert client.post(
        "/api/agent/conversations/delete",
        params={
            "tenantId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 409


def test_agent_conversations_are_created_and_listed_within_workspace(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    local_series_id = _create_series(client)
    other_series_id = _create_series(client, tenant_id="other-workspace")
    conversations = {}
    try:
        for tenant_id, series_id in (
            ("local-default", local_series_id),
            ("other-workspace", other_series_id),
        ):
            response = client.post(
                "/api/agent/conversations/create",
                headers={"Idempotency-Key": "same-key-across-workspaces"},
                json={"tenantId": tenant_id},
            )
            assert response.status_code == 201, response.text
            conversations[tenant_id] = response.json()
            listed = client.get(
                "/api/agent/conversations/list",
                params={"tenantId": tenant_id},
            )
            assert listed.status_code == 200
            assert listed.json()["items"] == [response.json()]
        assert (
            conversations["local-default"]["conversationId"]
            != conversations["other-workspace"]["conversationId"]
        )
        conversation_id = conversations["local-default"]["conversationId"]
        for path in (
            "/api/agent/conversations/get",
            "/api/agent/messages/list",
            "/api/agent/messages/events",
        ):
            outside = client.get(
                path,
                params={
                    "conversationId": conversation_id,
                    "tenantId": "other-workspace",
                },
            )
            assert outside.status_code == 404
        outside_send = client.post(
            "/api/agent/messages/send",
            params={"tenantId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "outside-message"},
            json={"tenantId": "other-workspace", "content": "找画面"},
        )
        assert outside_send.status_code == 404
        outside_scope = client.post(
            "/api/agent/messages/send",
            params={"tenantId": "local-default", "conversationId": conversations["other-workspace"]["conversationId"]},
            headers={"Idempotency-Key": "outside-scope"},
            json={"tenantId": "other-workspace", "content": "找画面", "seriesIds": [local_series_id]},
        )
        assert outside_scope.status_code == 404
        another = client.post(
            "/api/agent/conversations/create",
            headers={"Idempotency-Key": "another-local-conversation"},
            json={"tenantId": "local-default"},
        )
        assert another.status_code == 201
        assert len(client.get("/api/agent/conversations/list", params={"tenantId": "local-default"}).json()["items"]) == 2
        limited = client.get("/api/agent/conversations/list", params={"tenantId": "local-default", "limit": 1})
        assert limited.status_code == 200
        assert len(limited.json()["items"]) == 1
        assert limited.json()["items"][0]["tenantId"] == "local-default"
        assert retrieval.calls == []
    finally:
        client.app.state.services.agent.close()


def test_series_and_search_contract(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    listed = client.get("/api/series/list", params={"tenantId": "local-default"}).json()["items"]
    detail = client.get(
        "/api/series/get",
        params={"tenantId": "local-default", "seriesId": series_id},
    ).json()
    searched = client.post(
        "/api/search",
        json={
            "tenantId": "local-default",
            "query": "她为什么哭",
            "seriesIds": [series_id],
            "topK": 5,
        },
    )

    assert listed[0]["seriesId"] == series_id
    assert {"seriesId", "name", "status"} <= listed[0].keys()
    assert detail["videoCount"] == 0
    assert searched.status_code == 200
    assert searched.json() == {
        "query": "她为什么哭",
        "seriesIds": [series_id],
        "searchedVideoIds": [],
        "partial": False,
        "warnings": [],
        "agentReply": {
            "message": "这次没有找到符合当前检索范围的候选片段。",
            "resultCount": 0,
        },
        "hits": [],
    }
    assert retrieval.calls == [("她为什么哭", [series_id], 5, "local-default")]


def test_search_top_k_uses_retrieval_default_and_preserves_explicit_values(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    service = client.app.state.services.agent
    series_id = _create_series(client)
    body = {"tenantId": "local-default", "query": "空走廊", "seriesIds": [series_id]}
    try:
        service._config = replace(service._config, default_top_k=3, maximum_top_k=5)
        response = client.post("/api/search", json=body)
        assert response.status_code == 200
        assert retrieval.calls[-1][2] == 30

        for top_k in (1, 5, 21, 101):
            response = client.post("/api/search", json={**body, "topK": top_k})
            assert response.status_code == 200
            assert retrieval.calls[-1][2] == top_k

        service._config = replace(service._config, default_top_k=4, maximum_top_k=100)
        response = client.post("/api/search", json=body)
        assert response.status_code == 200
        assert retrieval.calls[-1][2] == 30
        response = client.post("/api/search", json={**body, "topK": None})
        assert response.status_code == 200
        assert retrieval.calls[-1][2] == 30

        client = TestClient(create_app(
            client.app.state.services,
            settings=SimpleNamespace(
                auth=client.app.state.auth_config,
                sse=client.app.state.sse_config,
                retrieval=RetrievalConfig(search_default_top_k=120),
                model_config_path=client.app.state.model_config_path,
                allowed_origins=(),
            ),
        ))
        for overrides in ({}, {"topK": None}):
            response = client.post("/api/search", json={**body, **overrides})
            assert response.status_code == 200
            assert retrieval.calls[-1][2] == 120

        before = len(retrieval.calls)
        for top_k in (0, -1, True, 1.5, "2"):
            response = client.post("/api/search", json={**body, "topK": top_k})
            assert response.status_code == 422
        assert len(retrieval.calls) == before
    finally:
        service.close()


def test_video_episode_update_contract_and_conflict(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    first_video_id = "0ce9a0c8-69aa-4cdf-97e5-837e9b552a35"
    second_video_id = "e6109e20-6233-4a5c-a1d4-acaa23cc8e13"
    with client.app.state.services.library._repository._session_factory.begin() as session:
        for video_id, episode_order, content_hash in (
            (first_video_id, 1, "a" * 64),
            (second_video_id, 2, "b" * 64),
        ):
            session.add(Video(
                id=video_id,
                series_id=series_id,
                episode_order=episode_order,
                upload_sequence=episode_order,
                filename=f"episode-{episode_order}.mp4",
                source_uri=f"artifact://test/{video_id}.mp4",
                proxy_uri=None,
                hash=content_hash,
                size_bytes=10,
                duration_ms=1,
                upload_status="COMPLETED",
                parse_status="SUCCEEDED",
                upload_progress=100,
                parse_progress=100,
            ))

    updated = client.post(
        "/api/videos/update_episode",
        json={
            "tenantId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 3,
        },
    )
    conflict = client.post(
        "/api/videos/update_episode",
        json={
            "tenantId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 2,
        },
    )
    outside_workspace = client.post(
        "/api/videos/update_episode",
        json={
            "tenantId": "another-workspace",
            "videoId": first_video_id,
            "episodeOrder": 4,
        },
    )
    invalid = client.post(
        "/api/videos/update_episode",
        json={
            "tenantId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 0,
        },
    )
    too_large = client.post(
        "/api/videos/update_episode",
        json={
            "tenantId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 2_147_483_648,
        },
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["episodeOrder"] == 3
    # Compatibility projection follows the effective business order until the
    # legacy column is removed in a later migration.
    assert updated.json()["uploadSequence"] == 3
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONFLICT"
    assert outside_workspace.status_code == 404
    assert invalid.status_code == 422
    assert too_large.status_code == 422


def test_upload_episode_order_is_required_unique_and_idempotent(tmp_path: Path) -> None:
    client, _retrieval, _jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    payload = {
        "tenantId": "local-default", "originalFilename": "episode-99.mp4",
        "mediaType": "video/mp4", "totalSizeBytes": 4,
    }
    first = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 99},
    )
    replay = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 99},
    )
    changed_order = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 100},
    )
    duplicate_order = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "duplicate-order"},
        json={**payload, "episodeOrder": 99},
    )
    second = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "second"},
        json={**payload, "episodeOrder": 100},
    )
    assert first.status_code == replay.status_code == second.status_code == 201
    assert first.json()["episodeOrder"] == first.json()["uploadSequence"] == 99
    assert second.json()["episodeOrder"] == second.json()["uploadSequence"] == 100
    assert replay.json()["uploadSessionId"] == first.json()["uploadSessionId"]
    assert replay.json()["episodeOrder"] == 99
    assert changed_order.status_code == 409
    assert duplicate_order.status_code == 409
    changed = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 99, "originalFilename": "changed.mp4"},
    )
    assert changed.status_code == 409
    missing = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "missing-order"}, json=payload,
    )
    assert missing.status_code == 422
    for index, episode_order in enumerate((True, 0, -1, 2_147_483_648)):
        invalid = client.post(
            "/api/uploads/create",
            params={"seriesId": series_id},
            headers={"Idempotency-Key": f"invalid-order-{index}"},
            json={**payload, "episodeOrder": episode_order},
        )
        assert invalid.status_code == 422
    schema = client.get("/openapi.json").json()["components"]["schemas"]["UploadSessionCreateRequest"]
    assert "episodeOrder" in schema["required"]
    assert schema["properties"]["episodeOrder"]["exclusiveMinimum"] == 0
    assert schema["properties"]["episodeOrder"]["maximum"] == 2_147_483_647
    assert "uploadSequence" not in schema["properties"]


def test_upload_delete_is_workspace_scoped_and_idempotent(tmp_path):
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    upload = client.post("/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "delete-upload"}, json={
            "tenantId": "local-default", "episodeOrder": 1,
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4", "totalSizeBytes": 4,
        }).json()
    params = {"tenantId": "local-default", "uploadSessionId": upload["uploadSessionId"]}
    assert client.post("/api/uploads/delete", params={**params, "tenantId": "other"}).status_code == 404
    deleted = client.post("/api/uploads/delete", params=params)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "EXPIRED"
    assert deleted.headers["X-Request-Id"]
    assert client.post("/api/uploads/delete", params=params).json()["version"] == deleted.json()["version"]
    assert client.get("/api/uploads/list", params={"tenantId": "local-default"}).json()["total"] == 0
    assert client.post("/api/uploads/complete", params=params).status_code == 409


def test_upload_parse_retry_is_status_fenced_and_returns_the_same_v2_task(tmp_path):
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "manual-retry"},
        json={
            "tenantId": "local-default",
            "episodeOrder": 1,
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4",
            "totalSizeBytes": 4,
        },
    )
    assert created.status_code == 201, created.text
    upload_session_id = created.json()["uploadSessionId"]
    uploaded = client.put(
        "/api/uploads/segments/upload",
        params={
            "tenantId": "local-default",
            "uploadSessionId": upload_session_id,
            "segmentNumber": 0,
        },
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
        },
        content=b"abcd",
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        params={
            "tenantId": "local-default",
            "uploadSessionId": upload_session_id,
        },
    )
    assert completed.status_code == 200, completed.text
    job_id = _retain_historical_v1_job(jobs_repository, completed.json())
    video_parse_task_id = completed.json()["videoParseTaskId"]
    with jobs_repository._session_factory.begin() as session:
        job = session.get(SeriesParseJob, job_id)
        legacy_job_before = (job.status, job.version, job.attempt_count)
        task = session.get(VideoParseTask, video_parse_task_id)
        original_config = task.parse_config
        task.status = "PARSING_FAILED"
        task.attempt_count = 3
        task.max_attempts = 3
        task.last_error = {"code": "MODEL_TIMEOUT", "message": "provider timeout"}
    retry_params = {
        "tenantId": "local-default",
        "uploadSessionId": upload_session_id,
        "expectedJobVersion": 1,
        "expectedLeaseVersion": 0,
    }
    assert client.post(
        "/api/uploads/retry-parse",
        params={**retry_params, "tenantId": "other-workspace"},
    ).status_code == 404
    retried = client.post("/api/uploads/retry-parse", params=retry_params)
    assert retried.status_code == 200, retried.text
    assert {
        "videoParseTaskId": video_parse_task_id,
        "videoId": completed.json()["videoId"],
        "status": "PREPARING",
        "attemptCount": 3,
        "maxAttempts": 3,
        "version": 1,
    }.items() <= retried.json().items()
    assert retried.json()["parseConfig"] == original_config
    for field in ("nextExecuteTime", "createTime", "updateTime"):
        assert type(retried.json()[field]) is int
    duplicate = client.post("/api/uploads/retry-parse", params=retry_params)
    assert duplicate.status_code == 409
    assert duplicate.json()["requestId"] == duplicate.headers["X-Request-Id"]
    with jobs_repository._session_factory() as session:
        task = session.get(VideoParseTask, video_parse_task_id)
        assert (task.status, task.version) == ("PREPARING", 1)
        assert task.parse_config == original_config
        job = session.get(SeriesParseJob, job_id)
        assert (job.status, job.version, job.attempt_count) == legacy_job_before


def test_upload_full_reparse_creates_new_v2_task_and_retains_old_job(tmp_path):
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "full-reparse"},
        json={
            "tenantId": "local-default",
            "episodeOrder": 1,
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4",
            "totalSizeBytes": 4,
        },
    )
    upload_session_id = created.json()["uploadSessionId"]
    assert client.put(
        "/api/uploads/segments/upload",
        params={
            "tenantId": "local-default",
            "uploadSessionId": upload_session_id,
            "segmentNumber": 0,
        },
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
        },
        content=b"abcd",
    ).status_code == 200
    completed = client.post(
        "/api/uploads/complete",
        params={
            "tenantId": "local-default",
            "uploadSessionId": upload_session_id,
        },
    ).json()
    job_id = _retain_historical_v1_job(jobs_repository, completed)
    old_task_id = completed["videoParseTaskId"]
    with jobs_repository._session_factory.begin() as session:
        job = session.get(SeriesParseJob, job_id)
        job.status = ParseJobStatus.SUCCEEDED.value
        job.stage = ParseJobStage.COMPLETED.value
        job.progress = 100
        job.attempt_count = 1
        job.version = 6
        job.finish_time = 1_789_639_200_000
        legacy_job_before = (
            job.status,
            job.stage,
            job.progress,
            job.attempt_count,
            job.version,
            job.finish_time,
        )
        task = session.get(VideoParseTask, old_task_id)
        old_config = task.parse_config
        task.status = "COMPLETE"
        video = session.get(Video, job.video_id)
        video.parse_status = ParseJobStatus.SUCCEEDED.value
        video.parse_progress = 100
        session.get(Series, series_id).status = "READY"
        session.add(VideoChunkCheckpoint(
            job_id=job_id,
            sequence_no=0,
            chunk_id="reparse-checkpoint",
            stage=VideoChunkCheckpointStage.INDEXED.value,
            start_ms=0,
            end_ms=12,
        ))

    params = {
        "tenantId": "local-default",
        "uploadSessionId": upload_session_id,
        "expectedJobVersion": 1,
    }
    assert client.post(
        "/api/uploads/reparse",
        params={**params, "tenantId": "other-workspace"},
    ).status_code == 404
    replacement_config = dict(old_config)
    replacement_config["batch_max_size"] = 4
    reparsed = client.post(
        "/api/uploads/reparse",
        params=params,
        json={"videoParseConfig": replacement_config},
    )
    assert reparsed.status_code == 200, reparsed.text
    assert {
        "videoId": completed["videoId"],
        "status": "PENDING",
        "attemptCount": 0,
        "version": 2,
    }.items() <= reparsed.json().items()
    assert reparsed.json()["videoParseTaskId"] != old_task_id
    assert reparsed.json()["parseConfig"] == replacement_config
    for field in ("nextExecuteTime", "createTime", "updateTime"):
        assert type(reparsed.json()[field]) is int
    with jobs_repository._session_factory() as session:
        old_task = session.get(VideoParseTask, old_task_id)
        new_task = session.get(
            VideoParseTask,
            reparsed.json()["videoParseTaskId"],
        )
        assert (old_task.status, old_task.version) == (
            "COMPLETE",
            1,
        )
        assert old_task.parse_config == old_config
        assert new_task.version == 2
        assert new_task.parse_config == replacement_config
        assert session.get(
            VideoChunkCheckpoint,
            (job_id, 0),
        ) is not None
        job = session.get(SeriesParseJob, job_id)
        assert (
            job.status,
            job.stage,
            job.progress,
            job.attempt_count,
            job.version,
            job.finish_time,
        ) == legacy_job_before
    duplicate = client.post("/api/uploads/reparse", params=params)
    assert duplicate.status_code == 409
    with jobs_repository._session_factory() as session:
        assert len(list(session.scalars(
            select(VideoParseTask).where(
                VideoParseTask.video_id == completed["videoId"]
            )
        ))) == 2


def test_http_and_upload_logs_share_request_id_without_logging_query_or_body(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="server")
    context_filter = RequestIdFilter()
    caplog.handler.addFilter(context_filter)
    try:
        client, _retrieval, _jobs = _client(tmp_path)
        series_id = _create_series(client)
        caplog.clear()
        response = client.post("/api/uploads/create", params={"seriesId": series_id, "token": "private-query"},
            headers={"Idempotency-Key": "log-upload", "Authorization": "private-header"}, json={
                "tenantId": "local-default", "episodeOrder": 1,
                "originalFilename": "private-filename.mp4",
                "mediaType": "video/mp4", "totalSizeBytes": 4,
            })
        assert response.status_code == 201
        records = [record for record in caplog.records if record.name.startswith("server.")]
        assert any("upload_session_created" in record.message for record in records)
        assert any("path=/api/uploads/create status=201 duration_ms=" in record.message for record in records)
        assert {record.request_id for record in records} == {response.headers["X-Request-Id"]}
        assert "private-query" not in caplog.text
        assert "private-header" not in caplog.text
        assert "private-filename" not in caplog.text
        assert request_id_context.get() == "-"
    finally:
        caplog.handler.removeFilter(context_filter)


def test_chunked_upload_complete_job_progress_and_video_range(tmp_path: Path) -> None:
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    create_payload = {
        "tenantId": "local-default",
        "episodeOrder": 7,
        "originalFilename": "episode-07.mp4",
        "mediaType": "video/mp4",
        "totalSizeBytes": 10,
    }

    first = client.post(
        "/api/uploads/create",
        params={"tenantId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    repeated = client.post(
        "/api/uploads/create",
        params={"tenantId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    assert first.status_code == 201, first.text
    assert repeated.status_code == 201
    upload_id = first.json()["uploadSessionId"]
    assert repeated.json()["uploadSessionId"] == upload_id
    assert first.json()["missingSegmentNumbers"] == [0, 1, 2]
    assert first.json()["episodeOrder"] == first.json()["uploadSequence"] == 7

    parts = [(2, 8, b"ij"), (0, 0, b"abcd"), (1, 4, b"efgh")]
    for number, start, data in parts:
        end = start + len(data) - 1
        response = client.put(
            "/api/uploads/segments/upload",
            params={"tenantId": "local-default",
                "uploadSessionId": upload_id,
                "segmentNumber": number,
            },
            headers={
                "Content-Range": f"bytes {start}-{end}/10",
                "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                "Content-Type": "application/octet-stream",
            },
            content=data,
        )
        assert response.status_code == 200, response.text

    current = client.get(
        "/api/uploads/get",
        params={"tenantId": "local-default", "uploadSessionId": upload_id},
    )
    assert current.json()["uploadedSizeBytes"] == 10
    assert current.json()["missingSegmentNumbers"] == []

    completed = client.post(
        "/api/uploads/complete",
        params={"tenantId": "local-default", "uploadSessionId": upload_id},
    )
    assert completed.status_code == 200, completed.text
    video_id = completed.json()["videoId"]
    job_id = _retain_historical_v1_job(jobs_repository, completed.json())
    video_parse_task_id = completed.json()["videoParseTaskId"]
    video_parse_task = client.get(
        "/api/jobs/video-parse/get",
        params={
            "tenantId": "local-default",
            "videoParseTaskId": video_parse_task_id,
        },
    )
    assert video_parse_task.status_code == 200, video_parse_task.text
    assert video_parse_task.json()["status"] == "PENDING"
    detail = video_parse_task.json()["detail"]
    assert detail["progress"]["completedChunks"] == 0
    assert detail["progress"]["topologyReady"] is False
    assert detail["timing"]["activeExecutions"] == 0
    assert video_parse_task.json()["tenantId"] == "local-default"
    assert video_parse_task.json()["seriesId"] == series_id
    assert video_parse_task.json()["seriesName"] == "Test Series"
    assert video_parse_task.json()["episodeOrder"] == 7
    assert video_parse_task.json()["originalFilename"] == "episode-07.mp4"
    assert video_parse_task.json()["uploadSessionId"] == upload_id
    assert video_parse_task.json()["durationSeconds"] == 12
    assert video_parse_task.json()["canRetry"] is False
    assert video_parse_task.json()["canReparse"] is False
    video_parse_tasks = client.get(
        "/api/jobs/video-parse/list",
        params={
            "tenantId": "local-default",
            "seriesId": series_id,
            "status": "PENDING",
        },
    )
    assert video_parse_tasks.status_code == 200, video_parse_tasks.text
    assert video_parse_tasks.json()["total"] == 1
    assert [
        item["videoParseTaskId"] for item in video_parse_tasks.json()["items"]
    ] == [video_parse_task_id]
    assert client.get(
        "/api/jobs/video-parse/get",
        params={
            "tenantId": "another-workspace",
            "videoParseTaskId": video_parse_task_id,
        },
    ).status_code == 404
    assert client.get(
        "/api/jobs/video-parse/events",
        params={
            "tenantId": "another-workspace",
            "videoParseTaskId": video_parse_task_id,
        },
    ).status_code == 404
    assert client.get(
        "/api/jobs/video-parse/list",
        params={"tenantId": "another-workspace"},
    ).json()["items"] == []
    job = client.get("/api/jobs/get", params={"tenantId": "local-default", "jobId": job_id})
    jobs = client.get("/api/jobs/list", params={"tenantId": "local-default"})
    assert job.status_code == 200
    assert job.json()["status"] == "QUEUED"
    assert job.json()["uploadSessionId"] == upload_id
    assert job.json()["progress"] == 0
    assert job.json()["overallProgress"] is None
    assert job.json()["stageProgress"] is None
    assert job.json()["completedDurationSeconds"] is None
    assert job.json()["completedThroughSeconds"] is None
    assert job.json()["videoDurationSeconds"] == 12
    assert job.json()["totalDurationSeconds"] == 12
    assert job.json()["processingDurationMs"] == 0
    assert job.json()["processingTimingComplete"] is False
    assert job.json()["completedChunkCount"] is None
    assert job.json()["totalChunkCount"] == 0
    assert job.json()["currentChunk"] is None
    assert job.json()["seriesName"] == "Test Series"
    assert job.json()["episodeOrder"] == 7
    assert job.json()["originalFilename"] == "episode-07.mp4"
    assert [item["jobId"] for item in jobs.json()["items"]] == [job_id]
    for path in ("/api/jobs/get", "/api/jobs/events"):
        outside_workspace = client.get(
            path,
            params={"jobId": job_id, "tenantId": "another-workspace"},
        )
        assert outside_workspace.status_code == 404

    queue = MySqlParseJobQueue(jobs_repository._session_factory)
    lease = queue.claim_next(
        "api-test-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    job_service = client.app.state.services.jobs
    before_heartbeat = _job_view(
        job_service.get(job_id, tenant_id="local-default")
    )
    assert queue.heartbeat(
        lease,
        lease_duration=timedelta(minutes=5),
    )
    after_heartbeat = _job_view(
        job_service.get(job_id, tenant_id="local-default")
    )
    assert _job_event_fingerprint(before_heartbeat) == _job_event_fingerprint(
        after_heartbeat
    )
    assert jobs_repository.update_progress(
        lease,
        stage=ParseJobStage.PARSING,
        progress=30,
    )
    after_progress = _job_view(
        job_service.get(job_id, tenant_id="local-default")
    )
    assert _job_event_fingerprint(after_progress) != _job_event_fingerprint(
        after_heartbeat
    )
    assert queue.acknowledge(lease, duration_ms=0)
    with client.stream(
        "GET",
        "/api/jobs/events",
        params={"tenantId": "local-default", "jobId": job_id},
    ) as events:
        body = "\n".join(events.iter_lines())
    assert "event: job" in body
    assert '"status":"SUCCEEDED"' in body

    with jobs_repository._session_factory.begin() as session:
        task = session.get(VideoParseTask, video_parse_task_id)
        task.status = "COMPLETE"
        task.finish_time = task.update_date
    with client.stream(
        "GET",
        "/api/jobs/video-parse/events",
        params={
            "tenantId": "local-default",
            "videoParseTaskId": video_parse_task_id,
        },
    ) as events:
        video_parse_body = "\n".join(events.iter_lines())
    assert "event: video-parse-task" in video_parse_body
    assert '"status":"COMPLETE"' in video_parse_body
    assert "_worker_runtime_v2" not in video_parse_body

    full_video = client.get(
        "/api/videos/content",
        params={"tenantId": "local-default", "videoId": video_id, "variant": "original"},
    )
    ranged_video = client.get(
        "/api/videos/content",
        params={"tenantId": "local-default", "videoId": video_id, "variant": "original"},
        headers={"Range": "bytes=2-5"},
    )
    assert full_video.content == b"abcdefghij"
    assert ranged_video.status_code == 206
    assert ranged_video.content == b"cdef"

    delete_params = {
        "tenantId": "local-default",
        "uploadSessionId": upload_id,
    }
    deleted = client.post("/api/uploads/delete", params=delete_params)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "EXPIRED"
    assert deleted.json()["videoId"] is None
    assert deleted.json()["jobId"] is None
    assert client.get(
        "/api/jobs/get",
        params={"tenantId": "local-default", "jobId": job_id},
    ).status_code == 404
    assert client.get(
        "/api/videos/content",
        params={"tenantId": "local-default", "videoId": video_id},
    ).status_code == 404
    repeated_delete = client.post("/api/uploads/delete", params=delete_params)
    assert repeated_delete.status_code == 200
    assert repeated_delete.json()["version"] == deleted.json()["version"]


def test_upload_rejects_wrong_content_range_and_workspace(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"tenantId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "bad-range"},
        json={"tenantId": "local-default",
            "episodeOrder": 1,
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4",
            "totalSizeBytes": 4,
        },
    )
    upload_id = created.json()["uploadSessionId"]

    wrong_workspace = client.get(
        "/api/uploads/get",
        params={
            "uploadSessionId": upload_id,
            "tenantId": "another-workspace",
        },
    )
    wrong_range = client.put(
        "/api/uploads/segments/upload",
        params={"tenantId": "local-default",
            "uploadSessionId": upload_id,
            "segmentNumber": 0,
        },
        headers={
            "Content-Range": "bytes 0-2/4",
            "X-Content-SHA256": hashlib.sha256(b"abc").hexdigest(),
        },
        content=b"abc",
    )

    assert wrong_workspace.status_code == 404
    assert wrong_range.status_code == 409
    assert wrong_range.json()["code"] == "CONFLICT"

    mismatched_total = client.put(
        "/api/uploads/segments/upload",
        params={"tenantId": "local-default", "uploadSessionId": upload_id, "segmentNumber": 0},
        headers={
            "Content-Range": "bytes 0-3/5",
            "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
        },
        content=b"abcd",
    )
    assert mismatched_total.status_code == 409
    for path, method in (
        ("/api/uploads/segments/upload", "PUT"),
        ("/api/uploads/complete", "POST"),
    ):
        outside_workspace = client.request(
            method,
            path,
            params={
                "uploadSessionId": upload_id,
                "segmentNumber": 0,
                "tenantId": "another-workspace",
            },
            headers={
                "Content-Range": "bytes 0-3/4",
                "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
            },
            content=b"abcd" if method == "PUT" else None,
        )
        assert outside_workspace.status_code == 404


@pytest.mark.parametrize(
    ("segment_number", "content_range", "data", "expected_status"),
    [
        (0, "not-a-byte-range", b"abcd", 400),
        (0, "bytes 0-3/4", b"abc", 400),
        (-1, "bytes 0-3/4", b"abcd", 422),
    ],
)
def test_upload_protocol_errors_do_not_require_business_data(
    tmp_path: Path,
    segment_number: int,
    content_range: str,
    data: bytes,
    expected_status: int,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    response = client.put(
        "/api/uploads/segments/upload",
        params={"tenantId": "local-default",
            "uploadSessionId": "00000000-0000-0000-0000-000000000000",
            "segmentNumber": segment_number,
        },
        headers={
            "Content-Range": content_range,
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert response.status_code == expected_status


def _create_agent_conversation(client):
    response = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-stream"},
        json={"tenantId": "local-default"},
    )
    assert response.status_code == 201, response.text
    return response.json()["conversationId"]


def _answer_payloads(text):
    import json
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_each_message_can_choose_its_own_series_in_one_conversation(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    first_series = _create_series(client)
    second_series = _create_series(client, name="Second Series")
    conversation_id = _create_agent_conversation(client)
    scopes = [[first_series], [second_series, first_series]]
    try:
        for index, scope in enumerate(scopes):
            response = client.post(
                "/api/agent/messages/send",
                params={"tenantId": "local-default", "conversationId": conversation_id},
                headers={"Idempotency-Key": f"scoped-message-{index}"},
                json={"tenantId": "local-default", "content": "找哭泣的画面", "seriesIds": scope, "topK": 5},
            )
            assert response.status_code == 200, response.text
            pair = response.json()
            assert set(pair["userMessage"]["seriesIds"]) == set(scope)
            assert set(pair["assistantMessage"]["seriesIds"]) == set(scope)
            assert set(pair["assistantMessage"]["searchResult"]["seriesIds"]) == set(scope)
        assert [set(call[1]) for call in retrieval.calls] == [set(scope) for scope in scopes]
        messages = client.get(
            "/api/agent/messages/list", params={"tenantId": "local-default", "conversationId": conversation_id},
        ).json()["items"]
        assert [set(message["seriesIds"]) for message in messages] == [
            set(scopes[0]), set(scopes[0]), set(scopes[1]), set(scopes[1]),
        ]
        conversation = client.get(
            "/api/agent/conversations/get", params={"tenantId": "local-default", "conversationId": conversation_id},
        ).json()
        assert conversation["messageCount"] == 4
        assert "seriesIds" not in conversation
    finally:
        client.app.state.services.agent.close()


@pytest.mark.parametrize("selection", [None, []])
def test_empty_selection_resolves_all_workspace_series_once_for_idempotent_replay(tmp_path, selection):
    client, retrieval, _ = _client(tmp_path)
    scope = {_create_series(client), _create_series(client, name="Second Series")}
    _create_series(client, tenant_id="other-workspace")
    _create_series(client, name="Not Ready Series", ready=False)
    conversation_id = _create_agent_conversation(client)
    body = {"tenantId": "local-default", "content": "找画面", "topK": 5}
    if selection is not None:
        body["seriesIds"] = selection
    kwargs = {
        "params": {"tenantId": "local-default", "conversationId": conversation_id},
        "headers": {"Idempotency-Key": "all-series-message"},
        "json": body,
    }
    try:
        sent = client.post("/api/agent/messages/send", **kwargs)
        assert sent.status_code == 200, sent.text
        assert set(sent.json()["userMessage"]["seriesIds"]) == scope
        assert set(sent.json()["assistantMessage"]["seriesIds"]) == scope
        assert set(retrieval.calls[0][1]) == scope
        _create_series(client, name="Newly Ready Series")
        replay = client.post("/api/agent/messages/send", **kwargs)
        assert replay.status_code == 200, replay.text
        assert replay.json() == sent.json()
        assert len(retrieval.calls) == 1
    finally:
        client.app.state.services.agent.close()


def test_reusing_message_key_with_different_series_conflicts_before_retrieval(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    first_series = _create_series(client)
    second_series = _create_series(client, name="Second Series")
    conversation_id = _create_agent_conversation(client)
    kwargs = {
        "params": {"tenantId": "local-default", "conversationId": conversation_id},
        "headers": {"Idempotency-Key": "selected-series-message"},
    }
    try:
        sent = client.post(
            "/api/agent/messages/send", **kwargs,
            json={"tenantId": "local-default", "content": "找画面", "seriesIds": [first_series]},
        )
        assert sent.status_code == 200, sent.text
        conflicting = client.post(
            "/api/agent/messages/send", **kwargs,
            json={"tenantId": "local-default", "content": "找画面", "seriesIds": [second_series]},
        )
        assert conflicting.status_code == 409
        assert conflicting.json()["code"] == "CONFLICT"
        assert len(retrieval.calls) == 1
        assert len(client.get(
            "/api/agent/messages/list", params={"tenantId": "local-default", "conversationId": conversation_id},
        ).json()["items"]) == 2
    finally:
        client.app.state.services.agent.close()


def test_explicit_non_ready_series_is_rejected_before_starting_message(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    series_id = _create_series(client, ready=False)
    conversation_id = _create_agent_conversation(client)
    try:
        response = client.post(
            "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "non-ready-message"},
            json={"tenantId": "local-default", "content": "找画面", "seriesIds": [series_id]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "SERIES_NOT_READY"
        assert retrieval.calls == []
        assert client.get(
            "/api/agent/messages/list", params={"tenantId": "local-default", "conversationId": conversation_id},
        ).json()["items"] == []
    finally:
        client.app.state.services.agent.close()


@pytest.mark.parametrize("weights", [
    {"transcript": 0.2, "semantic": 0.5, "visual": 0.3},
    {"lexical": 0.2, "semantic": 0.5, "visual": 0.3},
])
def test_agent_evidence_view_preserves_old_and_new_retrieval_trace_weights(weights):
    raw = {
        "query": "门口",
        "seriesIds": ["series-1"],
        "searchedVideoIds": [],
        "partial": False,
        "warnings": [],
        "hits": [],
        "agentReply": {"message": "暂无片段", "resultCount": 0},
        "retrievalTrace": [{
            "query": "门口",
            "topK": 5,
            "strategyVersion": "stored-strategy",
            "requestedWeights": weights,
            "effectiveWeights": weights,
            "resultCount": 0,
            "videoChunkIds": [],
        }],
    }

    view = _agent_search_evidence_view(
        raw, SimpleNamespace(), tenant_id="local-default",
    )

    serialized = view.model_dump(mode="json", by_alias=True)
    assert "query" not in serialized
    assert serialized["retrievalTrace"] == raw["retrievalTrace"]


def test_agent_evidence_view_reads_legacy_single_query_without_trace():
    stored = {
        "query": "历史单值检索词",
        "seriesIds": ["series-1"],
        "hits": [],
        "agentReply": {"message": "暂无片段", "resultCount": 0},
    }

    view = _agent_search_evidence_view(
        stored, SimpleNamespace(), tenant_id="local-default",
    )

    serialized = view.model_dump(mode="json", by_alias=True)
    assert "query" not in serialized
    assert serialized["retrievalTrace"] == []


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
@pytest.mark.parametrize("leading_text", [True, False])
def test_joined_chunk_evidence_in_json_and_sse_always_contains_series_id(
    tmp_path, prepared_import, caplog, accept, leading_text,
):
    caplog.set_level(logging.INFO, logger="server.api.agent")
    client, _, _ = _client(tmp_path)
    service = client.app.state.services.agent
    video_repository = VideoRepository(service._repository._session_factory)
    report = video_repository.import_video_parse(prepared_import)
    with service._repository._session_factory.begin() as session:
        session.get(Series, prepared_import.series_id).status = "READY"
    conversation_id = _create_agent_conversation(client)

    def joined_search(
        query,
        *,
        tenant_id,
        series_ids,
        top_k,
        lexical_weight,
        text_weight,
        vision_weight,
    ):
        assert tenant_id == prepared_import.tenant_id
        assert series_ids == [prepared_import.series_id]
        assert lexical_weight + text_weight + vision_weight == pytest.approx(1.0)
        evidence = video_repository.hydrate_video_chunks(report.video_chunk_ids)
        hits = [
            VideoChunkSearchHit(
                candidate=VideoChunkCandidate(
                    video_chunk_id=chunk.video_chunk_id, fused_rank=index + 1, fused_score=1,
                ),
                selection_reason=SelectionReason(
                    summary="画面中有对应动作。", matched_evidence=[], limitations=[],
                ),
                evidence=chunk,
            )
            for index, chunk in enumerate(evidence[:top_k])
        ]
        return VideoChunkSearchResult(
            query=query, series_ids=series_ids,
            agent_reply=AgentReply(message="找到动作片段。", result_count=len(hits)), hits=hits,
        )

    class QueryWithEvidence(_AgentModelStub):
        def run_structured(self, _prompt, *, tools, result_type, **_kwargs):
            tools_by_name = {tool.name: tool for tool in tools}
            result = json.loads(tools_by_name["retrieve"].invoke({
                "query": "找动作片段",
                "top_k": 5,
                "channel_weights": {
                    "lexical": 0.2,
                    "semantic": 0.2,
                    "visual": 0.6,
                },
            }))
            for item in result["items"]:
                _read_all(tools_by_name["get_context_canvas"], item["video_chunk_id"])
                _write_canvas(tools_by_name, "upsert_draft_item", {
                    "video_chunk_id": item["video_chunk_id"],
                    "start_seconds": item["start_seconds"],
                    "end_seconds": item["end_seconds"],
                    "claim": "画面中有对应动作。",
                })
            return result_type.model_validate({
                "plan_context": "召回动作并检查片段来源。",
                "message": "这些片段包含动作。",
            })

    service._runtime._video_runtime._retrieve = joined_search
    service._runtime._video_runtime._query_model = QueryWithEvidence("query-evidence")
    class SupervisorModel:
        model_name = "supervisor-evidence"

        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            by_name = {tool.name: tool for tool in tools}
            if leading_text:
                handle_text("以下片段支持这个结论。")
            report = json.loads(by_name["query_video"].invoke({"query": "找动作片段"}))["reports"][0]
            for draft in report["draft_items"]:
                _read_all(by_name["get_context_canvas"], draft["video_chunk_id"], collection="retrieval")
                by_name["submit_video_item"].invoke({
                    **draft, "claim": "这些片段包含动作。",
                })
            handle_text("这些片段包含动作。")

        def confirm_turn(self, *_args):
            pass

        def discard_turn(self, *_args):
            pass

        def delete_thread(self, *_args):
            pass

    service._runtime = SupervisorRuntime(
        service._runtime._video_runtime, SupervisorModel(), SimpleNamespace(),
        max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=16_384,
        max_prompt_chars=200_000,
    )
    service._runtimes = [service._runtime]
    try:
        response = client.post(
            "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
            headers={"Accept": accept, "Idempotency-Key": "joined-evidence"},
            json={"tenantId": "local-default", "content": "找动作片段", "seriesIds": [prepared_import.series_id]},
        )
        assert response.status_code == 200, response.text
        if accept == "text/event-stream":
            events = _answer_payloads(response.text)
            final = events[-1]["assistantMessage"]
            snapshots = [event["assistantMessage"] for event in events if event["type"] == "answer.snapshot"]
            assert snapshots and all(message["status"] == "PROCESSING" for message in snapshots)
            if leading_text:
                assert snapshots[0]["content"] == "以下片段支持这个结论。"
                assert snapshots[0]["searchResult"] is None
            else:
                assert snapshots[0]["content"] == ""
                assert [block["type"] for block in snapshots[0]["renderBlocks"]] == ["tool"]
                assert snapshots[0]["renderBlocks"][0]["status"] == "inProgress"
                assert snapshots[0]["searchResult"] is None
            assert snapshots[-1]["renderBlocks"] == final["renderBlocks"]
            assert snapshots[-1]["searchResult"] == final["searchResult"]
            assert snapshots[-1]["content"] == final["content"]
            assert not {"answer.evidence", "answer.block", "answer.delta"} & {event["type"] for event in events}
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        else:
            final = response.json()["assistantMessage"]
        assert "agentReply" not in final["searchResult"]
        assert "query" not in final["searchResult"]
        assert final["searchResult"]["retrievalTrace"] == [{
            "query": "找动作片段",
            "topK": 5,
            "strategyVersion": "query-adaptive-three-channel-rrf-v2",
            "requestedWeights": {"lexical": 0.2, "semantic": 0.2, "visual": 0.6},
            "effectiveWeights": {"lexical": 0.2, "semantic": 0.2, "visual": 0.6},
            "resultCount": len(report.video_chunk_ids),
            "videoChunkIds": list(report.video_chunk_ids), "role": "query",
        }]
        hits = final["searchResult"]["hits"]
        assert len(hits) == len(report.video_chunk_ids) == 2
        tools = [block for block in final["renderBlocks"] if block["type"] == "tool"]
        assert len(tools) == 1 and tools[0]["callId"]
        assert tools[0] == {
            "type": "tool", "callId": tools[0]["callId"], "tool": "search_video_graph",
            "status": "completed", "query": "找动作片段",
        }
        assert final["renderBlocks"] == [
            *([{"type": "markdown", "markdown": "以下片段支持这个结论。"}] if leading_text else []),
            *tools,
            *[
                {
                    "type": "video",
                    "videoChunkId": hit["candidate"]["videoChunkId"],
                    "startSeconds": hit["evidence"]["startSeconds"],
                    "endSeconds": hit["evidence"]["endSeconds"],
                }
                for hit in hits
            ],
            {"type": "markdown", "markdown": "这些片段包含动作。"},
        ]
        assert final["seriesIds"] == [prepared_import.series_id]
        for hit in hits:
            evidence = hit["evidence"]
            assert evidence["seriesId"] == prepared_import.series_id
            assert evidence["tenantId"] == prepared_import.tenant_id
            assert evidence["seriesName"] == prepared_import.series_name
            assert evidence["videoId"] == prepared_import.video_id
            assert evidence["videoChunkId"] == hit["candidate"]["videoChunkId"]
            assert 0 <= evidence["startSeconds"] < evidence["endSeconds"] <= evidence["durationSeconds"]
    finally:
        service.close()


def test_same_send_path_supports_live_events_and_idempotent_replay(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    series_id = _create_series(client)
    conversation_id = _create_agent_conversation(client)
    kwargs = {
        "params": {"tenantId": "local-default", "conversationId": conversation_id},
        "headers": {"Accept": "text/event-stream", "Idempotency-Key": "stream-message"},
        "json": {"tenantId": "local-default", "content": "找哭泣的画面", "topK": 5},
    }
    response = client.post("/api/agent/messages/send", **kwargs)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _answer_payloads(response.text)
    assert events[0]["type"] == "answer.started" and events[-1]["type"] == "answer.completed"
    snapshots = [event["assistantMessage"] for event in events if event["type"] == "answer.snapshot"]
    assert snapshots
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["conversationId"] == conversation_id for event in events)
    assert len({event["messageId"] for event in events}) == 1
    assert events[0]["assistantMessage"]["status"] == "PROCESSING"
    assert events[0]["assistantMessage"]["content"] is None
    assert events[0]["assistantMessage"]["timing"]["totalDurationMs"] is None
    assert events[0]["assistantMessage"]["timing"]["stages"] == []
    assert type(events[0]["assistantMessage"]["timing"]["startedAt"]) is int
    progress = [event for event in events if event["type"] == "answer.progress"]
    assert all(event["timing"]["totalDurationMs"] is None for event in progress)
    assert events[0]["userMessage"]["modelName"] is None
    assert events[0]["userMessage"]["seriesIds"] == [series_id]
    assert events[0]["assistantMessage"]["seriesIds"] == [series_id]
    final = events[-1]["assistantMessage"]
    assert final["status"] == "COMPLETED"
    assert final["timing"]["totalDurationMs"] == sum(
        stage["durationMs"] for stage in final["timing"]["stages"]
    )
    assert [stage["phase"] for stage in final["timing"]["stages"]] == [
        "QUEUED", "RETRIEVING", "RESPONDING",
    ]
    assert final["content"] == snapshots[-1]["content"]
    assert final["renderBlocks"] == snapshots[-1]["renderBlocks"]
    persisted = client.get("/api/agent/messages/list", params={"tenantId": "local-default", "conversationId": conversation_id}).json()["items"]
    assert final == persisted[-1]
    replay = _answer_payloads(client.post("/api/agent/messages/send", **kwargs).text)
    assert [event["type"] for event in replay] == ["answer.started", "answer.completed"]
    assert replay[-1]["messageId"] == final["messageId"]
    assert replay[-1]["assistantMessage"]["timing"] == final["timing"]
    assert len(retrieval.calls) == 1
    content = client.get("/openapi.json").json()["paths"]["/api/agent/messages/send"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"}.issubset(content)
    client.app.state.services.agent.close()


def test_manual_cancel_api_checks_scope_and_returns_the_current_message_pair(tmp_path, monkeypatch):
    from threading import Event
    from agent.chat.common.errors import AgentModelInvocationError

    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    service = client.app.state.services.agent
    entered, release = Event(), Event()
    query = service._runtime._video_runtime._query_model.run_structured

    def blocked_query(*args, **kwargs):
        result = query(*args, **kwargs)
        entered.set()
        assert release.wait(5), "test did not release Query"
        return result

    monkeypatch.setattr(service._runtime._video_runtime._query_model, "run_structured", blocked_query)
    execution = service.start_message(
        conversation_id, tenant_id="local-default", content="找画面",
        idempotency_key="api-cancel",
    )
    params = {
        "conversationId": conversation_id, "tenantId": "local-default",
        "messageId": execution.initial.assistant_message.message_id,
    }
    try:
        assert entered.wait(5), "Query did not start"
        assert client.post("/api/agent/messages/cancel", params={**params, "tenantId": "other"}).status_code == 404
        assert client.post("/api/agent/messages/cancel", params={**params, "messageId": str(uuid4())}).status_code == 404
        assert client.post("/api/agent/messages/cancel", params={
            **params, "messageId": execution.initial.user_message.message_id,
        }).status_code == 400
        assert client.post("/api/agent/messages/cancel", params={"conversationId": conversation_id}).status_code == 422
        with monkeypatch.context() as scoped:
            scoped.setattr("server.api.agent.get_identity", lambda _request: SimpleNamespace(
                mode="cloud", user_id="other-user", tenant_id="local-default", public_tenant_id="public",
            ))
            assert client.post("/api/agent/messages/cancel", params=params).status_code == 404
        assert not execution.cancellation.is_set()

        response = client.post("/api/agent/messages/cancel", params=params)
        assert response.status_code == 200, response.text
        pair = response.json()
        assert pair["userMessage"]["messageId"] == execution.initial.user_message.message_id
        assert pair["assistantMessage"]["messageId"] == params["messageId"]
        assert pair["assistantMessage"]["status"] == "PROCESSING"
        assert execution.cancellation.is_set() and not execution.future.done()
        assert client.post("/api/agent/messages/cancel", params=params).json() == pair
    finally:
        release.set()
        service.close()

    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        execution.result()
    final = client.get("/api/agent/messages/get", params=params)
    assert final.status_code == 200
    assert final.json()["assistantMessage"]["status"] == "FAILED"
    assert final.json()["assistantMessage"]["lastError"] == {
        "code": "AGENT_CANCELLED", "message": "已停止生成回答。",
    }
    assert client.post("/api/agent/messages/cancel", params=params).json() == final.json()


def test_supervisor_failure_sends_failed_event_and_json_maps_harness_error(tmp_path):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    runtime = client.app.state.services.agent._runtime
    def failed_supervisor(*_args, **_kwargs):
        from agent.chat.common.errors import AgentModelInvocationError
        raise AgentModelInvocationError("private provider error")
    runtime._model.run_agent = failed_supervisor
    response = client.post(
        "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "rejected-stream"},
        json={"tenantId": "local-default", "content": "找摔杯子的画面"},
    )
    events = _answer_payloads(response.text)
    assert events[0]["type"] == "answer.started" and events[-1]["type"] == "answer.failed"
    assert events[-1]["error"]["code"] == "AGENT_MODEL_ERROR"
    assert events[-1]["assistantMessage"]["status"] == "FAILED"
    assert "需要重新找摔杯子的片段" not in events[-1]["error"]["message"]
    json_response = client.post(
        "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "rejected-json"}, json={"tenantId": "local-default", "content": "重试"},
    )
    assert json_response.status_code == 502
    assert json_response.json()["code"] == "AGENT_MODEL_ERROR"
    outside = client.post(
        "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "outside"},
        json={"content": "隔离", "tenantId": "other-workspace"},
    )
    assert outside.status_code == 404
    client.app.state.services.agent.close()


def test_agent_old_prompt_character_threshold_allows_completed_response(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    runtime = client.app.state.services.agent._runtime
    runtime._max_prompt_chars = 100
    calls = []
    for role, model, method in (
        ("query", runtime._video_runtime._query_model, "run_structured"),
        ("chat", runtime._model, "run_agent_loop"),
    ):
        invoke = getattr(model, method)

        def record_call(prompt, *, _role=role, _invoke=invoke, **kwargs):
            calls.append((_role, prompt))
            return _invoke(prompt, **kwargs)

        monkeypatch.setattr(model, method, record_call)

    response = client.post(
        "/api/agent/messages/send",
        params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "old-character-threshold"},
        json={"tenantId": "local-default", "content": "找摔杯子的画面"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["assistantMessage"]["status"] == "COMPLETED"
    assert response.json()["assistantMessage"]["lastError"] is None
    assert [role for role, _prompt in calls] == ["query", "chat"]
    assert all(len(prompt) > 100 for _role, prompt in calls)
    stored = client.get("/api/agent/messages/get", params={
        "tenantId": "local-default", "conversationId": conversation_id,
        "idempotencyKey": "old-character-threshold",
    })
    assert stored.status_code == 200 and stored.json() == response.json()
    client.app.state.services.agent.close()


def test_agent_provider_context_error_keeps_actionable_http_mapping(tmp_path):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    runtime = client.app.state.services.agent._runtime
    runtime._max_prompt_chars = 100

    class CapacityFailureSupervisor(_AgentModelStub):
        calls = 0

        def run_agent(self, _prompt, **_kwargs):
            self.calls += 1
            raise ModelInputTooLargeError("Provider context capacity exceeded")

    query = CapacityFailureSupervisor("capacity-failure")
    runtime._model = query

    response = client.post(
        "/api/agent/messages/send",
        params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "context-budget"},
        json={"tenantId": "local-default", "content": "找摔杯子的画面"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "AGENT_CONTEXT_BUDGET_EXCEEDED"
    assert query.calls == 1
    stored = client.get("/api/agent/messages/get", params={
        "tenantId": "local-default", "conversationId": conversation_id,
        "idempotencyKey": "context-budget",
    })
    assert stored.status_code == 200
    assert stored.json()["assistantMessage"]["status"] == "FAILED"
    assert stored.json()["assistantMessage"]["lastError"]["code"] == response.json()["code"]
    client.app.state.services.agent.close()


def test_asgi_publishes_only_after_tools_and_final_text_succeed(tmp_path):
    import asyncio
    import json
    from threading import Event

    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    service = client.app.state.services.agent
    provider_entered = Event()
    release = Event()
    original_call = service._runtime._model.run_agent_loop

    def blocked_provider(*args, **kwargs):
        provider_entered.set()
        assert release.wait(5), "test did not release Provider"
        return original_call(*args, **kwargs)
    service._runtime._model.run_agent_loop = blocked_provider
    body = json.dumps({"tenantId": "local-default", "content": "找画面"}).encode()

    async def exercise():
        request_done = False
        never_disconnected = asyncio.Event()
        output = []

        async def receive():
            nonlocal request_done
            if not request_done:
                request_done = True
                return {"type": "http.request", "body": body, "more_body": False}
            await never_disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            output.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/agent/messages/send", "raw_path": b"/api/agent/messages/send",
            "root_path": "", "query_string": f"conversationId={conversation_id}".encode(),
            "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"), (b"idempotency-key", b"live-asgi")],
            "client": ("127.0.0.1", 10000), "server": ("testserver", 80),
        }
        task = asyncio.create_task(client.app(scope, receive, send))
        try:
            entered = await asyncio.wait_for(
                asyncio.to_thread(provider_entered.wait, 3), timeout=4,
            )
            assert entered and not release.is_set() and not task.done()
            streamed = b"".join(
                message.get("body", b"")
                for message in output
                if message["type"] == "http.response.body"
            )
            assert b"event: answer.delta" not in streamed
            assert b"event: answer.evidence" not in streamed
            assert service.list_all_messages(conversation_id, tenant_id="local-default")[-1].status == "PROCESSING"
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=3)
        events = _answer_payloads(b"".join(message.get("body", b"") for message in output if message["type"] == "http.response.body").decode())
        snapshots = [event["assistantMessage"] for event in events if event["type"] == "answer.snapshot"]
        assert snapshots
        assert events[-1]["assistantMessage"]["content"] == snapshots[-1]["content"]

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("accept,expected", [
    ("text/event-stream;q=0", "application/json"),
    ("application/json;q=1,text/event-stream;q=0.5", "application/json"),
    ("application/json;q=0.5,text/event-stream;q=1", "text/event-stream"),
])
def test_send_respects_accept_quality(tmp_path, accept, expected):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    response = client.post(
        "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
        headers={"Accept": accept, "Idempotency-Key": "accept-message"},
        json={"tenantId": "local-default", "content": "找画面"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected)
    client.app.state.services.agent.close()


@pytest.mark.parametrize("failure,expected_status,expected_code", [
    ("not-ready", 409, "SERIES_NOT_READY"),
    ("missing-scope", 404, "NOT_FOUND"),
    ("index-inconsistent", 409, "CONFLICT"),
])
def test_retrieval_domain_errors_keep_http_mapping_through_harness(tmp_path, failure, expected_status, expected_code):
    from server.repository.video import SeriesNotReadyError, SeriesScopeNotFoundError
    from server.service.video_retrieval import RetrievalIndexConsistencyError
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    error_type = {
        "not-ready": SeriesNotReadyError,
        "missing-scope": SeriesScopeNotFoundError,
        "index-inconsistent": RetrievalIndexConsistencyError,
    }[failure]
    def failed_search(*_args, **_kwargs):
        raise error_type("retrieval source unavailable")
    service = client.app.state.services.agent
    service._runtime._video_runtime._retrieve = failed_search
    def direct_search(_prompt, *, tools, **_kwargs):
        available = {tool.name: tool for tool in tools}
        available["retrieve"].invoke({"query": "找画面", "top_k": 3, "channel_weights": {"lexical": 0.2, "semantic": 0.6, "visual": 0.2}})
    service._runtime._model.run_agent = direct_search
    try:
        response = client.post(
            "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "retrieval-error"}, json={"tenantId": "local-default", "content": "找画面"},
        )
        assert response.status_code == expected_status
        assert response.json()["code"] == expected_code
        failed = service.list_all_messages(conversation_id, tenant_id="local-default")[-1]
        assert failed.status == "FAILED" and failed.last_error is not None
        assert "retrieval source unavailable" not in failed.last_error.message
        response = client.post(
            "/api/agent/messages/send", params={"tenantId": "local-default", "conversationId": conversation_id},
            headers={"Accept": "text/event-stream", "Idempotency-Key": "retrieval-stream-error"},
            json={"tenantId": "local-default", "content": "再找画面"},
        )
        events = _answer_payloads(response.text)
        assert [event["type"] for event in events] == [
            "answer.started", "answer.snapshot", "answer.progress", "answer.snapshot", "answer.failed",
        ]
        assert events[-1]["assistantMessage"]["renderBlocks"] == events[-2]["assistantMessage"]["renderBlocks"]
        assert events[-1]["error"]["code"] == "PROCESSING_FAILED"
        assert events[-1]["assistantMessage"]["status"] == "FAILED"
    finally:
        service.close()


@pytest.mark.parametrize(("header_value", "expected_mode"), [
    (None, "release"),
    ("", "release"),
    ("0", "release"),
    ("01", "release"),
    ("true", "release"),
    ("debug", "release"),
    (" 1", "release"),
    ("1 ", "release"),
    ("1,0", "release"),
    ("1", "debug"),
])
def test_runtime_config_requires_exact_ppe_header(tmp_path, header_value, expected_mode):
    client, _, _ = _client(tmp_path)
    try:
        headers = {} if header_value is None else {"x-use-ppe": header_value}
        response = client.get("/api/system/runtime-config", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"displayMode": expected_mode}
    finally:
        client.app.state.services.agent.close()


@pytest.mark.parametrize("auth_mode", ["local", "cloud"])
def test_runtime_config_is_public_uncached_and_request_scoped(tmp_path, auth_mode):
    from server.config.auth import AuthConfig

    client, _, _ = _client(tmp_path)
    client.app.state.auth_config = AuthConfig(
        mode=auth_mode,
        secret_key="runtime-config-test-secret-key-32",
    )
    try:
        for headers, expected_mode in [
            ({"X-Use-PPE": "1"}, "debug"),
            ({}, "release"),
            ({"X-Use-PPE": "0"}, "release"),
            ({"X-Use-PPE": "1"}, "debug"),
        ]:
            response = client.get("/api/system/runtime-config", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == {"displayMode": expected_mode}
            assert response.headers["Cache-Control"] == "private, no-store"
            assert response.headers["Vary"] == "x-use-ppe"
            assert "Set-Cookie" not in response.headers
    finally:
        client.app.state.services.agent.close()


def test_ui_error_and_time_contracts_require_explicit_workspace(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        health = client.get("/healthz")
        assert health.json()["status"] == "ok"
        assert isinstance(health.json()["serverTime"], int)
        assert health.json()["components"]["parseConsumer"] == {
            "status": "running",
            "failure": None,
        }
        consumer = client.app.state.services.parse_consumer
        consumer.failure = RuntimeError("internal database detail")
        degraded = client.get("/healthz")
        assert degraded.json()["status"] == "degraded"
        assert degraded.json()["components"]["parseConsumer"] == {
            "status": "failed",
            "failure": "RuntimeError",
        }
        assert "internal database detail" not in degraded.text
        response = client.get("/api/series/list")
        assert response.status_code == 422
        error = response.json()
        assert error["code"] == "VALIDATION_ERROR"
        assert error["requestId"] == response.headers["X-Request-Id"]
        assert error["fieldErrors"][0]["field"] == "tenantId"
        assert error["retryAt"] is None
        created = client.post("/api/agent/conversations/create",
            headers={"Idempotency-Key": "ui-timestamp"}, json={"tenantId": "local-default"})
        for field in ("createdAt", "updatedAt"):
            assert type(created.json()[field]) is int
        assert created.json()["lastMessagePreview"] is None
    finally:
        client.app.state.services.agent.close()


def test_upload_list_has_precise_total_and_cursor_bound_to_workspace(tmp_path):
    client, _, _ = _client(tmp_path)
    try:
        series_id = _create_series(client)
        for episode in (1, 2):
            response = client.post("/api/uploads/create", params={"seriesId": series_id},
                headers={"Idempotency-Key": f"paged-upload-{episode}"}, json={
                    "tenantId": "local-default", "episodeOrder": episode,
                    "originalFilename": f"episode-{episode}.mp4", "mediaType": "video/mp4", "totalSizeBytes": 4,
                })
            assert response.status_code == 201, response.text
        params = {"tenantId": "local-default", "limit": 1}
        page = client.get("/api/uploads/list", params=params).json()
        assert page["total"] == 2 and page["hasMore"]
        assert len(page["items"]) == 1
        last = client.get("/api/uploads/list", params={**params, "cursor": page["nextCursor"]}).json()
        assert last["total"] == 2 and not last["hasMore"] and last["nextCursor"] is None
        assert page["items"][0]["uploadSessionId"] != last["items"][0]["uploadSessionId"]
        mismatch = client.get("/api/uploads/list", params={**params, "tenantId": "other", "cursor": page["nextCursor"]})
        assert mismatch.status_code == 400
    finally:
        client.app.state.services.agent.close()


def test_agent_message_options_and_openapi_come_from_service_configuration(tmp_path):
    from dataclasses import replace

    client, _, _ = _client(tmp_path)
    service = client.app.state.services.agent
    try:
        service._config = replace(
            service._config,
            default_top_k=5,
            maximum_top_k=7,
            max_model_calls_per_turn=8,
            maximum_model_calls_per_turn=10,
            max_tool_calls_per_turn=30,
            maximum_tool_calls_per_turn=40,
        )
        response = client.get("/api/agent/messages/options")
        assert response.status_code == 200
        assert response.json() == {
            "topK": {"defaultValue": 5, "minimum": 1, "maximum": 7},
            "maxModelCallsPerTurn": {
                "defaultValue": 8,
                "minimum": 1,
                "maximum": 10,
            },
            "maxToolCallsPerTurn": {
                "defaultValue": 30,
                "minimum": 1,
                "maximum": 40,
            },
            "maxLoopTurns": {
                "defaultValue": 100,
                "minimum": 1,
                "maximum": 100,
            },
        }
        schema = client.app.openapi()["components"]["schemas"]["AgentMessageSendRequest"]
        for name, default, maximum in (
            ("topK", 5, 7),
            ("maxModelCallsPerTurn", 8, 10),
            ("maxToolCallsPerTurn", 30, 40),
        ):
            field = schema["properties"][name]
            assert field["default"] == default
            integer = next(value for value in field["anyOf"] if value["type"] == "integer")
            assert integer["minimum"] == 1
            assert integer["maximum"] == maximum
        loop_field = schema["properties"]["maxLoopTurns"]
        loop_integer = next(value for value in loop_field["anyOf"] if value["type"] == "integer")
        assert loop_integer["minimum"] == 0
        assert loop_integer["maximum"] == 100
        conversation_id = _create_agent_conversation(client)
        rejected = client.post("/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "too-many-results"},
            json={"tenantId": "local-default", "content": "找画面", "topK": 8})
        assert rejected.status_code == 400
        rejected = client.post("/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "too-many-model-calls"},
            json={"tenantId": "local-default", "content": "找画面", "maxModelCallsPerTurn": 11})
        assert rejected.status_code == 400
        rejected = client.post("/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "too-many-loop-turns"},
            json={"tenantId": "local-default", "content": "找画面", "maxLoopTurns": 101})
        assert rejected.status_code == 422
    finally:
        service.close()


def test_global_agent_settings_read_patch_and_revision_conflict(tmp_path):
    client, _, _ = _client(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes((Path(__file__).resolve().parents[2] / "config.yaml").read_bytes())
    client.app.state.model_config_path = config_path
    service = client.app.state.services.agent
    try:
        current = client.get("/api/agent/global-settings")
        assert current.status_code == 200, current.text
        before = current.json()
        assert before["restartRequiredFields"] == ["maximum_concurrent_kicks"]
        assert before["activeValues"] == {
            "maximum_concurrent_kicks": service.active_maximum_concurrent_kicks,
        }
        assert "agent_model" not in before["values"]

        changed_concurrency = before["values"]["maximum_concurrent_kicks"] + 1
        changed_catalog = before["values"]["max_catalog_items_per_page"] - 1
        patch = {
            "revision": before["revision"],
            "values": {
                "maximum_concurrent_kicks": changed_concurrency,
                "max_catalog_items_per_page": changed_catalog,
            },
        }
        saved = client.patch("/api/agent/global-settings", json=patch)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] != before["revision"]
        assert saved.json()["values"]["maximum_concurrent_kicks"] == changed_concurrency
        assert saved.json()["values"]["max_catalog_items_per_page"] == changed_catalog
        assert saved.json()["activeValues"] == before["activeValues"]
        assert client.get("/api/agent/global-settings").json() == saved.json()
        assert "agent_runtime:" in config_path.read_text()

        stale = client.patch("/api/agent/global-settings", json=patch)
        assert stale.status_code == 409
        assert stale.json()["code"] == "CONFLICT"
        assert "X-Request-Id" in stale.headers
        invalid = client.patch("/api/agent/global-settings", json={
            "revision": saved.json()["revision"],
            "values": {"unknown_setting": 1},
        })
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "INVALID_REQUEST"
    finally:
        service.close()


def test_global_agent_settings_patch_requires_loopback(tmp_path):
    client, _, _ = _client(tmp_path)
    service = client.app.state.services.agent
    try:
        remote = TestClient(client.app, client=("192.0.2.2", 50000))
        response = remote.patch("/api/agent/global-settings", json={
            "revision": "unused",
            "values": {"default_top_k": 3},
        })
        assert response.status_code == 403
    finally:
        service.close()


def test_ui_cors_allows_stream_and_media_headers(tmp_path, monkeypatch):
    from server.config.settings import ServerSettings

    monkeypatch.setenv("ANIVAG_ENVIRONMENT", "test")
    monkeypatch.setenv("ANIVAG_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ANIVAG_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("ANIVAG_QDRANT_COLLECTION", "ui_contract_test")
    monkeypatch.setenv("ANIVAG_ALLOWED_ORIGINS", "http://localhost:5173")
    existing, _, _ = _client(tmp_path)
    service = existing.app.state.services.agent
    try:
        client = TestClient(create_app(existing.app.state.services, settings=ServerSettings.from_environment()))
        requested_headers = "Range, If-Range, If-None-Match, Last-Event-ID, Accept, Content-Range, X-Content-SHA256"
        preflight = client.options("/api/videos/content", headers={"Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": requested_headers})
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        health = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        exposed = health.headers["Access-Control-Expose-Headers"]
        assert all(name in exposed for name in ("Content-Range", "Content-Length", "ETag", "X-Request-Id"))
        settings_preflight = client.options("/api/agent/global-settings", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "Content-Type",
        })
        assert settings_preflight.status_code == 200, settings_preflight.text
        runtime_preflight = client.options("/api/system/runtime-config", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Use-PPE",
        })
        assert runtime_preflight.status_code == 200, runtime_preflight.text
        runtime_config = client.get("/api/system/runtime-config", headers={
            "Origin": "http://localhost:5173", "X-Use-PPE": "1",
        })
        assert runtime_config.json() == {"displayMode": "debug"}
        assert runtime_config.headers["Cache-Control"] == "private, no-store"
        assert runtime_config.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        assert {part.strip().lower() for part in runtime_config.headers["Vary"].split(",")} == {
            "x-use-ppe", "origin",
        }
    finally:
        service.close()
