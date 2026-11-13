"""Cloud guest routes through SQL scope resolution, recall, hydration and Agent tools."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from elastic_transport import ConnectionError as ElasticConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select

from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from engine.parsev2.schemas import VideoParseResult
from engine.rag import EncoderContract
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.indexing.events import build_event_index_receipt, plan_event_index
from engine.rag.retrieval.contracts import EventIndexNotReadyError, RawChunkHit, RetrievalUnavailableError, SeriesNotReadyError
from server.config.parse_profile import default_video_parse_v2_config
from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask, VideoParseTask
from server.model.video import Video, VideoChunk
from server.service.media import MediaService
from server.service.video_parse_content import EVENT_INDEX_TARGET_DETAIL_KEY
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY, VideoParsePersistenceService
from server.service.video_retrieval import RetrievalIndexConsistencyError, VideoParseRetrievalService
from tests.server.test_agent_chat_service import _DirectSupervisorModel, _QueryModel, _WebSearcherStub
from tests.server.test_cloud_access import _conversation, _guest, cloud_api
from tests.server.watch_browser import run_watch_browser
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
        requests = {
            "搜整门课": ("retrieve", None), "只查 E08": ("retrieve", 8), "只查 E09": ("retrieve", 9),
            "搜整门课的事件": ("search_video_events", None), "查 E07 的事件": ("search_video_events", 7),
            "查 E08 的事件": ("search_video_events", 8), "查 E09 的事件": ("search_video_events", 9),
            "委派查 E08": ("query_video", 8), "委派查 E09": ("query_video", 9),
            "只查第一个片段的事件": ("search_video_events", None),
            "检索后只发文字": ("retrieve", None),
        }
        if user_message not in requests:
            handle_text("可以继续讨论。" if history else "你好，我们可以聊这门课。")
            return
        by_name = {tool.name: tool for tool in tools}
        arguments = {"query": "课程", "top_k": 8, "channel_weights": {"lexical": 1, "semantic": 0, "visual": 0}}
        tool, episode = requests[user_message]
        if tool == "query_video":
            result = json.loads(by_name[tool].invoke({"query": "课程", "episode": episode}))
            handle_text(result["reports"][0]["error"]["message"])
            return
        if episode is not None:
            arguments["episode"] = episode
        if user_message == "只查第一个片段的事件":
            arguments["video_chunk_ids"] = [self.first_chunk_id]
        result = json.loads(by_name[tool].invoke(arguments))
        if "error" in result:
            handle_text(result["error"]["message"])
            return
        if result["items"] and user_message != "检索后只发文字":
            hit = result["items"][0]
            start = hit["start_seconds"]
            by_name["submit_video_item"].invoke({
                "video_chunk_id": hit["video_chunk_id"], "start_seconds": start + 1, "end_seconds": start + 3, "claim": "课程示例",
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
    model.first_chunk_id = episodes[0].video_chunk_ids[0]
    events, _, event_store, _, _ = event_search_fixture()
    contract = events._contract
    target = {"index_name": event_store.index_name, "contract_id": contract.contract_id, "contract": contract.as_dict()}
    event_store.plans = []
    # E01–E06 have paired Event receipts, E07 is a valid Chunk-only publication.
    with api.sessions.begin() as session:
        for episode in episodes[:6]:
            task = session.get(VideoParseTask, episode.video_parse_task_id)
            task.detail = {**task.detail, EVENT_INDEX_TARGET_DETAIL_KEY: target}
            for content, chunk in session.execute(select(ChunkParseContent, VideoChunk).select_from(ChunkParseTask)
                    .join(ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id)
                    .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
                    .where(ChunkParseTask.video_task_id == task.id).order_by(VideoChunk.sequence_no)):
                plans = plan_event_index(task.id, chunk.id, content.version, VideoParseResult.model_validate(content.content),
                                         chunk.start_ms, chunk.end_ms, contract)
                detail = deepcopy(content.detail)
                detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["events"] = build_event_index_receipt(
                    event_store.index_name, contract, plans,
                ).as_dict()
                content.detail = detail
                event_store.plans.extend(plans)
    events._persistence = VideoParsePersistenceService(api.sessions)
    api.services.agent._runtime = SupervisorRuntime(
        AgentRuntime(_QueryModel(), search.search, event_retrieve=events.search, retrieval_channel="lexical"), model, _WebSearcherStub(),
        max_web_results=1, web_timeout_seconds=5, execution_timeout_seconds=30,
        max_tool_result_bytes=16_384, max_prompt_chars=200_000,
    )
    api.services.agent._runtimes.append(api.services.agent._runtime)
    return SimpleNamespace(api=api, series_id=series.series_id, tenant=tenant, episodes=episodes,
                           index=index, embedder=embedder, model=model, source=source, events=events, event_store=event_store)


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
            assert result["partial"] and result["coverageMessages"] == result["warnings"] == ["本次检索覆盖 E01–E07，E08 正在处理"]
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
        missing_episode = search(episodeOrder=9)
        assert missing_episode.status_code == 404 and missing_episode.json()["code"] == "EPISODE_NOT_FOUND"
        assert len(index.requests) == calls_before
        assert len(course.embedder.calls) == embeddings_before

        conversation = _conversation(client, guest)
        chat = {"tenantId": course.tenant, "conversationId": conversation}
        playback = {"seriesId": course.series_id, "videoId": episodes[-1].video_id, "episodeOrder": 8, "positionSeconds": 6_000}

        def send(question):
            return client.post("/api/agent/messages/send", params=chat,
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={**scope, "content": question, "extraContext": {"playback": playback}})

        for question in ("你好", "继续解释", "搜整门课", "只查 E08", "查 E08 的事件", "委派查 E08",
                         "只查 E09", "查 E09 的事件", "委派查 E09", "继续讨论"):
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
            if "E09" in question:
                assert answer["content"] == "所选课程中没有这一集，请核对集数" and not answer["searchResult"]
        if searchable:
            for question, covered, warning in (
                ("搜整门课的事件", episodes[:6], "本次检索覆盖 E01–E06，E07 暂无可检索的事件索引，E08 正在处理"),
                ("只查第一个片段的事件", episodes[:1], "本次检索覆盖 E01 的指定片段"),
            ):
                answer = send(question).json()["assistantMessage"]
                result = answer["searchResult"]
                assert result["coverageMessages"] == [warning] and result["retrievalTrace"] == []
                assert set(result["searchedVideoIds"]) == {episode.video_id for episode in covered}
                assert result["hits"] and result["hadCandidates"] is True
            assert send("查 E07 的事件").json()["assistantMessage"]["content"] == "所选范围暂时无法检索事件"
            no_card = send("检索后只发文字").json()["assistantMessage"]
            evidence = no_card["searchResult"]
            assert evidence["hadCandidates"] is True and evidence["hits"] == evidence["retrievalTrace"] == []
            history = client.get("/api/agent/messages/list", params=chat).json()["items"]
            assert next(m for m in history if m["messageId"] == no_card["messageId"])["searchResult"] == evidence
            index.empty = True
            assert send("检索后只发文字").json()["assistantMessage"]["searchResult"]["hadCandidates"] is False
            index.empty = False
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
        hidden = search(episodeOrder=9)
        assert hidden.status_code == 404 and hidden.json()["code"] == "NOT_FOUND"
        assert send("继续讨论").status_code == 404


def test_event_scope_matches_published_sources_even_without_hits(public_course, seed_video, monkeypatch):
    course = public_course
    episodes = course.episodes
    options = dict(tenant_id=course.tenant, series_ids=[course.series_id], top_k=20,
                   lexical_weight=1, text_weight=0, vision_weight=0)
    search = course.events.search
    whole = search("动作", **options).chunk_result
    sources = course.event_store.calls[-1][1]["sources"]
    assert set(sources) == {(ep.video_parse_task_id, chunk_id, 1) for ep in episodes[:6] for chunk_id in ep.video_chunk_ids}
    assert whole.partial and whole.searched_video_ids == [ep.video_id for ep in episodes[:6]]
    assert {hit.evidence.episode_order for hit in whole.hits} == set(range(1, 7))
    first = episodes[0].video_chunk_ids[0]
    result = search("对白", video_chunk_ids=[first], event_types=["DIALOGUE"], **options).chunk_result
    assert result.hits == [] and result.searched_video_ids == [episodes[0].video_id]
    assert not result.partial and result.coverage_messages == ["本次检索覆盖 E01 的指定片段"]
    assert course.event_store.calls[-1][1]["sources"] == ((episodes[0].video_parse_task_id, first, 1),)

    mixed = search("动作", video_chunk_ids=[first, episodes[6].video_chunk_ids[0], episodes[7].video_chunk_ids[0]],
                   **options).chunk_result
    assert mixed.partial and mixed.searched_video_ids == [episodes[0].video_id]
    assert mixed.coverage_messages == ["本次检索覆盖 E01 的指定片段，E07 暂无可检索的事件索引，E08 正在处理"]
    before = len(course.event_store.calls)
    foreign = seed_video(course.api.sessions, tenant_id="another-tenant")
    for invalid_scope in ({"video_chunk_ids": [first, foreign.video_chunk_ids[0]]},
                          {"video_chunk_ids": [first, str(uuid4())]}, {"video_chunk_ids": [first], "episode_order": 2}):
        with pytest.raises(EventIndexNotReadyError, match="outside"):
            search("动作", **invalid_scope, **options)
    with pytest.raises(EventIndexNotReadyError):
        search("动作", episode_order=7, **options)
    with pytest.raises(SeriesNotReadyError) as pending:
        search("动作", video_chunk_ids=[episodes[-1].video_chunk_ids[0]], **options)
    assert pending.value.public_message == "该集正在解析，暂时无法检索"
    assert len(course.event_store.calls) == before

    real_search = course.event_store.search_lexical
    def wrong_episode(query, **kwargs):
        kwargs["sources"] = [(episodes[1].video_parse_task_id, episodes[1].video_chunk_ids[0], 1)]
        return real_search(query, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(course.event_store, "search_lexical", wrong_episode)
        with pytest.raises(RetrievalIndexConsistencyError, match="escaped"):
            search("动作", video_chunk_ids=[first], **options)

    def offline(_contract):
        raise ElasticConnectionError("private Event service detail")

    monkeypatch.setattr(course.event_store, "bind_contract", offline)
    with pytest.raises(RetrievalUnavailableError):
        search("动作", episode_order=1, **options)
    with pytest.raises(EventIndexNotReadyError):
        search("动作", episode_order=7, **options)


def test_event_queries_use_each_videos_chunk_generation(public_course):
    course = public_course
    episode = course.episodes[1]
    with course.api.sessions.begin() as session:
        parent = session.get(VideoParseTask, episode.video_parse_task_id)
        config = deepcopy(parent.parse_config)
        config["indexing"]["index_generation"] = 2
        parent.parse_config = config
        for content in session.scalars(select(ChunkParseContent).join(ChunkParseTask)
                                       .where(ChunkParseTask.video_task_id == parent.id)):
            detail = deepcopy(content.detail)
            detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["index_generation"] = 2
            content.detail = detail
    options = dict(tenant_id=course.tenant, series_ids=[course.series_id], top_k=20)
    assert course.events.search("动作", episode_order=2, **options).chunk_result.hits
    whole = course.events.search("动作", **options).chunk_result
    assert whole.searched_video_ids == [ep.video_id for ep in course.episodes[:6]]
    assert {hit.evidence.parse_provenance["indexGeneration"] for hit in whole.hits if hit.evidence.episode_order == 2} == {2}


@pytest.mark.parametrize("top_k", [1, 5])
def test_event_search_reads_only_bounded_candidate_bodies_without_index_cache(public_course, monkeypatch, top_k):
    course = public_course
    with course.api.sessions.begin() as session:
        for content in session.scalars(select(ChunkParseContent)):
            detail = deepcopy(content.detail)
            detail.setdefault(INDEX_EMBEDDINGS_DETAIL_KEY, {})["representations"] = {"vector": [0.123456789] * 4096}
            content.detail = detail
    course.events._policy = course.events._policy.model_copy(update={"candidate_budget": top_k})
    loaded, body_queries = [], []

    def record_content(content, _context):
        assert "detail" in inspect(content).unloaded
        loaded.append(content.chunk_task_id)

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        columns = statement.split("FROM", 1)[0]
        assert "chunk_parse_content.detail" not in columns
        if "chunk_parse_content.content" in columns:
            body_queries.append(statement)

    real_search = course.event_store.search_lexical

    def recall(query, **kwargs):
        assert loaded == body_queries == []
        assert len(kwargs["sources"]) == 12
        return real_search(query, **kwargs)

    monkeypatch.setattr(course.event_store, "search_lexical", recall)
    engine = course.api.sessions.kw["bind"]
    event.listen(ChunkParseContent, "load", record_content)
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        result = course.events.search(
            "动作", tenant_id=course.tenant, series_ids=[course.series_id], top_k=top_k,
            lexical_weight=1, text_weight=0, vision_weight=0,
        ).chunk_result
    finally:
        event.remove(ChunkParseContent, "load", record_content)
        event.remove(engine, "before_cursor_execute", record_sql)
    assert len(result.hits) == len(loaded) == len(set(loaded)) == top_k
    assert len(body_queries) == 1


@pytest.mark.parametrize("issue", ["missing", "forged", "absent_cache"])
def test_event_query_uses_completed_publication_instead_of_rechecking_cache(public_course, issue):
    course = public_course
    episode = course.episodes[1]
    with course.api.sessions.begin() as session:
        content = session.scalar(select(ChunkParseContent).join(ChunkParseTask)
                                 .where(ChunkParseTask.chunk_id == episode.video_chunk_ids[1]))
        detail = deepcopy(content.detail)
        receipt = detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]
        if issue == "missing":
            del receipt["events"]
        elif issue == "forged":
            receipt["events"]["event_set_sha256"] = "0" * 64
        else:
            del detail[INDEX_EMBEDDINGS_DETAIL_KEY]
        content.detail = detail
    options = dict(tenant_id=course.tenant, series_ids=[course.series_id], top_k=20)
    whole = course.events.search("动作", **options).chunk_result
    assert whole.searched_video_ids == [ep.video_id for ep in course.episodes[:6]]
    assert {hit.evidence.video_id for hit in whole.hits} == {ep.video_id for ep in course.episodes[:6]}
    single = course.events.search("动作", video_chunk_ids=[episode.video_chunk_ids[1]], **options).chunk_result
    assert len(single.hits) == 1 and not single.partial


@pytest.mark.parametrize("issue", ["stale_version", "pending_chunk", "index_target"])
def test_event_query_excludes_incomplete_current_publications_per_video(public_course, issue):
    course = public_course
    episode = course.episodes[1]
    with course.api.sessions.begin() as session:
        child = session.scalar(select(ChunkParseTask).where(ChunkParseTask.chunk_id == episode.video_chunk_ids[1]))
        if issue == "stale_version":
            session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id)).version += 1
        elif issue == "pending_chunk":
            child.status = "QUEUED"
        else:
            task = session.get(VideoParseTask, episode.video_parse_task_id)
            detail = deepcopy(task.detail)
            detail[EVENT_INDEX_TARGET_DETAIL_KEY]["contract_id"] = "another-contract"
            task.detail = detail
    options = dict(tenant_id=course.tenant, series_ids=[course.series_id], top_k=20)
    expected = [ep for ep in course.episodes[:6] if ep != episode]
    whole = course.events.search("动作", **options).chunk_result
    assert whole.partial and whole.searched_video_ids == [ep.video_id for ep in expected]
    assert {hit.evidence.video_id for hit in whole.hits} == {ep.video_id for ep in expected}
    assert {source[0] for _, call in course.event_store.calls for source in call["sources"]} == {
        ep.video_parse_task_id for ep in expected
    }
    with pytest.raises(EventIndexNotReadyError):
        course.events.search("动作", video_chunk_ids=[episode.video_chunk_ids[0]], **options)


@pytest.mark.skipif(os.getenv("SEEKINO_TEST_PUBLIC_COURSE_BROWSER") != "1",
                    reason="Requires built web/dist, ffmpeg/ffprobe and Playwright Chromium")
def test_public_course_watch_browser(public_course):
    course = public_course
    course.api.services.media = MediaService(course.api.services.library, course.api.store)
    course.api.services.parse_consumer = None
    run_watch_browser(course.api.app, course.source, "public-course-readiness.mjs",
                      course.series_id, course.episodes[-1].video_id)
    assert len(course.model.turns) == 8 and course.model.turns[-1][1]
    assert len(course.index.requests) == 2
    assert all({task.video_id for task in request.scope.tasks} == {e.video_id for e in course.episodes[:7]}
               for request in course.index.requests)
    assert [len(call["sources"]) for _, call in course.event_store.calls] == [12, 1]
