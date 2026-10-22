from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from server.api import agent as agent_api
from server.api.errors import register_exception_handlers
from server.config.auth import AuthConfig
from server.config.sse import SSEConfig
from server.constants.identity import LOCAL_USER_ID
from server.database import create_schema, create_session_factory
from server.model.agent import AgentConversation, AgentMessage
from server.model.dto.agentDTO import AgentMessageState
from server.model.dto.authDTO import AuthIdentity
from server.model.job import VideoParseTask
from server.model.tenant import Tenant
from server.model.upload import UploadSession
from server.model.user import User
from server.model.video import Series, SeriesVisibility, Video, VideoChunk
from server.repository.library import LibraryRepository
from server.service.video_parse_persistence import VideoParsePersistenceService
from server.router.library import router as library_router
from server.router.media import router as media_router
from server.router.search import router as search_router
from server.schema import (
    AgentReply,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from server.error.auth import AuthError
from server.service.library import LibraryService
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore
from server.utils.media import encode_video_keyframe_image_id
from server.utils.time import now_utc


PUBLIC = "platform-public"


def _identity(user_id: str, tenant_id: str | None, *, guest=False, admin=False):
    return AuthIdentity(
        user_id=user_id, email=None if guest else f"{user_id}@example.com",
        is_guest=guest, tenant_id=tenant_id, public_tenant_id=PUBLIC,
        role="admin" if admin else "user", auth_version=1,
        auth_level="guest" if guest else "email", mode="cloud",
    )


class FakeAuth:
    def __init__(self):
        self.config = AuthConfig(mode="cloud", secret_key="test-secret-" * 4, cookie_secure=False)
        self.redis = Mock(eval=Mock(return_value=[1, 0]))
        self.identities = {
            "guest": _identity("guest", None, guest=True),
            "guest-b": _identity("guest-b", None, guest=True),
            "alice": _identity("alice", "tenant-alice"),
            "bob": _identity("bob", "tenant-bob"),
            "admin": _identity("admin", "tenant-admin", admin=True),
        }

    def authenticate(self, token):
        if token not in self.identities:
            raise AuthError("请重新登录", status_code=401, code="AUTH_REQUIRED")
        return self.identities[token]

    def verify_csrf(self, token, value):
        if token not in self.identities or value != "csrf":
            raise AuthError("请求校验失败", status_code=403, code="CSRF_INVALID")

    def cache_key(self, *parts):
        return ":".join(parts)


@pytest.fixture
def cloud_library(seed_video, tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    create_schema(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(Tenant(id=PUBLIC, type="public", name="Seekino"))
        for user_id in ("alice", "bob", "admin"):
            session.add(User(id=user_id, email=f"{user_id}@example.com", is_anonymous=False,
                             is_superuser=user_id == "admin", nickname=user_id))
            session.add(Tenant(id=f"tenant-{user_id}", type="personal", owner_id=user_id, name=user_id))
    videos = VideoParsePersistenceService(factory)
    prepared = report = seed_video(factory, tenant_id=PUBLIC)
    store = LocalArtifactStore(tmp_path / "managed", environment="test")
    source = store.base_directory / "course.mp4"
    source.write_bytes(b"managed-course-video")
    frame = store.base_directory / "course.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nregistered-course-frame")
    with factory.begin() as session:
        session.get(Series, prepared.series_id).visibility = SeriesVisibility.PUBLIC
        session.get(Video, prepared.video_id).source_uri = store.uri_for_path(source)
        session.get(VideoChunk, report.video_chunk_ids[0]).keyframe_uris = [
            {"uri": store.uri_for_path(frame), "at_seconds": 0},
        ]
    library = LibraryService(LibraryRepository(factory, execution_mode="v2"))
    probe = Mock(return_value=True)
    media = MediaService(library, store, parse_persistence=videos, probe_playback=probe)
    evidence = videos.get_chunk_evidence(report.video_chunk_ids[0], tenant_id=PUBLIC)
    result = VideoChunkSearchResult(
        query="course", series_ids=[prepared.series_id], searched_video_ids=[prepared.video_id],
        agent_reply=AgentReply(message="Found course", result_count=1),
        hits=[VideoChunkSearchHit(
            candidate=VideoChunkCandidate(video_chunk_id=evidence.video_chunk_id, fused_rank=1, fused_score=0.1),
            selection_reason=SelectionReason(summary="Course evidence", matched_evidence=[], limitations=[]),
            evidence=evidence,
        )],
    )
    auth = FakeAuth()
    retrieval = Mock(search=Mock(return_value=result))
    services = SimpleNamespace(
        library=library, media=media, auth=auth, retrieval=retrieval,
        agent=Mock(message_options=Mock(return_value=SimpleNamespace(
            top_k=SimpleNamespace(default_value=10, minimum=1, maximum=100),
        ))),
    )
    app = FastAPI()
    app.state.services = services
    app.state.auth_config = auth.config
    app.include_router(library_router)
    app.include_router(media_router)
    app.include_router(search_router)
    register_exception_handlers(app)
    client = TestClient(app)
    client.cookies.set(auth.config.cookie_name, "guest")
    yield SimpleNamespace(
        client=client, factory=factory, library=library, media=media, auth=auth,
        prepared=prepared, result=result, retrieval=retrieval, source=source, probe=probe,
    )
    client.close()
    engine.dispose()


def _course(fixture, name, *, tenant_id=PUBLIC, published=True, task_status="COMPLETE", managed=True):
    state = fixture.library.create_series(tenant_id=tenant_id, name=name)
    video_id = str(uuid4())
    with fixture.factory.begin() as session:
        session.get(Series, state.series_id).visibility = SeriesVisibility.PUBLIC if published else SeriesVisibility.PRIVATE
        session.add(Video(
            id=video_id, series_id=state.series_id, episode_order=1, upload_sequence=1,
            filename="private-course.mp4",
            source_uri=(fixture.media._artifact_store.uri_for_path(fixture.source)
                                 if managed else "artifact://missing/course.mp4"),
            hash="f" * 64, size_bytes=20, duration_ms=20,
            upload_status="COMPLETED", parse_status="COMPLETED", upload_progress=100, parse_progress=100,
        ))
        session.add(VideoParseTask(
            id=str(uuid4()), video_id=video_id, pipeline_version="video-parse-v2",
            status=task_status, parse_config={}, chunk_count=1, version=1, max_attempts=3,
        ))
    return state.series_id, video_id


def _login(fixture, token):
    fixture.client.cookies.set(fixture.auth.config.cookie_name, token)


def _visibility(fixture, value, *, series_id=None, tenant_id=PUBLIC):
    return fixture.client.post("/api/series/update", headers={"X-CSRF-Token": "csrf"}, json={
        "tenantId": tenant_id, "seriesId": series_id or fixture.prepared.series_id, "visibility": value,
    })


def _search(fixture, *, series_id=None, tenant_id=PUBLIC):
    return fixture.client.post("/api/search", headers={"X-CSRF-Token": "csrf"}, json={
        "tenantId": tenant_id, "query": "course", "seriesIds": [series_id or fixture.prepared.series_id],
    })


def test_public_library_filters_before_count_and_pagination(cloud_library):
    f = cloud_library
    visible, _ = _course(f, "Second available course")
    _course(f, "Draft course", published=False)
    _course(f, "Processing course", task_status="PENDING")
    _course(f, "Missing media course", managed=False)
    response = f.client.get("/api/series/list", params={"tenantId": PUBLIC, "limit": 1})
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 2 and page["hasMore"]
    assert page["items"][0]["visibility"] == "public"
    assert page["items"][0]["publisher"] == {"kind": "platform", "displayName": "Seekino", "avatar": None}
    next_page = f.client.get("/api/series/list", params={
        "tenantId": PUBLIC, "limit": 1, "cursor": page["nextCursor"],
    }).json()
    assert next_page["total"] == 2 and not next_page["hasMore"]
    assert {page["items"][0]["seriesId"], next_page["items"][0]["seriesId"]} == {f.prepared.series_id, visible}
    _login(f, "guest-b")
    assert f.client.get("/api/series/list", params={
        "tenantId": PUBLIC, "cursor": page["nextCursor"],
    }).status_code == 400


def test_private_media_and_library_require_personal_tenant(cloud_library):
    f = cloud_library
    series_id, video_id = _course(f, "Alice private course", tenant_id="tenant-alice", published=False)
    requests = [
        ("/api/series/list", {"tenantId": "tenant-alice"}),
        ("/api/series/get", {"tenantId": "tenant-alice", "seriesId": series_id}),
        ("/api/videos/content", {"tenantId": "tenant-alice", "videoId": video_id}),
    ]
    for token in ("guest", "bob"):
        _login(f, token)
        for path, params in requests:
            response = f.client.get(path, params=params, headers={"Range": "bytes=0-3"})
            assert response.status_code == 404
            assert "private-course" not in response.text and "artifact://" not in response.text
    _login(f, "alice")
    assert f.client.get(requests[1][0], params=requests[1][1]).status_code == 200
    assert f.client.get(requests[2][0], params=requests[2][1], headers={"Range": "bytes=0-3"}).status_code == 206


@pytest.mark.parametrize("token,visibility,ready,status,access", [
    ("guest", "private", True, 404, None),
    ("bob", "private", True, 404, None),
    ("admin", "private", True, 404, None),
    ("alice", "private", True, 200, "member"),
    ("guest", "public", True, 200, "public"),
    ("bob", "public", True, 200, "public"),
    ("admin", "public", True, 200, "public"),
    ("alice", "public", True, 200, "member"),
    ("guest", "public", False, 404, None),
    ("bob", "public", False, 404, None),
    ("admin", "public", False, 404, None),
    ("alice", "public", False, 200, "member"),
])
def test_personal_series_access_matrix(cloud_library, token, visibility, ready, status, access):
    f = cloud_library
    series_id, _ = _course(f, "Alice shared series", tenant_id="tenant-alice",
                           published=visibility == "public", task_status="COMPLETE" if ready else "PENDING")
    _login(f, token)
    response = f.client.get("/api/series/get", params={"tenantId": "tenant-alice", "seriesId": series_id})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["access"] == access
        assert response.json()["visibility"] == visibility


def test_owner_shares_safe_public_detail_and_can_revoke_media(cloud_library):
    f = cloud_library
    series_id, video_id = _course(f, "Alice shared series", tenant_id="tenant-alice", published=False)
    with f.factory.begin() as session:
        record = session.get(Series, series_id)
        record.transcript_language_hint = "zh-CN"
        record.important_extra_data = {"format": "markdown", "content": "owner background secret"}
        record.character_bible = [{"name": "owner character secret", "aliases": []}]
    _login(f, "alice")
    response = _visibility(f, "public", series_id=series_id, tenant_id="tenant-alice")
    assert response.status_code == 200, response.text
    assert response.json()["access"] == "member"
    assert response.json()["videos"][0]["originalFilename"] == "private-course.mp4"
    params = {"tenantId": "tenant-alice", "seriesId": series_id}
    media_params = {"tenantId": "tenant-alice", "videoId": video_id}
    assert f.client.get("/api/videos/content", params={**media_params, "variant": "original"}).status_code == 200
    for token in ("guest", "bob"):
        _login(f, token)
        response = f.client.get("/api/series/get", params=params)
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["access"] == "public"
        assert detail["publisher"]["kind"] == "user"
        assert detail["publisher"]["avatar"] is None
        assert detail["publisher"]["displayName"] != "alice"
        assert "example.com" not in response.text
        assert detail["transcriptLanguageHint"] is None
        assert detail["importantExtraData"] is None
        assert detail["characterBible"] == [] and detail["pendingUploads"] == []
        for field in ("originalFilename", "totalBytes", "latestJobId", "lastError"):
            assert detail["videos"][0][field] is None
        assert "owner background secret" not in response.text and "owner character secret" not in response.text
        assert f.client.get("/api/series/list", params={"tenantId": "tenant-alice"}).status_code == 404
        assert f.client.get("/api/videos/content", params=media_params, headers={"Range": "bytes=0-3"}).status_code == 206
        assert f.client.get("/api/videos/content", params={**media_params, "variant": "original"}).status_code == 404
    _login(f, "alice")
    assert _visibility(f, "private", series_id=series_id, tenant_id="tenant-alice").status_code == 200
    _login(f, "guest")
    assert f.client.get("/api/series/get", params=params).status_code == 404
    assert f.client.get("/api/videos/content", params=media_params, headers={"Range": "bytes=0-3"}).status_code == 404


def test_admin_takedown_does_not_grant_cross_tenant_edit_or_read(cloud_library):
    f = cloud_library
    series_id, _ = _course(f, "Alice private title", tenant_id="tenant-alice", published=True)
    _login(f, "admin")
    response = f.client.post("/api/series/update", headers={"X-CSRF-Token": "csrf"}, json={
        "tenantId": "tenant-alice", "seriesId": series_id, "visibility": "private", "description": "changed",
    })
    assert response.status_code == 404
    assert _visibility(f, "public", series_id=series_id, tenant_id="tenant-alice").status_code == 404
    response = _visibility(f, "private", series_id=series_id, tenant_id="tenant-alice")
    assert response.status_code == 204 and response.content == b""
    assert f.library.get_series(series_id, tenant_id="tenant-alice").visibility == SeriesVisibility.PRIVATE
    assert f.client.get("/api/series/get", params={"tenantId": "tenant-alice", "seriesId": series_id}).status_code == 404


def test_public_discovery_crosses_tenants_and_cursor_is_not_viewer_scoped(cloud_library):
    f = cloud_library
    alice_id, _ = _course(f, "Alice available", tenant_id="tenant-alice")
    bob_id, _ = _course(f, "Bob available", tenant_id="tenant-bob")
    _course(f, "Alice draft", tenant_id="tenant-alice", published=False)
    _course(f, "Bob processing", tenant_id="tenant-bob", task_status="PENDING")
    _course(f, "Missing available", tenant_id="tenant-bob", managed=False)
    first = f.client.get("/api/series/public", params={"limit": 1})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["total"] == 3 and page["hasMore"]
    items = page["items"]
    _login(f, "bob")
    while page["hasMore"]:
        response = f.client.get("/api/series/public", params={"limit": 1, "cursor": page["nextCursor"]})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 3
        items.extend(page["items"])
    assert {item["seriesId"] for item in items} == {f.prepared.series_id, alice_id, bob_id}
    assert {item["tenantId"] for item in items} == {PUBLIC, "tenant-alice", "tenant-bob"}
    assert all(item["videoCount"] == 1 and item["publisher"] is not None for item in items)
    assert f.client.get("/api/series/public", params={"cursor": first.json()["nextCursor"], "query": "Alice"}).status_code == 400


@pytest.mark.parametrize("disable", ["owner", "tenant"])
def test_disabled_publisher_disappears_from_public_detail_media_and_discovery(cloud_library, disable):
    f = cloud_library
    series_id, video_id = _course(f, "Alice available", tenant_id="tenant-alice")
    with f.factory.begin() as session:
        record = session.get(User, "alice") if disable == "owner" else session.get(Tenant, "tenant-alice")
        record.status = "DISABLED"
    assert f.client.get("/api/series/get", params={"tenantId": "tenant-alice", "seriesId": series_id}).status_code == 404
    assert f.client.get("/api/videos/content", params={"tenantId": "tenant-alice", "videoId": video_id}).status_code == 404
    page = f.client.get("/api/series/public").json()
    assert page["total"] == 1 and page["items"][0]["seriesId"] == f.prepared.series_id


def test_withdrawal_blocks_existing_media_image_chunk_and_range_urls(cloud_library):
    f = cloud_library
    series = f.library.get_series(f.prepared.series_id, tenant_id=PUBLIC)
    endpoints = [
        ("/api/series/get", {"tenantId": PUBLIC, "seriesId": series.series_id}),
        ("/api/videos/content", {"tenantId": PUBLIC, "videoId": f.prepared.video_id}),
        ("/api/videos/content", {"tenantId": PUBLIC, "videoId": f.prepared.video_id, "variant": "original"}),
        ("/api/media/images/content", {"tenantId": PUBLIC, "imageId": encode_video_keyframe_image_id(f.prepared.video_id, 0)}),
        ("/api/video-chunks/get", {"tenantId": PUBLIC, "videoChunkId": f.result.hits[0].evidence.video_chunk_id}),
    ]
    chunk_image = f.media.keyframe_views(f.result.hits[0].evidence, tenant_id=PUBLIC)[0]
    endpoints.append(("/api/media/images/content", {"tenantId": PUBLIC, "imageId": chunk_image.image_id}))
    image_bytes = BytesIO()
    Image.new("RGB", (4, 4), "green").save(image_bytes, format="PNG")
    background = f.media.upload_series_image(series.series_id, image_bytes.getvalue(), tenant_id=PUBLIC)
    endpoints.append(("/api/media/images/content", {"tenantId": PUBLIC, "imageId": background.image_id}))
    for path, params in endpoints:
        response = f.client.get(path, params=params)
        assert response.status_code == (404 if params.get("variant") == "original" else 200)
        if response.status_code == 200 and path in {"/api/videos/content", "/api/media/images/content"}:
            assert response.headers["cache-control"] == "private, no-store"
    _login(f, "admin")
    assert _visibility(f, "private").status_code == 200
    _login(f, "guest")
    for path, params in endpoints:
        response = f.client.get(path, params=params, headers={"Range": "bytes=0-3"})
        assert response.status_code == 404
        assert "artifact://" not in response.text and "Course evidence" not in response.text
    assert _search(f).status_code == 404
    f.retrieval.search.assert_not_called()


def test_visibility_requires_ownership_ready_and_playable_media(cloud_library):
    f = cloud_library
    for token, expected in (("guest", 403), ("alice", 404)):
        _login(f, token)
        assert _visibility(f, "private").status_code == expected
    _login(f, "admin")
    private_id, _ = _course(f, "Admin private course", tenant_id="tenant-admin", published=False)
    assert _visibility(f, "public", series_id=private_id, tenant_id="tenant-admin").status_code == 200
    assert _visibility(f, "private").status_code == 200
    f.probe.return_value = False
    assert _visibility(f, "public").status_code == 400
    f.probe.return_value = True
    assert _visibility(f, "public").json()["visibility"] == "public"
    not_ready, _ = _course(f, "Unready course", published=False, task_status="PENDING")
    assert _visibility(f, "public", series_id=not_ready).status_code == 400
    assert _visibility(f, None).status_code == 400
    assert _visibility(f, "unlisted").status_code == 422
    assert _visibility(f, True).status_code == 422
    f.source.unlink()
    _login(f, "guest")
    assert f.client.get("/api/series/list", params={"tenantId": PUBLIC}).json()["total"] == 0


def test_pending_upload_hides_course_and_blocks_publication(cloud_library):
    f = cloud_library
    with f.factory.begin() as session:
        session.add(UploadSession(
            id=str(uuid4()), tenant_id=PUBLIC, series_id=f.prepared.series_id,
            creator_id="admin", episode_order=2, upload_sequence=2,
            filename="future-private.mp4", media_type="video/mp4", size_bytes=20,
            segment_size_bytes=20, segment_count=1, status="UPLOADING",
            idempotency_key="pending-course", expire_time=now_utc() + timedelta(hours=1),
        ))
    assert f.client.get("/api/series/list", params={"tenantId": PUBLIC}).json()["total"] == 0
    assert f.client.get("/api/series/get", params={"tenantId": PUBLIC, "seriesId": f.prepared.series_id}).status_code == 404
    assert _search(f).status_code == 404
    f.retrieval.search.assert_not_called()
    _login(f, "admin")
    assert _visibility(f, "public").status_code == 400


def test_last_search_is_scoped_to_user_within_public_tenant(cloud_library):
    f = cloud_library
    with f.factory.begin() as session:
        for index, owner in enumerate(("guest", "guest-b", LOCAL_USER_ID)):
            conversation_id = str(uuid4())
            session.add(AgentConversation(
                id=conversation_id, tenant_id=PUBLIC, owner_id=owner,
                title=owner, idempotency_key=f"conversation-{owner}",
            ))
            session.add(AgentMessage(
                id=str(uuid4()), conversation_id=conversation_id, sequence_no=0,
                role="USER", status="COMPLETED", content=f"private query by {owner}",
                series_ids=[f.prepared.series_id], create_time=1000 + index,
            ))
    for token in ("guest", "guest-b"):
        _login(f, token)
        response = f.client.get("/api/series/get", params={"tenantId": PUBLIC, "seriesId": f.prepared.series_id})
        assert response.json()["lastSearch"]["query"] == f"private query by {token}"
        page = f.client.get("/api/series/list", params={"tenantId": PUBLIC}).json()
        assert page["items"][0]["lastSearch"]["query"] == f"private query by {token}"
    assert f.library.get_series(f.prepared.series_id, tenant_id=PUBLIC).last_search.query == f"private query by {LOCAL_USER_ID}"


def test_search_quota_denial_happens_before_retrieval(cloud_library):
    f = cloud_library
    f.auth.redis.eval.return_value = [0, 30]
    response = _search(f)
    assert response.status_code == 429 and response.headers["retry-after"] == "30"
    f.retrieval.search.assert_not_called()
    f.auth.redis.zrem.assert_not_called()


@pytest.mark.parametrize("token,public_view", [("guest", True), ("bob", True), ("admin", False)])
def test_chunk_and_search_routes_project_filename_for_actual_viewer(cloud_library, token, public_view):
    f = cloud_library
    _login(f, token)
    source = f.result.hits[0].evidence
    expected = None if public_view else source.original_filename
    assert source.original_filename
    chunk = f.client.get("/api/video-chunks/get", params={
        "tenantId": PUBLIC, "videoChunkId": source.video_chunk_id,
    })
    assert chunk.status_code == 200, chunk.text
    assert chunk.json()["originalFilename"] == expected
    searched = _search(f)
    assert searched.status_code == 200, searched.text
    assert searched.json()["hits"][0]["evidence"]["originalFilename"] == expected


@pytest.mark.parametrize("token,public_view", [("guest", True), ("bob", True), ("admin", False)])
def test_agent_history_and_evidence_stream_project_filename_for_actual_viewer(cloud_library, token, public_view):
    f = cloud_library
    identity = f.auth.identities[token]
    result = f.result.model_dump(by_alias=True, exclude={"query"})
    message = AgentMessageState(
        message_id=str(uuid4()), conversation_id=str(uuid4()), sequence_no=1,
        role="ASSISTANT", status="COMPLETED", content="Found course",
        series_ids=(f.prepared.series_id,), model_name=None, search_result=result,
        last_error=None, created_at=1, updated_at=1, source_tenant_id=PUBLIC,
    )
    expected = None if public_view else f.result.hits[0].evidence.original_filename
    historical = agent_api._agent_message_view(
        message, media=f.media, tenant_id=identity.tenant_id or PUBLIC, identity=identity,
    )
    assert historical.search_result.hits[0].evidence.original_filename == expected
    request = Request({
        "type": "http", "method": "GET", "path": "/api/agent/messages/events",
        "app": f.client.app,
        "headers": [(b"cookie", f"{f.auth.config.cookie_name}={token}".encode())],
    })
    request.is_disconnected = AsyncMock(return_value=False)
    execution = SimpleNamespace(
        next_event=Mock(side_effect=[{
            "type": "answer.evidence", "conversationId": message.conversation_id,
            "messageId": message.message_id, "sequence": 1, "occurredAt": 1,
            "searchResult": result,
        }, None]), cancellation=Mock(), detach=Mock(),
    )

    async def collect():
        return [event async for event in agent_api._answer_events(
            request, execution, SSEConfig(), media=f.media, tenant_id=PUBLIC,
            series_ids=[f.prepared.series_id], chat_tenant_id=identity.tenant_id or PUBLIC,
        )]

    events = asyncio.run(collect())
    assert len(events) == 1
    payload = json.loads(next(line[6:] for line in events[0].splitlines() if line.startswith("data: ")))
    assert payload["searchResult"]["hits"][0]["evidence"]["originalFilename"] == expected
    execution.detach.assert_called_once()


def test_search_revalidates_publication_after_model_work(cloud_library):
    f = cloud_library
    def withdraw(*args, **kwargs):
        with f.factory.begin() as session:
            session.get(Series, f.prepared.series_id).visibility = SeriesVisibility.PRIVATE
        return f.result
    f.retrieval.search.side_effect = withdraw
    response = _search(f)
    assert response.status_code == 404
    assert "Course evidence" not in response.text and "playbackUrl" not in response.text
    assert f.auth.redis.eval.call_count == 1 and f.auth.redis.zrem.call_count == 3


def test_search_revalidates_session_after_model_work(cloud_library):
    f = cloud_library
    def revoke(*args, **kwargs):
        del f.auth.identities["guest"]
        return f.result
    f.retrieval.search.side_effect = revoke
    assert _search(f).status_code == 401
    assert f.auth.redis.zrem.call_count == 3


def test_search_checks_result_evidence_series_and_releases_query_lease(cloud_library):
    f = cloud_library
    assert _search(f).status_code == 200
    assert f.auth.redis.eval.call_count == 1 and f.auth.redis.zrem.call_count == 3
    hidden, _ = _course(f, "Hidden source", published=False)
    evidence = f.result.hits[0].evidence.model_copy(update={"series_id": hidden})
    hit = f.result.hits[0].model_copy(update={"evidence": evidence})
    f.retrieval.search.return_value = f.result.model_copy(update={"series_ids": [hidden], "hits": [hit]})
    response = _search(f)
    assert response.status_code == 404
    assert "Course evidence" not in response.text
    assert f.auth.redis.zrem.call_count == 6
