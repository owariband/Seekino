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

from server.api.app import create_app
from server.agent.runtime import AgentRuntime
from server.api.agent import _agent_render_blocks
from server.api.jobs import _job_event_fingerprint, _job_view
from server.bootstrap import ApplicationServices
from server.config.media import ImageUploadConfig
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
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoChunkCheckpointStage,
)
from server.model.dto.agentDTO import AgentMessageState
from server.model.video import SeriesRecord, VideoRecord
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
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]]

    def search(
        self,
        query: str,
        *,
        workspace_id: str,
        series_ids,
        top_k: int,
        transcript_weight: float | None = None,
        text_weight: float | None = None,
        vision_weight: float | None = None,
    ):
        if any(
            weight is not None
            for weight in (transcript_weight, text_weight, vision_weight)
        ):
            assert None not in (transcript_weight, text_weight, vision_weight)
            assert transcript_weight + text_weight + vision_weight == pytest.approx(1.0)
        scope = list(series_ids)
        self.calls.append((query, scope, top_k, workspace_id))
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
        reply_to_message_id="user",
        retrieval_query="query",
        search_result={
            "renderBlocks": [
                {"type": "markdown", "markdown": "第一段"},
                {
                    "type": "video",
                    "videoChunkId": "known-2",
                    "startMs": 1200,
                    "endMs": 1800,
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
            evidence=SimpleNamespace(start_ms=1000, end_ms=2000),
        ),
        SimpleNamespace(
            candidate=SimpleNamespace(video_chunk_id="known-2"),
            evidence=SimpleNamespace(start_ms=1000, end_ms=2000),
        ),
    ])

    blocks = _agent_render_blocks(state, projected)

    assert [block.model_dump(mode="json", by_alias=True) for block in blocks] == [
        {"type": "markdown", "markdown": "第一段"},
        {
            "type": "video",
            "videoChunkId": "known-2",
            "startMs": 1200,
            "endMs": 1800,
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
    assert "proposed_start_ms=None proposed_end_ms=None" in warnings[1]
    assert "evidence_start_ms=1000 evidence_end_ms=2000" in warnings[1]
    assert "action=drop_invalid_range" in warnings[1]
    legacy = _agent_render_blocks(
        replace(state, search_result={}),
        projected,
    )
    assert [block.type for block in legacy] == ["markdown"]


@pytest.mark.parametrize(("start_ms", "end_ms"), [
    (1500, 1500),
    (1500, 1400),
    (999, 1800),
    (1200, 2001),
    (True, 1800),
])
def test_agent_render_blocks_drops_invalid_known_chunk_range(
    caplog, start_ms, end_ms,
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
        reply_to_message_id="user",
        retrieval_query="query",
        search_result={
            "renderBlocks": [{
                "type": "video",
                "videoChunkId": "known",
                "startMs": start_ms,
                "endMs": end_ms,
            }],
        },
        last_error=None,
        created_at=1_789_603_200_000,
        updated_at=1_789_603_200_000,
    )
    projected = SimpleNamespace(hits=[SimpleNamespace(
        candidate=SimpleNamespace(video_chunk_id="known"),
        evidence=SimpleNamespace(start_ms=1000, end_ms=2000),
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
    assert "evidence_start_ms=1000 evidence_end_ms=2000" in warning
    assert "action=drop_invalid_range" in warning


@dataclass
class _AgentModelStub:
    model_name: str
    provider_name: str = "test"

    def run_structured(
        self, _prompt, *, tools, result_type, max_model_calls, **_kwargs,
    ):
        tools_by_name = {tool.name: tool for tool in tools}
        if "action" in result_type.model_fields:
            catalog = json.loads(tools_by_name["get_draft_catelog"].invoke({}))
            for item in catalog["items"]:
                cursor = None
                while True:
                    page = json.loads(tools_by_name["read_draft_item"].invoke({
                        "video_chunk_id": item["video_chunk_id"],
                        "cursor": cursor,
                    }))
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
            return result_type.model_validate({
                "action": "complete", "advice": "回答与证据一致，可以交付。",
            })
        tools_by_name["retrieve"].invoke({
            "query": "她为什么哭",
            "top_k": 5,
            "channel_weights": {
                "transcript": 0.1, "semantic": 0.7, "visual": 0.2,
            },
        })
        return result_type.model_validate({
            "plan_context": "召回哭泣的片段并检查画面和对白。",
            "message": "没有找到可以核验的候选画面。",
        })

    def run_agent_loop(
        self, prompt, *, tools: Sequence[BaseTool], max_model_calls, handle_text,
        require_tool_call=False,
        request_timeout_seconds=None, check_control=None, max_input_chars=None,
    ):
        import json

        update_video_item = next(tool for tool in tools if tool.name == "update_video_item")
        assert set(update_video_item.args_schema.model_fields) == {"video_chunk_id", "claim"}
        assert max_model_calls > 0
        assert request_timeout_seconds is not None
        assert request_timeout_seconds > 0
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        approved = context["approved_turn"]
        chunk_ids = approved["video_chunk_ids"]
        assert require_tool_call is bool(chunk_ids)
        if not chunk_ids:
            handle_text(approved["message"])
            return
        handle_text("以下片段支持这个结论。")
        for video_chunk_id in chunk_ids:
            check_control and check_control()
            update_video_item.invoke({
                "video_chunk_id": video_chunk_id,
                "claim": f"片段 {video_chunk_id} 支持结论。",
            })
        handle_text(approved["message"])


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
    job_repository = ParseJobRepository(session_factory)
    retrieval = _RetrievalStub([])
    agent = AgentChatService(
        AgentRepository(session_factory),
        library,
        AgentRuntime(
            _AgentModelStub("query-model-v1"),
            _AgentModelStub("evaluator-model-v1"),
            _AgentModelStub("chat-model-v1"),
            retrieval.search,
        ),
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
            probe_duration=lambda _path: 12_345,
        ),
        jobs=ParseJobService(job_repository, library),
        media=MediaService(library, artifact_store),
        retrieval=retrieval,  # type: ignore[arg-type]
        agent=agent,
        index_sync=object(),  # type: ignore[arg-type]
        parse_consumer=_ParseConsumerStub(),  # type: ignore[arg-type]
    )
    return TestClient(create_app(services)), retrieval, job_repository


def _create_series(
    client: TestClient,
    *,
    workspace_id: str = "local-default",
    name: str = "Test Series",
    ready: bool = True,
) -> str:
    response = client.post(
        "/api/series/create",
        json={
            "workspaceId": workspace_id,
            "name": name,
            "description": "API fixture",
        },
    )
    assert response.status_code == 201, response.text
    series_id = response.json()["seriesId"]
    if ready:
        with client.app.state.services.library._repository._session_factory.begin() as session:
            session.get(SeriesRecord, series_id).status = "READY"
    return series_id


def test_api_paths_name_create_list_get_and_upload_actions(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    openapi_paths = client.app.openapi()["paths"]
    paths = set(openapi_paths)

    assert {
        "/healthz",
        "/api/series/create",
        "/api/series/list",
        "/api/series/get",
        "/api/series/update",
        "/api/videos/update_episode",
        "/api/series/images/upload",
        "/api/uploads/create",
        "/api/uploads/get",
        "/api/uploads/list",
        "/api/uploads/delete",
        "/api/uploads/retry-parse",
        "/api/uploads/reparse",
        "/api/uploads/segments/upload",
        "/api/uploads/complete",
        "/api/jobs/list",
        "/api/jobs/get",
        "/api/jobs/usage/get",
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
        "/api/agent/messages/list",
        "/api/agent/messages/get",
        "/api/agent/messages/options",
        "/api/agent/messages/send",
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
        ("/api/series/images/upload", "post"): {"seriesId", "workspaceId"},
        ("/api/uploads/create", "post"): {"seriesId"},
        ("/api/uploads/get", "get"): {"uploadSessionId"},
        ("/api/uploads/delete", "post"): {"uploadSessionId", "workspaceId"},
        ("/api/uploads/retry-parse", "post"): {
            "uploadSessionId", "workspaceId", "expectedJobVersion",
        },
        ("/api/uploads/reparse", "post"): {
            "uploadSessionId", "workspaceId", "expectedJobVersion",
        },
        ("/api/uploads/segments/upload", "put"): {
            "uploadSessionId",
            "segmentNumber",
        },
        ("/api/uploads/complete", "post"): {"uploadSessionId"},
        ("/api/jobs/get", "get"): {"jobId"},
        ("/api/jobs/usage/get", "get"): {"jobId", "workspaceId"},
        ("/api/jobs/events", "get"): {"jobId"},
        ("/api/videos/content", "get"): {"videoId"},
        ("/api/agent/conversations/get", "get"): {"conversationId"},
        ("/api/agent/conversations/delete", "post"): {
            "conversationId", "workspaceId",
        },
        ("/api/agent/messages/list", "get"): {"conversationId"},
        ("/api/agent/messages/send", "post"): {"conversationId"},
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
    assert "workspaceId" in schemas["AgentConversationView"]["required"]
    assert "seriesIds" not in schemas["AgentConversationView"]["properties"]
    assert "seriesIds" not in schemas["AgentConversationCreateRequest"]["properties"]
    assert "seriesIds" in schemas["AgentMessageView"]["required"]
    assert "seriesIds" in schemas["AgentMessageSendRequest"]["properties"]
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
        "workspaceId", "limit", "cursor", "seriesId", "query"
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
        "completedDurationMs",
        "completedThroughMs",
        "totalDurationMs",
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
    assert "retrievalTrace" in schemas["AgentSearchEvidenceView"]["required"]
    assert schemas["AgentMessageView"]["properties"]["searchResult"]["anyOf"][0] == {
        "$ref": "#/components/schemas/AgentSearchEvidenceView",
    }
    assert "selectionReason" in schemas["SearchHitView"]["required"]
    assert {
        "speaker",
        "text",
        "startMs",
        "endMs",
    } <= set(schemas["TranscriptCueView"]["required"])
    assert "modelName" in schemas["AgentMessageView"]["required"]


def _background_png() -> bytes:
    image_bytes = BytesIO()
    Image.new("RGB", (32, 16), color="red").save(image_bytes, format="PNG")
    return image_bytes.getvalue()


def test_series_background_upload_save_and_read_back(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    picture = _background_png()
    uploaded = client.post(
        "/api/series/images/upload",
        params={"workspaceId": "local-default", "seriesId": series_id},
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
            "workspaceId": "local-default", "seriesId": series_id,
            "importantExtraData": {"content": content},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["importantExtraData"] == {"format": "markdown", "content": content}
    assert updated.json()["description"] == "API fixture"
    loaded = client.get("/api/series/get", params={"workspaceId": "local-default", "seriesId": series_id})
    assert loaded.json()["importantExtraData"] == updated.json()["importantExtraData"]
    repeat_upload = client.post(
        "/api/series/images/upload",
        params={"workspaceId": "local-default", "seriesId": series_id}, content=picture,
    )
    assert repeat_upload.json()["imageId"] == asset["imageId"]


def test_series_background_json_patch_and_clear(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    request_scope = {"workspaceId": "local-default", "seriesId": series_id}
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
            "workspaceId": "local-default",
            "name": "Japanese Series",
            "transcriptLanguageHint": "ja-jp",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["transcriptLanguageHint"] == "ja-JP"
    scope = {
        "workspaceId": "local-default",
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
        "workspaceId": "local-default", "seriesId": series_id, "description": "must not persist",
        "importantExtraData": document,
    })
    assert result.status_code == expected_status, result.text
    loaded = client.get("/api/series/get", params={"workspaceId": "local-default", "seriesId": series_id})
    assert loaded.json()["description"] == "API fixture"
    assert loaded.json()["importantExtraData"] is None


def test_series_background_image_scope_and_missing_file(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    first_id = _create_series(client, name="First Series")
    other_id = _create_series(client, name="Other Series")
    foreign_id = _create_series(client, workspace_id="other-workspace", name="Foreign Series")
    image = client.post(
        "/api/series/images/upload", content=_background_png(),
        params={"workspaceId": "local-default", "seriesId": first_id},
    ).json()
    for series_id, workspace_id, status in ((other_id, "local-default", 400), (foreign_id, "local-default", 404)):
        result = client.post("/api/series/update", json={
            "workspaceId": workspace_id, "seriesId": series_id,
            "importantExtraData": {"content": f"asset://{image['imageId']}"},
        })
        assert result.status_code == status
    assert client.get(image["url"].replace("workspaceId=local-default", "workspaceId=other-workspace")).status_code == 404
    store = client.app.state.services.media._artifact_store
    store.resolve_series_image(image["imageId"], series_id=first_id).unlink()
    missing = client.post("/api/series/update", json={
        "workspaceId": "local-default", "seriesId": first_id,
        "importantExtraData": {"content": f"asset://{image['imageId']}"},
    })
    assert missing.status_code == 400


def test_series_background_rejects_image_without_model_variant(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    uploaded = client.post(
        "/api/series/images/upload", content=_background_png(),
        params={"workspaceId": "local-default", "seriesId": series_id},
    ).json()
    store = client.app.state.services.media._artifact_store
    store.resolve_series_image(uploaded["imageId"], series_id=series_id, variant="model").unlink()
    assert store.resolve_series_image(uploaded["imageId"], series_id=series_id).is_file()
    result = client.post("/api/series/update", json={
        "workspaceId": "local-default", "seriesId": series_id, "description": "must not save",
        "importantExtraData": {"content": f"asset://{uploaded['imageId']}"},
    })
    assert result.status_code == 400
    loaded = client.get("/api/series/get", params={"workspaceId": "local-default", "seriesId": series_id}).json()
    assert loaded["description"] == "API fixture"
    assert loaded["importantExtraData"] is None


def test_series_background_upload_validates_scope_body_and_limit(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    client.app.state.services.media.image_upload_config = ImageUploadConfig(maximum_upload_size_bytes=128)
    scope = {"workspaceId": "local-default", "seriesId": series_id}
    assert client.post("/api/series/images/upload", params=scope, content=b"").status_code == 400
    assert client.post("/api/series/images/upload", params=scope, content=b"not an image").status_code == 400
    assert client.post("/api/series/images/upload", params=scope, content=b"x" * 129).status_code == 400
    streamed = client.post("/api/series/images/upload", params=scope, content=iter([b"x" * 100, b"x" * 100]))
    assert streamed.status_code == 400
    assert client.post("/api/series/images/upload", params={**scope, "workspaceId": "other"}, content=b"x" * 129).status_code == 404


def test_agent_conversation_send_and_list_messages(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    conversation_response = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={
            "workspaceId": "local-default",
            "title": "寻找哭泣场景",
        },
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation_id = conversation_response.json()["conversationId"]
    assert conversation_response.json()["workspaceId"] == "local-default"
    assert "sessionId" not in conversation_response.json()
    assert "seriesIds" not in conversation_response.json()
    replayed = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={
            "workspaceId": "local-default",
            "title": "寻找哭泣场景",
        },
    )
    assert replayed.status_code == 201
    assert replayed.json() == conversation_response.json()
    conflicting = client.post(
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": "conversation-1"},
        json={"workspaceId": "local-default", "title": "另一个标题"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "CONFLICT"

    sent = client.post(
        "/api/agent/messages/send",
        params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "message-1"},
        json={
            "workspaceId": "local-default",
            "content": "她为什么哭？",
            "seriesIds": [series_id],
            "topK": 5,
        },
    )
    listed = client.get(
        "/api/agent/messages/list",
        params={"workspaceId": "local-default", "conversationId": conversation_id},
    )
    listed_conversations = client.get("/api/agent/conversations/list", params={"workspaceId": "local-default"})
    messages_after_user = client.get(
        "/api/agent/messages/list",
        params={"workspaceId": "local-default", "conversationId": conversation_id, "afterSequenceNo": 0},
    )

    assert sent.status_code == 200, sent.text
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


def test_agent_conversation_delete_is_workspace_scoped_and_removes_messages(
    tmp_path: Path,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    sent = client.post(
        "/api/agent/messages/send",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
        headers={"Idempotency-Key": "message-before-delete"},
        json={"workspaceId": "local-default", "content": "找画面"},
    )
    assert sent.status_code == 200, sent.text

    outside = client.post(
        "/api/agent/conversations/delete",
        params={
            "workspaceId": "another-workspace",
            "conversationId": conversation_id,
        },
    )
    assert outside.status_code == 404

    deleted = client.post(
        "/api/agent/conversations/delete",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["conversationId"] == conversation_id
    assert deleted.json()["messageCount"] == 2
    assert client.get(
        "/api/agent/conversations/get",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 404
    assert client.get(
        "/api/agent/messages/list",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 404
    assert client.post(
        "/api/agent/conversations/delete",
        params={
            "workspaceId": "local-default",
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
        workspace_id="local-default",
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
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_IN_PROGRESS"
    assert client.get(
        "/api/agent/conversations/get",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 200
    assert client.get(
        "/api/agent/messages/list",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 200
    assert client.post(
        "/api/agent/conversations/delete",
        params={
            "workspaceId": "local-default",
            "conversationId": conversation_id,
        },
    ).status_code == 409


def test_agent_conversations_are_created_and_listed_within_workspace(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    local_series_id = _create_series(client)
    other_series_id = _create_series(client, workspace_id="other-workspace")
    conversations = {}
    try:
        for workspace_id, series_id in (
            ("local-default", local_series_id),
            ("other-workspace", other_series_id),
        ):
            response = client.post(
                "/api/agent/conversations/create",
                headers={"Idempotency-Key": "same-key-across-workspaces"},
                json={"workspaceId": workspace_id},
            )
            assert response.status_code == 201, response.text
            conversations[workspace_id] = response.json()
            listed = client.get(
                "/api/agent/conversations/list",
                params={"workspaceId": workspace_id},
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
                    "workspaceId": "other-workspace",
                },
            )
            assert outside.status_code == 404
        outside_send = client.post(
            "/api/agent/messages/send",
            params={"workspaceId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "outside-message"},
            json={"workspaceId": "other-workspace", "content": "找画面"},
        )
        assert outside_send.status_code == 404
        outside_scope = client.post(
            "/api/agent/messages/send",
            params={"workspaceId": "local-default", "conversationId": conversations["other-workspace"]["conversationId"]},
            headers={"Idempotency-Key": "outside-scope"},
            json={"workspaceId": "other-workspace", "content": "找画面", "seriesIds": [local_series_id]},
        )
        assert outside_scope.status_code == 404
        another = client.post(
            "/api/agent/conversations/create",
            headers={"Idempotency-Key": "another-local-conversation"},
            json={"workspaceId": "local-default"},
        )
        assert another.status_code == 201
        assert len(client.get("/api/agent/conversations/list", params={"workspaceId": "local-default"}).json()["items"]) == 2
        limited = client.get("/api/agent/conversations/list", params={"workspaceId": "local-default", "limit": 1})
        assert limited.status_code == 200
        assert len(limited.json()["items"]) == 1
        assert limited.json()["items"][0]["workspaceId"] == "local-default"
        assert retrieval.calls == []
    finally:
        client.app.state.services.agent.close()


def test_series_and_search_contract(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    listed = client.get("/api/series/list", params={"workspaceId": "local-default"}).json()["items"]
    detail = client.get(
        "/api/series/get",
        params={"workspaceId": "local-default", "seriesId": series_id},
    ).json()
    searched = client.post(
        "/api/search",
        json={
            "workspaceId": "local-default",
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
            session.add(VideoRecord(
                video_id=video_id,
                series_id=series_id,
                episode_order=episode_order,
                upload_sequence=episode_order,
                original_filename=f"episode-{episode_order}.mp4",
                original_source_uri=f"artifact://test/{video_id}.mp4",
                inference_proxy_uri=None,
                content_hash=content_hash,
                total_bytes=10,
                duration_ms=1_000,
                upload_status="COMPLETED",
                parse_status="SUCCEEDED",
                upload_progress=100,
                parse_progress=100,
            ))

    updated = client.post(
        "/api/videos/update_episode",
        json={
            "workspaceId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 3,
        },
    )
    conflict = client.post(
        "/api/videos/update_episode",
        json={
            "workspaceId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 2,
        },
    )
    outside_workspace = client.post(
        "/api/videos/update_episode",
        json={
            "workspaceId": "another-workspace",
            "videoId": first_video_id,
            "episodeOrder": 4,
        },
    )
    invalid = client.post(
        "/api/videos/update_episode",
        json={
            "workspaceId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 0,
        },
    )
    too_large = client.post(
        "/api/videos/update_episode",
        json={
            "workspaceId": "local-default",
            "videoId": first_video_id,
            "episodeOrder": 2_147_483_648,
        },
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["episodeOrder"] == 3
    assert updated.json()["uploadSequence"] == 1
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONFLICT"
    assert outside_workspace.status_code == 404
    assert invalid.status_code == 422
    assert too_large.status_code == 422


def test_upload_order_is_assigned_and_legacy_order_fields_are_ignored(tmp_path: Path) -> None:
    client, _retrieval, _jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    payload = {
        "workspaceId": "local-default", "originalFilename": "episode-99.mp4",
        "mediaType": "video/mp4", "totalSizeBytes": 4,
    }
    first = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 99, "uploadSequence": 500},
    )
    second = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "second"}, json=payload,
    )
    replay = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"},
        json={**payload, "episodeOrder": 10, "uploadSequence": 20},
    )
    assert first.status_code == second.status_code == replay.status_code == 201
    assert first.json()["episodeOrder"] == first.json()["uploadSequence"] == 1
    assert second.json()["episodeOrder"] == second.json()["uploadSequence"] == 2
    assert replay.json()["uploadSessionId"] == first.json()["uploadSessionId"]
    changed = client.post(
        "/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "first"}, json={**payload, "originalFilename": "changed.mp4"},
    )
    assert changed.status_code == 409
    schema = client.get("/openapi.json").json()["components"]["schemas"]["UploadSessionCreateRequest"]
    for field in ("episodeOrder", "uploadSequence"):
        assert field not in schema["required"]
        assert schema["properties"][field]["deprecated"] is True
        assert "ignored" in schema["properties"][field]["description"]


def test_upload_delete_is_workspace_scoped_and_idempotent(tmp_path):
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    upload = client.post("/api/uploads/create", params={"seriesId": series_id},
        headers={"Idempotency-Key": "delete-upload"}, json={
            "workspaceId": "local-default", "originalFilename": "episode.mp4",
            "mediaType": "video/mp4", "totalSizeBytes": 4,
        }).json()
    params = {"workspaceId": "local-default", "uploadSessionId": upload["uploadSessionId"]}
    assert client.post("/api/uploads/delete", params={**params, "workspaceId": "other"}).status_code == 404
    deleted = client.post("/api/uploads/delete", params=params)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "EXPIRED"
    assert deleted.headers["X-Request-Id"]
    assert client.post("/api/uploads/delete", params=params).json()["version"] == deleted.json()["version"]
    assert client.get("/api/uploads/list", params={"workspaceId": "local-default"}).json()["total"] == 0
    assert client.post("/api/uploads/complete", params=params).status_code == 409


def test_upload_parse_retry_is_version_fenced_and_returns_the_same_job(tmp_path):
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "manual-retry"},
        json={
            "workspaceId": "local-default",
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
            "workspaceId": "local-default",
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
            "workspaceId": "local-default",
            "uploadSessionId": upload_session_id,
        },
    )
    assert completed.status_code == 200, completed.text
    job_id = completed.json()["jobId"]
    with jobs_repository._session_factory.begin() as session:
        job = session.get(SeriesParseJobRecord, job_id)
        job.status = ParseJobStatus.FAILED.value
        job.stage = ParseJobStage.PARSING.value
        job.progress = 18
        job.attempt_count = 3
        job.max_attempts = 3
        job.version = 7
        job.last_error = "provider timeout"
        video = session.get(VideoRecord, job.video_id)
        video.parse_status = ParseJobStatus.FAILED.value
        video.parse_progress = 18
        session.get(SeriesRecord, series_id).status = "FAILED"

    failed = client.get(
        "/api/jobs/get",
        params={"workspaceId": "local-default", "jobId": job_id},
    )
    assert failed.status_code == 200
    assert failed.json()["canRetry"] is True
    retry_params = {
        "workspaceId": "local-default",
        "uploadSessionId": upload_session_id,
        "expectedJobVersion": 7,
    }
    assert client.post(
        "/api/uploads/retry-parse",
        params={**retry_params, "workspaceId": "other-workspace"},
    ).status_code == 404
    retried = client.post("/api/uploads/retry-parse", params=retry_params)
    assert retried.status_code == 200, retried.text
    assert {
        "jobId": job_id,
        "uploadSessionId": upload_session_id,
        "status": "QUEUED",
        "stage": "PARSING",
        "progress": 18,
        "attemptCount": 3,
        "maxAttempts": 4,
        "canRetry": False,
        "version": 8,
    }.items() <= retried.json().items()
    duplicate = client.post("/api/uploads/retry-parse", params=retry_params)
    assert duplicate.status_code == 409
    assert duplicate.json()["requestId"] == duplicate.headers["X-Request-Id"]
    with jobs_repository._session_factory() as session:
        job = session.get(SeriesParseJobRecord, job_id)
        assert (job.status, job.attempt_count, job.max_attempts, job.version) == (
            ParseJobStatus.QUEUED.value, 3, 4, 8,
        )


def test_upload_full_reparse_is_distinct_version_fenced_and_starts_at_zero(tmp_path):
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "full-reparse"},
        json={
            "workspaceId": "local-default",
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4",
            "totalSizeBytes": 4,
        },
    )
    upload_session_id = created.json()["uploadSessionId"]
    assert client.put(
        "/api/uploads/segments/upload",
        params={
            "workspaceId": "local-default",
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
            "workspaceId": "local-default",
            "uploadSessionId": upload_session_id,
        },
    ).json()
    job_id = completed["jobId"]
    with jobs_repository._session_factory.begin() as session:
        job = session.get(SeriesParseJobRecord, job_id)
        job.status = ParseJobStatus.SUCCEEDED.value
        job.stage = ParseJobStage.COMPLETED.value
        job.progress = 100
        job.attempt_count = 1
        job.version = 6
        job.completed_at = 1_789_639_200_000
        video = session.get(VideoRecord, job.video_id)
        video.parse_status = ParseJobStatus.SUCCEEDED.value
        video.parse_progress = 100
        session.get(SeriesRecord, series_id).status = "READY"
        session.add(VideoChunkCheckpointRecord(
            job_id=job_id,
            sequence_no=0,
            video_chunk_id="reparse-checkpoint",
            stage=VideoChunkCheckpointStage.INDEXED.value,
            chunk_start_ms=0,
            chunk_end_ms=12_345,
        ))

    before = client.get(
        "/api/jobs/get",
        params={"workspaceId": "local-default", "jobId": job_id},
    ).json()
    assert before["canRetry"] is False
    assert before["canReparse"] is True
    params = {
        "workspaceId": "local-default",
        "uploadSessionId": upload_session_id,
        "expectedJobVersion": 6,
    }
    assert client.post(
        "/api/uploads/reparse",
        params={**params, "workspaceId": "other-workspace"},
    ).status_code == 404
    reparsed = client.post("/api/uploads/reparse", params=params)
    assert reparsed.status_code == 200, reparsed.text
    assert {
        "jobId": job_id,
        "uploadSessionId": upload_session_id,
        "status": "QUEUED",
        "stage": "QUEUED",
        "progress": 0,
        "attemptCount": 1,
        "canRetry": False,
        "canReparse": False,
        "version": 7,
    }.items() <= reparsed.json().items()
    assert reparsed.json()["checkpoints"] == []
    duplicate = client.post("/api/uploads/reparse", params=params)
    assert duplicate.status_code == 409


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
                "workspaceId": "local-default", "originalFilename": "private-filename.mp4",
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
        "workspaceId": "local-default",
        "episodeOrder": 7,
        "originalFilename": "episode-07.mp4",
        "mediaType": "video/mp4",
        "totalSizeBytes": 10,
    }

    first = client.post(
        "/api/uploads/create",
        params={"workspaceId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    repeated = client.post(
        "/api/uploads/create",
        params={"workspaceId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    assert first.status_code == 201, first.text
    assert repeated.status_code == 201
    upload_id = first.json()["uploadSessionId"]
    assert repeated.json()["uploadSessionId"] == upload_id
    assert first.json()["missingSegmentNumbers"] == [0, 1, 2]
    assert first.json()["episodeOrder"] == first.json()["uploadSequence"] == 1

    parts = [(2, 8, b"ij"), (0, 0, b"abcd"), (1, 4, b"efgh")]
    for number, start, data in parts:
        end = start + len(data) - 1
        response = client.put(
            "/api/uploads/segments/upload",
            params={"workspaceId": "local-default",
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
        params={"workspaceId": "local-default", "uploadSessionId": upload_id},
    )
    assert current.json()["uploadedSizeBytes"] == 10
    assert current.json()["missingSegmentNumbers"] == []

    completed = client.post(
        "/api/uploads/complete",
        params={"workspaceId": "local-default", "uploadSessionId": upload_id},
    )
    assert completed.status_code == 200, completed.text
    video_id = completed.json()["videoId"]
    job_id = completed.json()["jobId"]
    job = client.get("/api/jobs/get", params={"workspaceId": "local-default", "jobId": job_id})
    jobs = client.get("/api/jobs/list", params={"workspaceId": "local-default"})
    assert job.status_code == 200
    assert job.json()["status"] == "QUEUED"
    assert job.json()["uploadSessionId"] == upload_id
    assert job.json()["progress"] == 0
    assert job.json()["overallProgress"] is None
    assert job.json()["stageProgress"] is None
    assert job.json()["completedDurationMs"] is None
    assert job.json()["completedThroughMs"] is None
    assert job.json()["videoDurationMs"] == 12_345
    assert job.json()["totalDurationMs"] == 12_345
    assert job.json()["processingDurationMs"] == 0
    assert job.json()["processingTimingComplete"] is False
    assert job.json()["completedChunkCount"] is None
    assert job.json()["totalChunkCount"] == 0
    assert job.json()["currentChunk"] is None
    assert job.json()["seriesName"] == "Test Series"
    assert job.json()["episodeOrder"] == 1
    assert job.json()["originalFilename"] == "episode-07.mp4"
    assert [item["jobId"] for item in jobs.json()["items"]] == [job_id]
    for path in ("/api/jobs/get", "/api/jobs/events"):
        outside_workspace = client.get(
            path,
            params={"jobId": job_id, "workspaceId": "another-workspace"},
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
        job_service.get(job_id, workspace_id="local-default")
    )
    assert queue.heartbeat(
        lease,
        lease_duration=timedelta(minutes=5),
    )
    after_heartbeat = _job_view(
        job_service.get(job_id, workspace_id="local-default")
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
        job_service.get(job_id, workspace_id="local-default")
    )
    assert _job_event_fingerprint(after_progress) != _job_event_fingerprint(
        after_heartbeat
    )
    assert queue.acknowledge(lease, duration_ms=0)
    with client.stream(
        "GET",
        "/api/jobs/events",
        params={"workspaceId": "local-default", "jobId": job_id},
    ) as events:
        body = "\n".join(events.iter_lines())
    assert "event: job" in body
    assert '"status":"SUCCEEDED"' in body

    full_video = client.get(
        "/api/videos/content",
        params={"workspaceId": "local-default", "videoId": video_id, "variant": "original"},
    )
    ranged_video = client.get(
        "/api/videos/content",
        params={"workspaceId": "local-default", "videoId": video_id, "variant": "original"},
        headers={"Range": "bytes=2-5"},
    )
    assert full_video.content == b"abcdefghij"
    assert ranged_video.status_code == 206
    assert ranged_video.content == b"cdef"

    delete_params = {
        "workspaceId": "local-default",
        "uploadSessionId": upload_id,
    }
    deleted = client.post("/api/uploads/delete", params=delete_params)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "EXPIRED"
    assert deleted.json()["videoId"] is None
    assert deleted.json()["jobId"] is None
    assert client.get(
        "/api/jobs/get",
        params={"workspaceId": "local-default", "jobId": job_id},
    ).status_code == 404
    assert client.get(
        "/api/videos/content",
        params={"workspaceId": "local-default", "videoId": video_id},
    ).status_code == 404
    repeated_delete = client.post("/api/uploads/delete", params=delete_params)
    assert repeated_delete.status_code == 200
    assert repeated_delete.json()["version"] == deleted.json()["version"]


def test_upload_rejects_wrong_content_range_and_workspace(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"workspaceId": "local-default", "seriesId": series_id},
        headers={"Idempotency-Key": "bad-range"},
        json={"workspaceId": "local-default",
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
            "workspaceId": "another-workspace",
        },
    )
    wrong_range = client.put(
        "/api/uploads/segments/upload",
        params={"workspaceId": "local-default",
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
        params={"workspaceId": "local-default", "uploadSessionId": upload_id, "segmentNumber": 0},
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
                "workspaceId": "another-workspace",
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
        params={"workspaceId": "local-default",
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
        json={"workspaceId": "local-default"},
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
                params={"workspaceId": "local-default", "conversationId": conversation_id},
                headers={"Idempotency-Key": f"scoped-message-{index}"},
                json={"workspaceId": "local-default", "content": "找哭泣的画面", "seriesIds": scope, "topK": 5},
            )
            assert response.status_code == 200, response.text
            pair = response.json()
            assert set(pair["userMessage"]["seriesIds"]) == set(scope)
            assert set(pair["assistantMessage"]["seriesIds"]) == set(scope)
            assert set(pair["assistantMessage"]["searchResult"]["seriesIds"]) == set(scope)
        assert [set(call[1]) for call in retrieval.calls] == [set(scope) for scope in scopes]
        messages = client.get(
            "/api/agent/messages/list", params={"workspaceId": "local-default", "conversationId": conversation_id},
        ).json()["items"]
        assert [set(message["seriesIds"]) for message in messages] == [
            set(scopes[0]), set(scopes[0]), set(scopes[1]), set(scopes[1]),
        ]
        conversation = client.get(
            "/api/agent/conversations/get", params={"workspaceId": "local-default", "conversationId": conversation_id},
        ).json()
        assert conversation["messageCount"] == 4
        assert "seriesIds" not in conversation
    finally:
        client.app.state.services.agent.close()


@pytest.mark.parametrize("selection", [None, []])
def test_empty_selection_resolves_all_workspace_series_once_for_idempotent_replay(tmp_path, selection):
    client, retrieval, _ = _client(tmp_path)
    scope = {_create_series(client), _create_series(client, name="Second Series")}
    _create_series(client, workspace_id="other-workspace")
    _create_series(client, name="Not Ready Series", ready=False)
    conversation_id = _create_agent_conversation(client)
    body = {"workspaceId": "local-default", "content": "找画面", "topK": 5}
    if selection is not None:
        body["seriesIds"] = selection
    kwargs = {
        "params": {"workspaceId": "local-default", "conversationId": conversation_id},
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
        "params": {"workspaceId": "local-default", "conversationId": conversation_id},
        "headers": {"Idempotency-Key": "selected-series-message"},
    }
    try:
        sent = client.post(
            "/api/agent/messages/send", **kwargs,
            json={"workspaceId": "local-default", "content": "找画面", "seriesIds": [first_series]},
        )
        assert sent.status_code == 200, sent.text
        conflicting = client.post(
            "/api/agent/messages/send", **kwargs,
            json={"workspaceId": "local-default", "content": "找画面", "seriesIds": [second_series]},
        )
        assert conflicting.status_code == 409
        assert conflicting.json()["code"] == "CONFLICT"
        assert len(retrieval.calls) == 1
        assert len(client.get(
            "/api/agent/messages/list", params={"workspaceId": "local-default", "conversationId": conversation_id},
        ).json()["items"]) == 2
    finally:
        client.app.state.services.agent.close()


def test_explicit_non_ready_series_is_rejected_before_starting_message(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    series_id = _create_series(client, ready=False)
    conversation_id = _create_agent_conversation(client)
    try:
        response = client.post(
            "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "non-ready-message"},
            json={"workspaceId": "local-default", "content": "找画面", "seriesIds": [series_id]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "SERIES_NOT_READY"
        assert retrieval.calls == []
        assert client.get(
            "/api/agent/messages/list", params={"workspaceId": "local-default", "conversationId": conversation_id},
        ).json()["items"] == []
    finally:
        client.app.state.services.agent.close()


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_joined_chunk_evidence_in_json_and_sse_always_contains_series_id(
    tmp_path, prepared_import, caplog, accept,
):
    caplog.set_level(logging.INFO, logger="server.api.agent")
    client, _, _ = _client(tmp_path)
    service = client.app.state.services.agent
    video_repository = VideoRepository(service._repository._session_factory)
    report = video_repository.import_video_parse(prepared_import)
    with service._repository._session_factory.begin() as session:
        session.get(SeriesRecord, prepared_import.series_id).status = "READY"
    conversation_id = _create_agent_conversation(client)

    def joined_search(
        query,
        *,
        workspace_id,
        series_ids,
        top_k,
        transcript_weight,
        text_weight,
        vision_weight,
    ):
        assert workspace_id == prepared_import.workspace_id
        assert series_ids == [prepared_import.series_id]
        assert transcript_weight + text_weight + vision_weight == pytest.approx(1.0)
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
                    "transcript": 0.2,
                    "semantic": 0.2,
                    "visual": 0.6,
                },
            }))
            for item in result["items"]:
                cursor = None
                while True:
                    page = json.loads(tools_by_name["read_trieve_item"].invoke({
                        "video_chunk_id": item["video_chunk_id"],
                        "cursor": cursor,
                    }))
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                tools_by_name["update_draft_tem"].invoke({
                    "video_chunk_id": item["video_chunk_id"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "claim": "画面中有对应动作。",
                })
            return result_type.model_validate({
                "plan_context": "召回动作并检查片段来源。",
                "message": "这些片段包含动作。",
            })

    service._runtime._retrieve = joined_search
    service._runtime._query_model = QueryWithEvidence("query-evidence")
    try:
        response = client.post(
            "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
            headers={"Accept": accept, "Idempotency-Key": "joined-evidence"},
            json={"workspaceId": "local-default", "content": "找动作片段", "seriesIds": [prepared_import.series_id]},
        )
        assert response.status_code == 200, response.text
        if accept == "text/event-stream":
            events = _answer_payloads(response.text)
            evidence_event = next(event for event in events if event["type"] == "answer.evidence")
            evidence_hits = evidence_event["searchResult"]["hits"]
            assert "agentReply" not in evidence_event["searchResult"]
            final = events[-1]["assistantMessage"]
            assert evidence_hits == final["searchResult"]["hits"]
            block_events = [
                event for event in events if event["type"] == "answer.block"
            ]
            assert [event["block"] for event in block_events] == final["renderBlocks"]
            event_types = [event["type"] for event in events]
            assert event_types.index("answer.evidence") < event_types.index("answer.block")
            assert max(
                index for index, value in enumerate(event_types)
                if value == "answer.block"
            ) < event_types.index("answer.delta")
            projection_log = next(
                record.getMessage() for record in caplog.records
                if record.name == "server.api.agent"
                and record.getMessage().startswith("agent_evidence_projected ")
            )
            assert f"conversation_id={conversation_id}" in projection_log
            assert "hit_count=2" in projection_log
            assert "playable_hits=0" in projection_log
            assert "thumbnail_hits=0" in projection_log
            assert "partial=True" in projection_log
            assert "warning_count=5" in projection_log
        else:
            final = response.json()["assistantMessage"]
        assert "agentReply" not in final["searchResult"]
        assert final["searchResult"]["retrievalTrace"] == [{
            "query": "找动作片段",
            "topK": min(5, service.default_top_k),
            "strategyVersion": "query-adaptive-three-channel-rrf-v2",
            "requestedWeights": {"transcript": 0.2, "semantic": 0.2, "visual": 0.6},
            "effectiveWeights": {"transcript": 0.2, "semantic": 0.2, "visual": 0.6},
            "resultCount": len(report.video_chunk_ids),
            "videoChunkIds": list(report.video_chunk_ids),
        }]
        if accept == "text/event-stream":
            assert evidence_event["searchResult"]["retrievalTrace"] == (
                final["searchResult"]["retrievalTrace"]
            )
        hits = final["searchResult"]["hits"]
        assert len(hits) == len(report.video_chunk_ids) == 2
        assert final["renderBlocks"] == [
            {"type": "markdown", "markdown": "以下片段支持这个结论。"},
            *[
                {
                    "type": "video",
                    "videoChunkId": hit["candidate"]["videoChunkId"],
                    "startMs": hit["evidence"]["startMs"],
                    "endMs": hit["evidence"]["endMs"],
                }
                for hit in hits
            ],
            {"type": "markdown", "markdown": "这些片段包含动作。"},
        ]
        assert final["seriesIds"] == [prepared_import.series_id]
        for hit in hits:
            evidence = hit["evidence"]
            assert evidence["seriesId"] == prepared_import.series_id
            assert evidence["workspaceId"] == prepared_import.workspace_id
            assert evidence["seriesName"] == prepared_import.series_name
            assert evidence["videoId"] == prepared_import.video_id
            assert evidence["videoChunkId"] == hit["candidate"]["videoChunkId"]
            assert 0 <= evidence["startMs"] < evidence["endMs"] <= evidence["durationMs"]
    finally:
        service.close()


def test_same_send_path_supports_live_events_and_idempotent_replay(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    series_id = _create_series(client)
    conversation_id = _create_agent_conversation(client)
    kwargs = {
        "params": {"workspaceId": "local-default", "conversationId": conversation_id},
        "headers": {"Accept": "text/event-stream", "Idempotency-Key": "stream-message"},
        "json": {"workspaceId": "local-default", "content": "找哭泣的画面", "topK": 5},
    }
    response = client.post("/api/agent/messages/send", **kwargs)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _answer_payloads(response.text)
    assert [event["type"] for event in events] == [
        "answer.started", "answer.progress", "answer.progress", "answer.progress",
        "answer.evidence", "answer.block", "answer.delta", "answer.completed",
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["conversationId"] == conversation_id for event in events)
    assert len({event["messageId"] for event in events}) == 1
    assert events[0]["assistantMessage"]["status"] == "PROCESSING"
    assert events[0]["assistantMessage"]["content"] is None
    assert events[0]["assistantMessage"]["timing"]["totalDurationMs"] is None
    assert events[0]["assistantMessage"]["timing"]["stages"] == []
    assert type(events[0]["assistantMessage"]["timing"]["startedAt"]) is int
    progress = [event for event in events if event["type"] == "answer.progress"]
    assert [
        [stage["phase"] for stage in event["timing"]["stages"]]
        for event in progress
    ] == [
        ["QUEUED"],
        ["QUEUED", "RETRIEVING"],
        ["QUEUED", "RETRIEVING", "EVALUATING"],
    ]
    assert events[0]["userMessage"]["modelName"] is None
    assert events[0]["userMessage"]["seriesIds"] == [series_id]
    assert events[0]["assistantMessage"]["seriesIds"] == [series_id]
    final = events[-1]["assistantMessage"]
    assert final["status"] == "COMPLETED"
    assert final["timing"]["totalDurationMs"] == sum(
        stage["durationMs"] for stage in final["timing"]["stages"]
    )
    assert [stage["phase"] for stage in final["timing"]["stages"]] == [
        "QUEUED", "RETRIEVING", "EVALUATING", "RESPONDING",
    ]
    assert final["content"] == "".join(event["delta"] for event in events if event["type"] == "answer.delta")
    assert final["renderBlocks"] == [
        event["block"] for event in events if event["type"] == "answer.block"
    ]
    persisted = client.get("/api/agent/messages/list", params={"workspaceId": "local-default", "conversationId": conversation_id}).json()["items"]
    assert final == persisted[-1]
    replay = _answer_payloads(client.post("/api/agent/messages/send", **kwargs).text)
    assert [event["type"] for event in replay] == ["answer.started", "answer.completed"]
    assert replay[-1]["messageId"] == final["messageId"]
    assert replay[-1]["assistantMessage"]["timing"] == final["timing"]
    assert len(retrieval.calls) == 1
    content = client.get("/openapi.json").json()["paths"]["/api/agent/messages/send"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"}.issubset(content)
    client.app.state.services.agent.close()


def test_rejected_loop_sends_failed_event_and_json_maps_harness_error(tmp_path):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    runtime = client.app.state.services.agent._runtime

    class RejectModel:
        model_name = "reject-model"
        def run_structured(self, _prompt, *, result_type, **_kwargs):
            return result_type.model_validate({"action": "continue", "advice": "需要重新找摔杯子的片段。"})

    runtime._evaluator_model = RejectModel()
    def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("rejected draft reached final Chat")
    runtime._chat_model.run_agent_loop = forbidden_chat
    response = client.post(
        "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "rejected-stream"},
        json={"workspaceId": "local-default", "content": "找摔杯子的画面"},
    )
    events = _answer_payloads(response.text)
    assert [event["type"] for event in events] == ["answer.started", *(["answer.progress"] * 6), "answer.failed"]
    assert events[-1]["error"]["code"] == "AGENT_MODEL_ERROR"
    assert events[-1]["assistantMessage"]["status"] == "FAILED"
    assert "需要重新找摔杯子的片段" not in events[-1]["error"]["message"]
    json_response = client.post(
        "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "rejected-json"}, json={"workspaceId": "local-default", "content": "重试"},
    )
    assert json_response.status_code == 502
    assert json_response.json()["code"] == "AGENT_MODEL_ERROR"
    outside = client.post(
        "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "outside"},
        json={"content": "隔离", "workspaceId": "other-workspace"},
    )
    assert outside.status_code == 404
    client.app.state.services.agent.close()


def test_agent_context_budget_error_is_actionable_before_provider_call(tmp_path):
    client, _, _ = _client(tmp_path)
    _create_series(client)
    conversation_id = _create_agent_conversation(client)
    runtime = client.app.state.services.agent._runtime
    runtime._max_prompt_chars = 100

    response = client.post(
        "/api/agent/messages/send",
        params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Idempotency-Key": "context-budget"},
        json={"workspaceId": "local-default", "content": "找摔杯子的画面"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "AGENT_CONTEXT_BUDGET_EXCEEDED"
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
    original_call = service._runtime._chat_model.run_agent_loop

    def blocked_provider(*args, **kwargs):
        provider_entered.set()
        assert release.wait(5), "test did not release Provider"
        return original_call(*args, **kwargs)
    service._runtime._chat_model.run_agent_loop = blocked_provider
    body = json.dumps({"workspaceId": "local-default", "content": "找画面"}).encode()

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
            assert service.list_all_messages(conversation_id, workspace_id="local-default")[-1].status == "PROCESSING"
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=3)
        events = _answer_payloads(b"".join(message.get("body", b"") for message in output if message["type"] == "http.response.body").decode())
        deltas = [event["delta"] for event in events if event["type"] == "answer.delta"]
        assert len(deltas) == 1
        assert events[-1]["assistantMessage"]["content"] == deltas[0]

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
        "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
        headers={"Accept": accept, "Idempotency-Key": "accept-message"},
        json={"workspaceId": "local-default", "content": "找画面"},
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
    service._runtime._retrieve = failed_search
    try:
        response = client.post(
            "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
            headers={"Idempotency-Key": "retrieval-error"}, json={"workspaceId": "local-default", "content": "找画面"},
        )
        assert response.status_code == expected_status
        assert response.json()["code"] == expected_code
        failed = service.list_all_messages(conversation_id, workspace_id="local-default")[-1]
        assert failed.status == "FAILED" and failed.last_error is not None
        assert "retrieval source unavailable" not in failed.last_error.message
        response = client.post(
            "/api/agent/messages/send", params={"workspaceId": "local-default", "conversationId": conversation_id},
            headers={"Accept": "text/event-stream", "Idempotency-Key": "retrieval-stream-error"},
            json={"workspaceId": "local-default", "content": "再找画面"},
        )
        events = _answer_payloads(response.text)
        assert [event["type"] for event in events] == ["answer.started", "answer.progress", "answer.failed"]
        assert events[-1]["error"]["code"] == "PROCESSING_FAILED"
        assert events[-1]["assistantMessage"]["status"] == "FAILED"
    finally:
        service.close()


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
        assert error["fieldErrors"][0]["field"] == "workspaceId"
        assert error["retryAt"] is None
        created = client.post("/api/agent/conversations/create",
            headers={"Idempotency-Key": "ui-timestamp"}, json={"workspaceId": "local-default"})
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
                    "workspaceId": "local-default", "episodeOrder": episode,
                    "originalFilename": f"episode-{episode}.mp4", "mediaType": "video/mp4", "totalSizeBytes": 4,
                })
            assert response.status_code == 201, response.text
        params = {"workspaceId": "local-default", "limit": 1}
        page = client.get("/api/uploads/list", params=params).json()
        assert page["total"] == 2 and page["hasMore"]
        assert len(page["items"]) == 1
        last = client.get("/api/uploads/list", params={**params, "cursor": page["nextCursor"]}).json()
        assert last["total"] == 2 and not last["hasMore"] and last["nextCursor"] is None
        assert page["items"][0]["uploadSessionId"] != last["items"][0]["uploadSessionId"]
        mismatch = client.get("/api/uploads/list", params={**params, "workspaceId": "other", "cursor": page["nextCursor"]})
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
        conversation_id = _create_agent_conversation(client)
        rejected = client.post("/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "too-many-results"},
            json={"workspaceId": "local-default", "content": "找画面", "topK": 8})
        assert rejected.status_code == 400
        rejected = client.post("/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "too-many-model-calls"},
            json={"workspaceId": "local-default", "content": "找画面", "maxModelCallsPerTurn": 11})
        assert rejected.status_code == 400
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
    finally:
        service.close()
