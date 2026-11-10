"""Cloud guest routes through SQL scope resolution, recall, hydration and Agent tools."""

import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from elastic_transport import ConnectionError as ElasticConnectionError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from engine.rag import EncoderContract
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.retrieval.contracts import RawChunkHit
from server.config.parse_profile import default_video_parse_v2_config
from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask, VideoParseTask
from server.model.video import Video
from server.service.media import MediaService
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY, VideoParsePersistenceService
from server.service.video_retrieval import VideoParseRetrievalService
from tests.server.test_agent_chat_service import _DirectSupervisorModel, _QueryModel, _WebSearcherStub
from tests.server.test_cloud_access import _conversation, _guest, cloud_api
from tests.server.test_video_retrieval_service import FakeQueryEmbedder
from tests.server.test_video_event_retrieval import _fixture as event_search_fixture


class CourseIndex:
    """Replace only ES. Return one real, SQL-hydratable Chunk per permitted task."""

    def __init__(self, episodes, dimensions):
        self.episodes = {item.video_id: item for item in episodes}
        self.semantic_dimensions = self.vision_dimensions = dimensions
        self.requests = []
        self.offline = self.empty = False

    def recall(self, request, *, exact=False):
        self.requests.append(request)
        if self.offline:
            raise ElasticConnectionError("private upstream detail")
        if self.empty:
            return ()
        hits = []
        for rank, task in enumerate(request.scope.tasks, 1):
            episode = self.episodes[task.video_id]
            assert task.parse_task_id == episode.video_parse_task_id
            hits.append(RawChunkHit(
                channel=request.channel, video_chunk_id=episode.video_chunk_ids[0], rank=rank, score=1 / rank,
                start_seconds=0, end_seconds=10_000, retrieval_identity=VideoChunkRetrievalIdentity(
                    tenant_id=request.scope.tenant_id, series_id=task.series_id, video_id=task.video_id,
                    parse_task_id=task.parse_task_id, content_version=1, index_generation=request.index_generation,
                ),
            ))
        return tuple(hits)


class CourseModel(_DirectSupervisorModel):
    """Deterministic tool choices; the Supervisor, tools and message lifecycle are real."""

    def run_agent(self, prompt, *, user_message, tools, handle_text, history, **kwargs):
        self.turns.append((user_message, history))
        if user_message not in {"搜整门课", "只查 E08", "查 E08 的事件", "委派查 E08"}:
            handle_text("可以继续讨论。" if history else "你好，我们可以聊这门课。")
            return
        by_name = {tool.name: tool for tool in tools}
        arguments = {"query": "课程", "top_k": 8, "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0}}
        if user_message == "委派查 E08":
            result = json.loads(by_name["query_video"].invoke({"query": "课程", "episode": 8}))
            handle_text(result["reports"][0]["error"]["message"])
            return
        if user_message != "搜整门课":
            arguments["episode"] = 8
        tool = "search_video_events" if user_message == "查 E08 的事件" else "retrieve"
        result = json.loads(by_name[tool].invoke(arguments))
        if "error" in result:
            handle_text(result["error"]["message"])
            return
        if result["items"]:
            hit = result["items"][0]
            by_name["submit_video_item"].invoke({
                "video_chunk_id": hit["video_chunk_id"], "start_seconds": 1, "end_seconds": 3, "claim": "课程示例",
            })
        handle_text("已检索课程。")


@pytest.fixture
def public_course(cloud_api, seed_video):
    api = cloud_api
    tenant = api.services.auth.config.public_tenant_id
    series = api.services.library.create_series(tenant_id=tenant, name="隔离公开课程")
    # Exercise first publication before the first upload.
    api.services.library.update_series(series.series_id, tenant_id=tenant, changes={"visibility": "public"})
    source = api.store.base_directory / "lecture.mp4"
    source.write_bytes(b"playable-course-fixture")
    episodes = [seed_video(api.sessions, tenant_id=tenant, series_id=series.series_id, episode_order=i, content_hash=f"{i:064x}")
                for i in range(1, 9)]
    with api.sessions.begin() as session:
        for episode in episodes:
            session.get(Video, episode.video_id).source_uri = api.store.uri_for_path(source)
        session.get(VideoParseTask, episodes[-1].video_parse_task_id).status = "INDEXING"
        remaining = session.scalars(select(ChunkParseTask).where(ChunkParseTask.chunk_id == episodes[-1].video_chunk_ids[-1])).one()
        remaining.status, remaining.indexed_content_version = "QUEUED", None
        content = session.scalars(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == remaining.id)).one()
        content.detail = {key: value for key, value in content.detail.items() if key != INDEX_EMBEDDINGS_DETAIL_KEY}
    embedding = default_video_parse_v2_config().models.embedding
    index = CourseIndex(episodes, embedding.dimensions)
    model_name = embedding.encoder_model or embedding.model
    embedder = FakeQueryEmbedder([1.0] + [0.0] * (embedding.dimensions - 1), model=model_name)
    search = api.services.retrieval = VideoParseRetrievalService(
        VideoParsePersistenceService(api.sessions), index, embedder,
        semantic_encoder=EncoderContract(provider="test", model=model_name, encoder_revision="test", dimensions=embedding.dimensions),
        expected_index_generation=1, expected_embedding_input_mode=embedding.input_mode,
        expected_query_instruction=embedding.query_instruction, lexical_weight=0, text_weight=1, vision_weight=0,
    )
    model = CourseModel()
    events, _, _, _, _ = event_search_fixture()
    events._persistence = VideoParsePersistenceService(api.sessions)
    api.services.agent._runtime = SupervisorRuntime(
        AgentRuntime(_QueryModel(), search.search, event_retrieve=events.search, retrieval_channel="lexical"), model, _WebSearcherStub(),
        max_web_results=1, web_timeout_seconds=5, execution_timeout_seconds=30,
        max_tool_result_bytes=16_384, max_prompt_chars=200_000,
    )
    api.services.agent._runtimes.append(api.services.agent._runtime)
    return SimpleNamespace(api=api, series_id=series.series_id, tenant=tenant, episodes=episodes,
                           index=index, embedder=embedder, model=model, source=source)


@pytest.mark.parametrize("searchable", [7, 0])
def test_public_watch_chat_and_scoped_retrieval(public_course, searchable, monkeypatch):
    course = public_course
    api, episodes, index = course.api, course.episodes, course.index
    if searchable == 0:
        with api.sessions.begin() as session:
            session.query(VideoParseTask).update({VideoParseTask.status: "INDEXING"})
    params = {"tenantId": course.tenant, "seriesId": course.series_id}
    scope = {"tenantId": course.tenant, "seriesIds": [course.series_id]}
    with TestClient(api.app) as client, TestClient(api.app) as other:
        guest = _guest(client)
        _guest(other)
        headers = {"X-CSRF-Token": guest["csrfToken"]}
        directory = client.get("/api/series/public").json()
        assert [item["seriesId"] for item in directory["items"]] == [course.series_id]
        details = client.get("/api/series/get", params=params).json()
        assert all(video["playbackStatus"] == "READY" and video["originalFilename"] is None for video in details["videos"])
        assert len(details["videos"]) == 8 and details["readyVideoCount"] == searchable
        media = details["videos"][7]["playbackUrl"]
        assert client.get(media, headers={"Range": "bytes=1-3"}).status_code == 206

        def search(**filters):
            return client.post("/api/search", json={**scope, "query": "课程", "topK": 8, **filters}, headers=headers)

        if searchable:
            result = search().json()
            expected = {item.video_id for item in episodes[:7]}
            assert set(result["searchedVideoIds"]) == {hit["evidence"]["videoId"] for hit in result["hits"]} == expected
            assert result["partial"] and result["warnings"] == ["本次检索覆盖 E01–E07，E08 正在处理"]
            assert {t.video_id for t in index.requests[-1].scope.tasks} == expected
            single = search(episodeOrder=7).json()
            assert single["searchedVideoIds"] == [episodes[6].video_id] and not single["partial"]
            index.empty = True
            empty = search(episodeOrder=7)
            assert empty.status_code == 200 and empty.json()["hits"] == []
            index.empty, index.offline = False, True
            offline = search()
            assert offline.status_code == 503 and offline.json()["code"] == "RETRIEVAL_UNAVAILABLE"
            assert "private upstream" not in offline.text
            index.offline = False
            def offline_encoder(_text):
                raise httpx.ConnectError("offline embedding endpoint")
            with monkeypatch.context() as patch:
                patch.setattr(course.embedder, "embed_query", offline_encoder)
                assert search().json()["code"] == "RETRIEVAL_UNAVAILABLE"
        else:
            assert search().json()["code"] == "SERIES_NOT_READY"
        calls_before = len(index.requests)
        embeddings_before = len(course.embedder.calls)
        target = search(episodeOrder=8)
        assert target.status_code == 409 and target.json()["message"] == "该集正在解析，暂时无法检索"
        assert len(index.requests) == calls_before
        assert len(course.embedder.calls) == embeddings_before

        conversation = _conversation(client, guest)
        chat = {"tenantId": course.tenant, "conversationId": conversation}
        playback = {"seriesId": course.series_id, "videoId": episodes[-1].video_id, "episodeOrder": 8, "positionSeconds": 6_000}

        def send(question):
            return client.post("/api/agent/messages/send", params=chat,
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={**scope, "content": question, "extraContext": {"playback": playback}})

        for question in ("你好", "继续解释", "搜整门课", "只查 E08", "查 E08 的事件", "委派查 E08", "继续讨论"):
            response = send(question)
            assert response.status_code == 200, response.text
            pair = response.json()
            history = api.services.agent.list_all_messages(conversation, tenant_id=course.tenant, owner_user_id=guest["userId"])
            assert history[-2].extra_context["playback"] == playback
            answer = pair["assistantMessage"]
            assert answer["status"] == "COMPLETED" and answer["seriesIds"] == [course.series_id]
            if question == "搜整门课" and searchable:
                assert answer["searchResult"]["warnings"] == ["本次检索覆盖 E01–E07，E08 正在处理"]
                assert answer["searchResult"]["hits"] and all(
                    hit["evidence"]["episodeOrder"] <= 7 for hit in answer["searchResult"]["hits"]
                )
            if "E08" in question:
                assert answer["content"] == "该集正在解析，暂时无法检索" and not answer["searchResult"]
        if searchable:
            index.offline = True
            failed_tool = send("搜整门课").json()["assistantMessage"]
            assert failed_tool["status"] == "COMPLETED" and failed_tool["content"] == "检索服务暂不可用，请稍后重试"
            assert send("继续讨论").status_code == 200
        assert course.model.turns[-1][1]
        assert other.get("/api/agent/messages/list", params=chat).status_code == 404
        with api.sessions.begin() as session:
            missing = session.get(Video, episodes[0].video_id)
            removed = api.store.base_directory / "missing.mp4"
            removed.write_bytes(b"removed original")
            missing.source_uri = api.store.uri_for_path(removed)
            removed.unlink()
            pending = session.get(Video, episodes[1].video_id)
            pending.upload_status, pending.filename = "UPLOADING", "unpublished-upload-name.mp4"
        changed = client.get("/api/series/get", params=params)
        assert changed.status_code == 200, changed.text
        assert "unpublished-upload-name" not in changed.text
        updated = changed.json()["videos"]
        assert [v["playbackStatus"] for v in updated] == ["MISSING", "PREPARING"] + ["READY"] * 6
        assert all(v["playbackUrl"] is None for v in updated[:2])
        assert all(v["uploadProgress"] is None and v["parseProgress"] is None and v["lastError"] is None for v in updated)
        assert client.get(details["videos"][0]["playbackUrl"], headers={"Range": "bytes=1-2"}).status_code == 410
        assert client.get(details["videos"][1]["playbackUrl"], headers={"Range": "bytes=1-2"}).status_code == 409
        assert client.get(media, headers={"Range": "bytes=1-2"}).status_code == 206
        api.services.library.update_series(course.series_id, tenant_id=course.tenant, changes={"visibility": "private"})
        assert client.get("/api/series/public").json()["items"] == []
        assert client.get("/api/series/get", params=params).status_code == 404
        assert client.get(media, headers={"Range": "bytes=3-6"}).status_code == 404
        assert search().status_code == client.get("/api/agent/messages/list", params=chat).status_code == 404
        assert send("继续讨论").status_code == 404


@pytest.mark.skipif(os.getenv("SEEKINO_TEST_PUBLIC_COURSE_BROWSER") != "1",
                    reason="Requires built web/dist, ffmpeg/ffprobe and Playwright Chromium")
def test_public_course_watch_browser(public_course):
    course = public_course
    web = Path(__file__).resolve().parents[2] / "web"
    dist = web / "dist"
    assert (dist / "index.html").is_file(), "Run npm --prefix web run build first"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5",
                    "-t", "20", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(course.source)], check=True)
    # Use the production codec probe and live HTTP Range responses for this check.
    course.api.services.media = MediaService(course.api.services.library, course.api.store)
    course.api.services.parse_consumer = None
    app = course.api.app
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="web-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        return FileResponse(dist / "index.html")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(.02)
            assert server.started
            subprocess.run(["node", "tests/public-course-readiness.mjs", f"http://127.0.0.1:{port}",
                            course.series_id, course.episodes[-1].video_id], cwd=web, check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(10)
    assert len(course.model.turns) == 4 and course.model.turns[-1][1]
    assert len(course.index.requests) == 1
    assert {task.video_id for task in course.index.requests[0].scope.tasks} == {e.video_id for e in course.episodes[:7]}
