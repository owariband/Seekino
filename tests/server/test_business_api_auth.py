from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from server.api import jobs as jobs_api, uploads as uploads_api
from server.api.errors import register_exception_handlers
from server.config.sse import SSEConfig
from server.model.dto.authDTO import AuthIdentity
from server.model.dto.parseUsageDTO import UsageOverviewState, UsageTotals
from server.model.dto.uploadDTO import UploadSessionCreateRequest
from server.router.jobs import router as jobs_router
from server.router.uploads import router as uploads_router
from server.error.auth import AuthError


def _identity(*, guest=False, role="user", mode="cloud"):
    return AuthIdentity(
        user_id="verified-user", email=None if guest else "user@example.org",
        is_guest=guest, tenant_id=None if guest else "personal-tenant",
        public_tenant_id="public-tenant", role=role, auth_version=1,
        auth_level="local" if mode == "local" else "guest" if guest else "email",
        mode=mode,
    )


class _Auth:
    config = SimpleNamespace(cookie_name="session")

    def __init__(self, identity):
        self.identity = identity
        self.calls = 0
        self.revoked = False

    def authenticate(self, token):
        assert token == "test-token"
        self.calls += 1
        if self.revoked:
            raise AuthError("身份已失效", status_code=401, code="AUTH_REQUIRED")
        return self.identity

    def verify_csrf(self, token, csrf):
        if csrf != "trusted-csrf":
            raise AuthError("请求校验失败", status_code=403, code="CSRF_FAILED")


class _ForbiddenBusiness:
    def __getattr__(self, name):
        raise AssertionError(f"unauthorized request reached business method {name}")


def _app(identity, *, uploads=None, jobs=None, usage=None):
    auth = _Auth(identity)
    app = FastAPI()
    app.state.services = SimpleNamespace(
        auth=auth, uploads=uploads or _ForbiddenBusiness(),
        jobs=jobs or _ForbiddenBusiness(), parse_usage=usage or _ForbiddenBusiness(),
    )
    app.state.sse_config = SSEConfig(poll_interval_seconds=0.001)
    register_exception_handlers(app)
    app.include_router(uploads_router)
    app.include_router(jobs_router)
    return app, auth


def _request(app, *, method="POST"):
    return Request({
        "type": "http", "app": app, "method": method, "path": "/",
        "headers": [(b"cookie", b"session=test-token"), (b"x-csrf-token", b"trusted-csrf")],
    })


_UPLOAD_BODY = {
    "tenantId": "public-tenant", "episodeOrder": 1,
    "originalFilename": "course.mp4", "mediaType": "video/mp4", "totalSizeBytes": 4,
}
_ROUTES = [
    ("GET", "/api/uploads/list", {}),
    ("GET", "/api/uploads/get", {"uploadSessionId": "upload"}),
    ("POST", "/api/uploads/create", {"seriesId": "series"}),
    ("POST", "/api/uploads/delete", {"uploadSessionId": "upload"}),
    ("POST", "/api/uploads/retry-parse", {"uploadSessionId": "upload", "expectedJobVersion": 1, "expectedLeaseVersion": 0}),
    ("POST", "/api/uploads/reparse", {"uploadSessionId": "upload", "expectedJobVersion": 1}),
    ("PUT", "/api/uploads/segments/upload", {"uploadSessionId": "upload", "segmentNumber": 0}),
    ("POST", "/api/uploads/complete", {"uploadSessionId": "upload"}),
    ("GET", "/api/jobs/video-parse/get", {"videoParseTaskId": "task"}),
    ("GET", "/api/jobs/video-parse/list", {}),
    ("GET", "/api/jobs/video-parse/events", {"videoParseTaskId": "task"}),
]


@pytest.mark.parametrize("method,path,params", _ROUTES)
@pytest.mark.parametrize("guest,tenant,status,code", [
    (True, "public-tenant", 403, "LOGIN_REQUIRED"),
    (False, "public-tenant", 404, "NOT_FOUND"),
    (False, "another-tenant", 404, "NOT_FOUND"),
])
def test_upload_and_job_routes_check_write_scope_before_business(
    method, path, params, guest, tenant, status, code,
):
    app, _ = _app(_identity(guest=guest))
    with TestClient(app) as client:
        client.cookies.set("session", "test-token")
        response = client.request(
            method, path, params={"tenantId": tenant, **params},
            json={**_UPLOAD_BODY, "tenantId": tenant} if path.endswith("/create") else None,
            headers={
                "X-CSRF-Token": "trusted-csrf", "Idempotency-Key": "request-key",
                "Content-Range": "bytes 0-3/4", "X-Content-SHA256": "a" * 64,
            },
        )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


@pytest.mark.parametrize("role,mode,tenant", [
    ("user", "cloud", "personal-tenant"),
    ("admin", "cloud", "public-tenant"),
    ("user", "local", "arbitrary-local-scope"),
])
def test_upload_and_task_lists_allow_own_tenant_admin_public_and_local(role, mode, tenant):
    uploads = SimpleNamespace(list_sessions=lambda **_: [])
    jobs = SimpleNamespace(list_video_parse_tasks=lambda **_: [], count_video_parse_tasks=lambda **_: 0)
    app, _ = _app(_identity(role=role, mode=mode), uploads=uploads, jobs=jobs)
    with TestClient(app) as client:
        client.cookies.set("session", "test-token")
        for path in ("/api/uploads/list", "/api/jobs/video-parse/list"):
            response = client.get(path, params={"tenantId": tenant})
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []


def test_upload_mutation_requires_csrf_and_guest_cannot_read_parse_defaults():
    app, _ = _app(_identity())
    with TestClient(app) as client:
        client.cookies.set("session", "test-token")
        response = client.post(
            "/api/uploads/create", params={"seriesId": "series"},
            json={**_UPLOAD_BODY, "tenantId": "personal-tenant"},
            headers={"Idempotency-Key": "request-key"},
        )
    assert response.status_code == 403 and response.json()["code"] == "CSRF_FAILED"
    guest_app, _ = _app(_identity(guest=True))
    with TestClient(guest_app) as client:
        client.cookies.set("session", "test-token")
        response = client.get("/api/uploads/parse-config")
    assert response.status_code == 403 and response.json()["code"] == "LOGIN_REQUIRED"


def test_segment_denial_does_not_read_request_body():
    app, _ = _app(_identity(guest=True))
    request = _request(app, method="PUT")

    async def unexpected_body():
        raise AssertionError("unauthorized segment body was consumed")

    request._receive = unexpected_body
    with pytest.raises(AuthError) as error:
        asyncio.run(uploads_api.upload_segment(
            request, upload_session_id="upload", segment_number=0,
            content_range="bytes 0-3/4", checksum_sha256="a" * 64,
            tenant_id="public-tenant",
        ))
    assert error.value.code == "LOGIN_REQUIRED"


def test_upload_creation_and_reexecution_take_actor_from_identity(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(("create", kwargs))
        return SimpleNamespace(upload_session_id="upload")

    def execute(name, *args, **kwargs):
        calls.append((name, kwargs))
        return "task"

    service = SimpleNamespace(
        create_session=create, get_detail=lambda *_, **__: "detail",
        retry_parse=lambda *args, **kwargs: execute("retry", *args, **kwargs),
        reparse=lambda *args, **kwargs: execute("reparse", *args, **kwargs),
    )
    app, _ = _app(_identity(), uploads=service)
    monkeypatch.setattr(uploads_api, "_upload_view", lambda detail: detail)
    monkeypatch.setattr(uploads_api, "project_video_parse_task", lambda task: task)
    body = UploadSessionCreateRequest.model_validate({**_UPLOAD_BODY, "tenantId": "personal-tenant"})
    assert uploads_api.create_upload_session(body, _request(app), series_id="series", idempotency_key="key") == "detail"
    assert uploads_api.retry_upload_parse(_request(app), upload_session_id="upload", tenant_id="personal-tenant", expected_job_version=1, expected_lease_version=0) == "task"
    assert uploads_api.reparse_upload(_request(app), upload_session_id="upload", tenant_id="personal-tenant", expected_job_version=1) == "task"
    assert calls[0][1]["created_by_user_id"] == "verified-user"
    assert all(kwargs["actor_user_id"] == "verified-user" for _, kwargs in calls[1:])


@pytest.mark.parametrize("mode,expected_actor", [("cloud", "verified-user"), ("local", None)])
def test_usage_filters_public_tenant_to_authenticated_actor(mode, expected_actor):
    calls = []
    totals = UsageTotals(0, 0, 0, 0, 0, 0, 0, 0, 0)

    def overview(**kwargs):
        calls.append(kwargs)
        return UsageOverviewState("available", None, 0, 1, 1, False, totals, (), ())

    app, _ = _app(_identity(mode=mode), usage=SimpleNamespace(overview=overview))
    with TestClient(app) as client:
        client.cookies.set("session", "test-token")
        response = client.get("/api/usage/overview", params={"tenantId": "public-tenant", "actorUserId": "forged-user"})
    assert response.status_code == 200, response.text
    assert calls[0]["actor_user_id"] == expected_actor
    assert calls[0]["tenant_id"] == "public-tenant"


@pytest.mark.parametrize("guest,tenant,status,code", [
    (True, "public-tenant", 403, "LOGIN_REQUIRED"),
    (False, "another-tenant", 404, "NOT_FOUND"),
])
def test_usage_denies_guests_and_other_tenants(guest, tenant, status, code):
    app, _ = _app(_identity(guest=guest))
    with TestClient(app) as client:
        client.cookies.set("session", "test-token")
        response = client.get("/api/usage/overview", params={"tenantId": tenant})
    assert response.status_code == status and response.json()["code"] == code


@pytest.mark.parametrize("revocation", ["token", "tenant"])
def test_processing_sse_stops_before_fetching_after_access_revocation(monkeypatch, revocation):
    app, auth = _app(_identity())
    request = _request(app, method="GET")

    async def connected():
        return False

    request.is_disconnected = connected
    view = SimpleNamespace(
        version=1, video_parse_task_id="task",
        model_dump_json=lambda **_: '{"status":"PARSING"}',
    )
    monkeypatch.setattr(jobs_api, "project_video_parse_task", lambda _: view)
    initial = SimpleNamespace(task=SimpleNamespace(status="PARSING"))
    generator = jobs_api._video_parse_task_events

    async def collect():
        events = generator(request, _ForbiddenBusiness(), "id", "personal-tenant", initial, app.state.sse_config)
        first = await anext(events)
        if revocation == "token":
            auth.revoked = True
        else:
            auth.identity = replace(auth.identity, tenant_id="replacement-tenant")
        assert [event async for event in events] == []
        return first

    assert asyncio.run(collect()).startswith("event:")
    assert auth.calls == 2
