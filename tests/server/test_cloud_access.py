from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import Request
import pytest
from sqlalchemy import select

from server.api.app import create_app
from server.api.agent import _agent_message_events
from server.config.auth import AuthConfig
from server.config.sse import SSEConfig
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.agent import AgentConversation, AgentMessage
from server.model.index_sync import IndexSyncIntent
from server.model.video import Series, Video, VideoChunk, VideoChunkEmbedding
from server.repository.agent import AgentRepository
from server.repository.identity import IdentityRepository
from server.repository.library import LibraryRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.agent_chat import AgentChatService
from server.service.auth import AuthService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore


class AvailableRedis:
    """Protocol stub: real Lua budgets and single-use OTP have separate Redis tests."""

    def ping(self):
        return True

    def eval(self, *_arguments):
        return [1, 60]

    def zrem(self, *_arguments):
        return 1


class CapturingEmail:
    def __init__(self):
        self.messages = []

    def send_code(self, email, code, ttl_seconds):
        self.messages.append((email, code, ttl_seconds))


@dataclass
class ModelRuntime:
    model_name: str = "isolated-test-model"
    calls: list = field(default_factory=list)

    def kick(self, inputs, **options):
        self.calls.append((inputs, options))
        return VideoChunkSearchResult(
            query=inputs["user_message"], series_ids=list(inputs["series_ids"]),
            searched_video_ids=[], hits=[],
            agent_reply=AgentReply(message="测试课程检索完成。", result_count=0),
        )

    def close(self):
        pass


@pytest.fixture
def cloud_api(tmp_path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cloud-access.sqlite'}", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    sessions = create_session_factory(engine)
    store = LocalArtifactStore(tmp_path / "artifacts", environment=DatabaseEnvironment.TEST)
    library = LibraryService(LibraryRepository(sessions), store)
    sender = CapturingEmail()
    auth = AuthService(
        IdentityRepository(sessions),
        AuthConfig(mode="cloud", secret_key=uuid4().hex, cookie_secure=False),
        redis=AvailableRedis(), email_sender=sender,
    )
    auth.bootstrap()
    runtime = ModelRuntime()
    agent = AgentChatService(AgentRepository(sessions), library, runtime)
    services = SimpleNamespace(
        auth=auth, library=library, agent=agent,
        media=MediaService(library, store, probe_playback=lambda _path: True),
        uploads=SimpleNamespace(parse_defaults=None), jobs=SimpleNamespace(),
        retrieval=SimpleNamespace(search=lambda *args, **kwargs: None),
    )
    app = create_app(services)
    try:
        yield SimpleNamespace(app=app, services=services, sessions=sessions, store=store, sender=sender, runtime=runtime)
    finally:
        agent.close()
        engine.dispose()


def _guest(client):
    response = client.post("/api/auth/guest")
    assert response.status_code == 200, response.text
    return response.json()


def _login(api, client, email):
    guest = _guest(client)
    headers = {"X-CSRF-Token": guest["csrfToken"]}
    sent = client.post("/api/auth/email/send", json={"email": email}, headers=headers)
    assert sent.status_code == 200, sent.text
    code = api.sender.messages[-1][1]
    verified = client.post("/api/auth/email/verify", json={"email": email, "code": code}, headers=headers)
    assert verified.status_code == 200, verified.text
    return verified.json()


def _conversation(client, identity, tenant_id=None, key=None):
    response = client.post(
        "/api/agent/conversations/create", json={"tenantId": tenant_id or identity["publicTenantId"]},
        headers={"X-CSRF-Token": identity["csrfToken"], "Idempotency-Key": key or str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()["conversationId"]


def _course(api):
    tenant_id = api.services.auth.config.public_tenant_id
    series = api.services.library.create_series(tenant_id=tenant_id, name=f"公开测试课程 {uuid4()}")
    video_id, chunk_id = str(uuid4()), str(uuid4())
    source = api.store.base_directory / f"{video_id}.mp4"
    source.write_bytes(b"isolated-test-playable-media")
    with api.sessions.begin() as session:
        row = session.get(Series, series.series_id)
        row.status, row.is_published = "READY", True
        session.add(Video(
            video_id=video_id, series_id=series.series_id, episode_order=1, upload_sequence=1,
            original_filename="course.mp4", original_source_uri=api.store.uri_for_path(source),
            content_hash="a" * 64, total_bytes=source.stat().st_size, duration_seconds=10000,
            upload_status="COMPLETED", parse_status="COMPLETED", upload_progress=100, parse_progress=100,
        ))
        session.flush()
        session.add(VideoChunk(
            video_chunk_id=chunk_id, video_id=video_id, sequence_no=0,
            start_seconds=0, end_seconds=10000, analysis_window_start_seconds=0, analysis_window_end_seconds=10000,
            chunk_media_ref=api.store.uri_for_path(source), analysis_window_media_ref=api.store.uri_for_path(source),
        ))
        session.flush()
        session.add(VideoChunkEmbedding(video_chunk_id=chunk_id, text_input="测试课程片段", text_vector=[1.0], vision_vector=[1.0]))
        session.add(IndexSyncIntent(video_chunk_id=chunk_id, operation="UPSERT", source_revision="a" * 64, status="SYNCED"))
    state = api.services.library.get_series(series.series_id, tenant_id=tenant_id)
    assert api.services.media.publication_ready(state)
    return series.series_id, video_id


@pytest.mark.parametrize("path,params", [
    ("/api/series/list", {"tenantId": "platform-public"}),
    ("/api/agent/conversations/list", {"tenantId": "platform-public"}),
    ("/api/agent/global-settings", {}),
])
def test_cloud_business_reads_require_authentication(cloud_api, path, params):
    with TestClient(cloud_api.app) as client:
        response = client.get(path, params=params)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_cloud_write_requires_csrf_before_creating_conversation(cloud_api):
    with TestClient(cloud_api.app) as client:
        guest = _guest(client)
        response = client.post(
            "/api/agent/conversations/create", json={"tenantId": guest["publicTenantId"]},
            headers={"Idempotency-Key": "missing-csrf"},
        )
    assert response.status_code == 403 and response.json()["code"] == "CSRF_INVALID"
    with cloud_api.sessions() as session:
        assert list(session.scalars(select(AgentConversation))) == []


def test_guests_share_public_courses_but_conversations_are_owned(cloud_api):
    with TestClient(cloud_api.app) as first, TestClient(cloud_api.app) as second:
        alice, bob = _guest(first), _guest(second)
        conversation_id = _conversation(first, alice, key="same-browser-key")
        bob_id = _conversation(second, bob, key="same-browser-key")
        assert conversation_id != bob_id
        params = {"tenantId": "platform-public", "conversationId": conversation_id}
        assert second.get("/api/agent/conversations/get", params=params).status_code == 404
        assert second.get("/api/agent/messages/list", params=params).status_code == 404
        assert second.get("/api/agent/messages/events", params=params).status_code == 404
        denied = second.post("/api/agent/conversations/delete", params=params, headers={"X-CSRF-Token": bob["csrfToken"]})
        assert denied.status_code == 404
        own = second.get("/api/agent/conversations/list", params={"tenantId": "platform-public"}).json()
        assert [item["conversationId"] for item in own["items"]] == [bob_id]
        assert first.get("/api/agent/conversations/get", params=params).status_code == 200


def test_guest_cannot_read_another_tenant_or_use_management(cloud_api):
    with TestClient(cloud_api.app) as client:
        guest = _guest(client)
        assert client.get("/api/series/list", params={"tenantId": "private-tenant"}).status_code == 404
        assert client.get("/api/agent/global-settings").status_code == 403
        assert client.get("/api/uploads/parse-config").status_code == 403
        assert client.get("/api/uploads/list", params={"tenantId": "platform-public"}).status_code == 403
        upload = client.post(
            "/api/uploads/create", params={"seriesId": str(uuid4())},
            headers={"Idempotency-Key": "guest-upload", "X-CSRF-Token": guest["csrfToken"]},
            json={"tenantId": "platform-public", "episodeOrder": 1, "originalFilename": "course.mp4", "mediaType": "video/mp4", "totalSizeBytes": 4},
        )
        assert upload.status_code == 403


def test_registered_user_cannot_read_or_write_another_personal_tenant(cloud_api):
    with TestClient(cloud_api.app) as first, TestClient(cloud_api.app) as second:
        alice = _login(cloud_api, first, "alice@example.com")
        bob = _login(cloud_api, second, "bob@example.com")
        created = first.post(
            "/api/series/create", headers={"X-CSRF-Token": alice["csrfToken"]},
            json={"tenantId": alice["tenantId"], "name": "Alice private course"},
        )
        assert created.status_code == 201, created.text
        series_id = created.json()["seriesId"]
        assert second.get("/api/series/list", params={"tenantId": alice["tenantId"]}).status_code == 404
        assert second.get("/api/series/get", params={"tenantId": alice["tenantId"], "seriesId": series_id}).status_code == 404
        denied = second.post(
            "/api/series/update", headers={"X-CSRF-Token": bob["csrfToken"]},
            json={"tenantId": alice["tenantId"], "seriesId": series_id, "description": "overwritten"},
        )
        assert denied.status_code == 404
        assert first.get("/api/series/get", params={"tenantId": alice["tenantId"], "seriesId": series_id}).status_code == 200


def test_logout_invalidates_old_token_for_business_routes(cloud_api):
    with TestClient(cloud_api.app) as client, TestClient(cloud_api.app) as replay:
        account = _login(cloud_api, client, "logout-access@example.com")
        replay.cookies.set(cloud_api.services.auth.config.cookie_name, client.cookies.get(cloud_api.services.auth.config.cookie_name))
        result = client.post("/api/auth/logout", headers={"X-CSRF-Token": account["csrfToken"]})
        assert result.status_code == 200
        old = replay.get("/api/series/list", params={"tenantId": account["tenantId"]})
        assert old.status_code == 401 and old.json()["code"] == "AUTH_EXPIRED"


def test_withdrawing_public_course_removes_detail_media_and_query_access(cloud_api):
    series_id, video_id = _course(cloud_api)
    with TestClient(cloud_api.app) as client:
        guest = _guest(client)
        scope = {"tenantId": "platform-public", "seriesId": series_id}
        assert client.get("/api/series/get", params=scope).status_code == 200
        assert client.get("/api/videos/content", params={"tenantId": "platform-public", "videoId": video_id, "variant": "playback"}).status_code == 200
        conversation_id = _conversation(client, guest)
        with cloud_api.sessions.begin() as session:
            session.get(Series, series_id).is_published = False
        assert client.get("/api/series/get", params=scope).status_code == 404
        assert client.get("/api/videos/content", params={"tenantId": "platform-public", "videoId": video_id, "variant": "playback"}).status_code == 404
        listed = client.get("/api/series/list", params={"tenantId": "platform-public"})
        assert listed.status_code == 200 and listed.json()["items"] == []
        denied = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "withdrawn-course", "X-CSRF-Token": guest["csrfToken"]},
            json={"tenantId": "platform-public", "content": "找课程片段", "seriesIds": [series_id]},
        )
        assert denied.status_code == 404 and not cloud_api.runtime.calls


def test_private_conversation_can_use_public_source_without_changing_owner(cloud_api):
    series_id, _ = _course(cloud_api)
    with TestClient(cloud_api.app) as client, TestClient(cloud_api.app) as other:
        account = _login(cloud_api, client, "source-owner@example.com")
        outsider = _login(cloud_api, other, "source-outsider@example.com")
        conversation_id = _conversation(client, account, tenant_id=account["tenantId"])
        payload = {"tenantId": account["tenantId"], "sourceTenantId": "platform-public", "seriesIds": [series_id], "content": "查询公开课程"}
        sent = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id}, json=payload,
            headers={"Idempotency-Key": "public-source", "X-CSRF-Token": account["csrfToken"]},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["assistantMessage"]["status"] == "COMPLETED"
        assert cloud_api.runtime.calls[-1][0]["tenant_id"] == "platform-public"
        with cloud_api.sessions() as session:
            conversation = session.get(AgentConversation, conversation_id)
            assert conversation.tenant_id == account["tenantId"] and conversation.owner_user_id == account["userId"]
            messages = list(session.scalars(select(AgentMessage).where(AgentMessage.conversation_id == conversation_id)))
            assert len(messages) == 2 and all(message.detail.get("sourceTenantId") == "platform-public" for message in messages)
        assert client.get("/api/agent/messages/list", params={"conversationId": conversation_id, "tenantId": account["tenantId"]}).status_code == 200
        denied = other.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id}, json=payload,
            headers={"Idempotency-Key": "public-source-other-owner", "X-CSRF-Token": outsider["csrfToken"]},
        )
        assert denied.status_code == 404


def test_guest_model_call_limits_are_capped_by_server(cloud_api):
    series_id, _ = _course(cloud_api)
    with TestClient(cloud_api.app) as client:
        guest = _guest(client)
        conversation_id = _conversation(client, guest)
        response = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "guest-limit-cap", "X-CSRF-Token": guest["csrfToken"]},
            json={"tenantId": "platform-public", "content": "找课程片段", "seriesIds": [series_id], "maxModelCallsPerTurn": 999, "maxToolCallsPerTurn": 999},
        )
        assert response.status_code == 200, response.text
        assert cloud_api.runtime.calls[-1][1]["max_model_calls_per_turn"] == cloud_api.services.auth.config.guest_max_model_calls
        assert cloud_api.runtime.calls[-1][1]["max_tool_calls_per_turn"] == cloud_api.services.auth.config.guest_max_model_calls * 2


def test_open_message_stream_revalidates_revoked_token_before_next_snapshot(cloud_api):
    credentials = cloud_api.services.auth.create_guest("192.0.2.30")
    conversation = cloud_api.services.agent.create_conversation(
        tenant_id="platform-public", owner_user_id=credentials.identity.user_id,
        idempotency_key="stream-auth-revalidation",
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "GET", "path": "/api/agent/messages/events",
        "scheme": "http", "server": ("testserver", 80), "client": ("192.0.2.30", 10000),
        "query_string": b"", "app": cloud_api.app,
        "headers": [(b"cookie", f"{cloud_api.services.auth.config.cookie_name}={credentials.token}".encode())],
    }, receive=receive)

    async def consume():
        stream = _agent_message_events(
            request, cloud_api.services.agent, conversation.conversation_id, "platform-public",
            SSEConfig(poll_interval_seconds=0.01), media=cloud_api.services.media,
        )
        try:
            assert (await stream.__anext__()).startswith("event: messages\n")
            cloud_api.services.auth.repository.revoke(credentials.identity.user_id, credentials.identity.auth_version)
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(stream.__anext__(), timeout=1)
        finally:
            await stream.aclose()

    asyncio.run(consume())
