"""Single-episode visual find through the public HTTP routes and V2 persistence."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask, VideoParseTask
from server.model.video import Series, SeriesVisibility
from server.repository import video_parse_retrieval as read_store
from server.service.media import MediaService
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server.test_cloud_access import _guest, cloud_api
from tests.server.test_public_course_readiness import public_course
from tests.server.test_video_text_find import watch_text


TIMELINE = "/api/videos/visual-timeline"
SEARCH = "/api/videos/visual-search"


@pytest.fixture
def watch_visuals(public_course):
    course = public_course
    services = course.api.services
    services.event_retrieval = course.events
    services.media = MediaService(services.library, course.api.store,
                                  parse_persistence=VideoParsePersistenceService(course.api.sessions))
    with TestClient(course.api.app) as client:
        client.headers["X-CSRF-Token"] = _guest(client)["csrfToken"]
        yield course, client


def test_timeline_projects_visible_people_and_preserves_text_without_an_event_index(watch_text):
    data = watch_text
    scope = {"tenantId": data.tenant, "videoId": data.matching.video_id}
    with data.api.sessions.begin() as session:
        content = session.scalar(select(ChunkParseContent).join(ChunkParseTask)
                                 .where(ChunkParseTask.chunk_id == data.matching.video_chunk_ids[0]))
        action, dialogue = content.content["timeline"]
        content.content = {"persons": ["千早愛音", "高松燈"], "timeline": [
            {**action, "description": "坐在沙发上看手机", "visible_persons": ["千早愛音"]},
            {**dialogue, "speaker": "高松燈", "participants": ["高松燈"]},
            {**dialogue, "start_seconds": 6270, "end_seconds": 7345, "description": "镜头转向听她说话的人",
             "speaker": "高松燈", "participants": ["高松燈"], "visible_persons": ["千早愛音"]},
        ]}
    data.api.services.event_retrieval = None
    with TestClient(data.api.app) as client:
        client.headers["X-CSRF-Token"] = _guest(client)["csrfToken"]
        response = client.get(TIMELINE, params=scope)
        assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        body = response.json()
        assert {"千早愛音", "高松燈"} <= set(body.pop("knownPersons"))
        assert body == {"ready": True, "cues": [
            {"startMs": 1000, "endMs": 3000, "kind": "ACTION", "description": "坐在沙发上看手机",
             "visiblePersons": ["千早愛音"]},
            {"startMs": 6270, "endMs": 7345, "kind": "DIALOGUE", "description": "镜头转向听她说话的人",
             "visiblePersons": ["千早愛音"]},
            {"startMs": 11000, "endMs": 13000, "kind": "ACTION", "description": "第 2 段动作", "visiblePersons": []},
        ]}
        sibling = client.get(TIMELINE, params={**scope, "videoId": data.empty.video_id}).json()
        assert all(not cue["visiblePersons"] for cue in sibling["cues"]) and len(sibling["cues"]) == 2
        text = client.get("/api/videos/text-timeline", params=scope).json()
        assert "我是高松灯" in text["cues"][0]["texts"]
        assert client.post(SEARCH, json={**scope, "query": "看手机"}).json() == {"ready": False, "cues": []}


def test_search_limits_sql_to_the_requested_video_before_embedding(watch_visuals, monkeypatch):
    course, client = watch_visuals
    episode = course.episodes[2]
    selected = []
    list_videos = read_store.list_series_videos
    embed = course.events._embedder.embed_query

    def read_videos(*args, **kwargs):
        rows = list_videos(*args, **kwargs)
        selected.append([row.id for row in rows])
        return rows

    def embed_query(query):
        assert selected == [[episode.video_id]]
        return embed(query)

    monkeypatch.setattr(read_store, "list_series_videos", read_videos)
    monkeypatch.setattr(course.events._embedder, "embed_query", embed_query)
    response = client.post(SEARCH, json={"tenantId": course.tenant, "videoId": episode.video_id, "query": "举起杯子"})
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    assert course.events._embedder.calls == ["举起杯子"]
    calls = course.event_store.calls
    assert [channel for channel, _ in calls] == ["lexical", "semantic", "vision"]
    sources = {(episode.video_parse_task_id, chunk, 1) for chunk in episode.video_chunk_ids}
    assert all(set(call["sources"]) == sources for _, call in calls)
    result = response.json()
    assert result["ready"] and sorted(result["cues"], key=lambda cue: cue["startMs"]) == [
        {"startMs": 1000, "endMs": 3000, "kind": "ACTION", "description": "第 1 段动作", "visiblePersons": []},
        {"startMs": 11000, "endMs": 13000, "kind": "ACTION", "description": "第 2 段动作", "visiblePersons": []},
    ]


def test_search_rejects_an_indexed_description_that_disagrees_with_the_published_timeline(watch_visuals):
    course, client = watch_visuals
    episode = course.episodes[0]
    for index, plan in enumerate(course.event_store.plans):
        if plan.video_chunk_id == episode.video_chunk_ids[0]:
            stale = plan.lexical_fields[0].model_copy(update={"raw_text": "旧索引里的画面"})
            course.event_store.plans[index] = replace(plan, lexical_fields=(stale, *plan.lexical_fields[1:]))

    response = client.post(SEARCH, json={"tenantId": course.tenant, "videoId": episode.video_id, "query": "找画面"})
    assert response.status_code == 200
    assert response.json() == {"ready": False, "cues": [
        {"startMs": 11000, "endMs": 13000, "kind": "ACTION", "description": "第 2 段动作", "visiblePersons": []},
    ]}


@pytest.mark.parametrize("change", ["content_version", "new_parse"])
def test_a_changed_content_or_parse_version_hides_past_visuals(watch_visuals, change):
    course, client = watch_visuals
    episode = course.episodes[0]
    with course.api.sessions.begin() as session:
        if change == "content_version":
            content = session.scalar(select(ChunkParseContent).join(ChunkParseTask)
                                     .where(ChunkParseTask.chunk_id == episode.video_chunk_ids[1]))
            content.version += 1
        else:
            task = session.get(VideoParseTask, episode.video_parse_task_id)
            session.add(VideoParseTask(id=str(uuid4()), video_id=task.video_id, pipeline_version=task.pipeline_version,
                                       status="PENDING", parse_config=deepcopy(task.parse_config),
                                       chunk_count=task.chunk_count, version=task.version + 1, max_attempts=3, is_test=True))
    scope = {"tenantId": course.tenant, "videoId": episode.video_id}
    assert client.get(TIMELINE, params=scope).json() == {"ready": False, "cues": []}
    assert client.post(SEARCH, json={**scope, "query": "动作"}).json() == {"ready": False, "cues": []}
    assert not course.event_store.calls and not course.events._embedder.calls


def test_unavailable_event_index_does_not_search_sibling_episodes(watch_visuals):
    course, client = watch_visuals
    # E07 has a published timeline without Event receipts; E08 is still parsing.
    for episode, timeline_ready in ((course.episodes[6], True), (course.episodes[7], False)):
        scope = {"tenantId": course.tenant, "videoId": episode.video_id}
        assert client.get(TIMELINE, params=scope).json()["ready"] is timeline_ready
        assert client.post(SEARCH, json={**scope, "query": "动作"}).json() == {"ready": False, "cues": []}
    course.event_store.ready = False
    scope = {"tenantId": course.tenant, "videoId": course.episodes[0].video_id}
    assert client.get(TIMELINE, params=scope).json()["ready"]
    assert client.post(SEARCH, json={**scope, "query": "动作"}).json() == {"ready": False, "cues": []}
    assert not course.event_store.calls and not course.events._embedder.calls


def test_visual_routes_check_tenant_visibility_and_inflight_withdrawal(watch_visuals, monkeypatch):
    course, client = watch_visuals
    scope = {"tenantId": course.tenant, "videoId": course.episodes[0].video_id}
    wrong_tenant = {**scope, "tenantId": "another-tenant"}
    assert client.get(TIMELINE, params=wrong_tenant).status_code == 404
    assert client.post(SEARCH, json={**wrong_tenant, "query": "动作"}).status_code == 404
    assert not course.events._embedder.calls and not course.event_store.calls

    embed = course.events._embedder.embed_query

    def withdraw_during_query(query):
        with course.api.sessions.begin() as session:
            session.get(Series, course.series_id).visibility = SeriesVisibility.PRIVATE
        return embed(query)

    monkeypatch.setattr(course.events._embedder, "embed_query", withdraw_during_query)
    response = client.post(SEARCH, json={**scope, "query": "动作"})
    assert response.status_code == 404 and "第 1 段动作" not in response.text
    assert client.get(TIMELINE, params=scope).status_code == 404
    assert client.post(SEARCH, json={**scope, "query": "动作"}).status_code == 404
    assert course.events._embedder.calls == ["动作"]
