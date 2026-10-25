from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from server.config.auth import AuthConfig
from server.caller.redis import create_redis_client, close_redis_client
from server.config.email import SMTPConfig
from server.constants.identity import LOCAL_USER_ID
from server.database import Base
from server.model.agent import AgentConversation
from server.model.tenant import Tenant
from server.model.user import User
from server.repository.identity import IdentityRepository, default_public_nickname
from server.router.auth import router
from server.error.auth import AuthError, IdentityDeniedError
from server.utils.email import normalize_email
from server.service.auth import AuthService, _VERIFY_CODE


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int]] = []

    def send_code(self, email: str, code: str, ttl_seconds: int) -> None:
        self.messages.append((email, code, ttl_seconds))


class StubRedis:
    """Protocol stub for token/repository tests; Lua is tested against Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, tuple]] = []
        self.results: list[list[int]] = []
        self.closed = False

    def ping(self) -> bool:
        return True

    def eval(self, script: str, key_count: int, *values):
        self.calls.append((script, key_count, values))
        return self.results.pop(0) if self.results else [1, 60]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def identities(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__, Tenant.__table__, AgentConversation.__table__])
    sessions = sessionmaker(engine, expire_on_commit=False)
    yield IdentityRepository(sessions), sessions
    engine.dispose()


@pytest.fixture
def cloud(identities):
    repository, sessions = identities
    redis = StubRedis()
    sender = CapturingEmailSender()
    service = AuthService(repository, AuthConfig(mode="cloud", secret_key="unit-test-auth-secret-32-bytes-or-longer", cookie_secure=False), redis=redis, email_sender=sender)
    service.bootstrap()
    return service, redis, sender, sessions


def _app(service: AuthService) -> FastAPI:
    app = FastAPI()
    app.state.services = SimpleNamespace(auth=service)
    app.include_router(router)

    @app.exception_handler(AuthError)
    def auth_error(_, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": str(exc)}, headers={"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None)

    return app


def _conversation(sessions, owner_id: str, tenant_id: str) -> str:
    conversation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(AgentConversation(id=conversation_id, owner_id=owner_id, tenant_id=tenant_id, title="测试对话", idempotency_key=conversation_id))
    return conversation_id


def _signed(service: AuthService, payload: dict, header: dict | None = None) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    data = f"{encode(header or {'alg': 'HS256', 'typ': 'JWT'})}.{encode(payload)}"
    signature = base64.urlsafe_b64encode(hmac.digest(service.config.secret_key.encode(), data.encode(), "sha256")).rstrip(b"=").decode()
    return f"{data}.{signature}"


def test_configuration_has_explicit_cloud_requirements(identities, monkeypatch):
    with pytest.raises(ValueError, match="secret"):
        AuthConfig(mode="cloud")
    repository, _ = identities
    config = AuthConfig(mode="cloud", secret_key="x" * 32)
    with pytest.raises(ValueError, match="caller-provided Redis client"):
        AuthService(repository, config, email_sender=CapturingEmailSender())
    borrowed_redis = StubRedis()
    with pytest.raises(ValueError, match="SMTP"):
        AuthService(repository, config, redis=borrowed_redis)
    assert not borrowed_redis.closed
    monkeypatch.setenv("ANIVAG_AUTH_MODE", "cloud")
    monkeypatch.setenv("ANIVAG_AUTH_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("ANIVAG_AUTH_REDIS_URL", "redis://127.0.0.1:6379/15")
    monkeypatch.setenv("ANIVAG_AUTH_OTP_ATTEMPT_LIMIT", "3")
    monkeypatch.setenv("ANIVAG_AUTH_QUERY_LIMIT_GUEST", "5")
    monkeypatch.setenv("ANIVAG_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("ANIVAG_PUBLIC_LIBRARY_ONLY", "true")
    parsed = AuthConfig.from_environment()
    assert parsed.otp_attempt_limit == 3 and not parsed.cookie_secure
    assert parsed.query_limit_guest == 5 and parsed.concurrent_queries_user == 3
    assert parsed.public_library_only
    assert "x" * 32 not in repr(parsed)
    with pytest.raises(ValueError):
        SMTPConfig(host="smtp.example.com", sender="bad\r\nheader")


def test_redis_timeout_loads_from_environment_and_requires_positive_integer(monkeypatch):
    assert AuthConfig().redis_timeout_seconds == 5
    monkeypatch.setenv("ANIVAG_AUTH_REDIS_TIMEOUT_SECONDS", "12")
    assert AuthConfig.from_environment().redis_timeout_seconds == 12
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="redis_timeout_seconds must be a positive integer"):
            AuthConfig(redis_timeout_seconds=invalid)
    monkeypatch.setenv("ANIVAG_AUTH_REDIS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="redis_timeout_seconds must be a positive integer"):
        AuthConfig.from_environment()


def test_local_mode_bootstraps_fixed_identity_and_tenants(identities):
    repository, sessions = identities
    service = AuthService(repository, AuthConfig())
    service.bootstrap()
    service.bootstrap()
    identity = service.authenticate(None)
    assert (identity.user_id, identity.tenant_id, identity.auth_level, identity.mode) == (LOCAL_USER_ID, "local-default", "local", "local")
    assert service.permissions(identity).model_dump() == {"can_upload": True, "can_manage": True, "can_configure": True}
    with sessions() as session:
        tenants = list(session.scalars(select(Tenant)))
        assert {(tenant.id, tenant.owner_id) for tenant in tenants} == {("platform-public", None), ("local-default", LOCAL_USER_ID)}
    with TestClient(_app(service)) as client:
        result = client.get("/api/auth/me")
    assert result.status_code == 200 and result.json()["csrfToken"]
    assert result.json()["permissions"]["canConfigure"] is True


def test_cloud_guest_is_stable_and_has_no_personal_tenant(cloud):
    service, _, _, sessions = cloud
    credentials = service.create_guest("192.0.2.1")
    assert service.authenticate(credentials.token) == credentials.identity
    assert credentials.identity.is_guest and credentials.identity.tenant_id is None
    assert credentials.identity.public_tenant_id == "platform-public"
    assert not any(service.permissions(credentials.identity).model_dump().values())
    with sessions() as session:
        assert list(session.scalars(select(Tenant).where(Tenant.owner_id == credentials.identity.user_id))) == []
    assert not service.redis.closed


def test_tokens_expire_and_recheck_database_state(cloud, monkeypatch):
    service, _, _, sessions = cloud
    credentials = service.create_guest()
    with pytest.raises(AuthError):
        service.authenticate(credentials.token[:-1] + ("a" if credentials.token[-1] != "a" else "b"))
    monkeypatch.setattr("server.service.auth.time.time", lambda: credentials.expires_at)
    with pytest.raises(AuthError, match="失效"):
        service.authenticate(credentials.token)
    monkeypatch.undo()
    with sessions.begin() as session:
        session.execute(update(User).where(User.id == credentials.identity.user_id).values(auth_version=2))
    with pytest.raises(AuthError):
        service.authenticate(credentials.token)
    fresh = service.issue_credentials(replace(credentials.identity, auth_version=2))
    with sessions.begin() as session:
        session.execute(update(User).where(User.id == credentials.identity.user_id).values(status="DISABLED"))
    with pytest.raises(AuthError):
        service.authenticate(fresh.token)


@pytest.mark.parametrize("changes,header", [({"auth_version": True}, None), ({"exp": "9999999999"}, None), ({"auth_level": "local"}, None), ({"sub": []}, None), ({}, {"alg": "none", "typ": "JWT"})])
def test_invalid_signed_claims_are_rejected(cloud, changes, header):
    service, _, _, _ = cloud
    credentials = service.create_guest()
    payload = {"sub": credentials.identity.user_id, "exp": int(time.time()) + 300, "auth_version": 1, "auth_level": "guest"} | changes
    with pytest.raises(AuthError):
        service.authenticate(_signed(service, payload, header))


def test_email_upgrade_keeps_user_and_conversations_but_revokes_guest(cloud):
    service, _, _, sessions = cloud
    guest = service.create_guest()
    conversation_id = _conversation(sessions, guest.identity.user_id, "platform-public")
    signed_in = service.verify_email_code(guest.identity, " Test@Example.com ", "123456", "192.0.2.2")
    assert signed_in.identity.user_id == guest.identity.user_id
    assert not signed_in.identity.is_guest and signed_in.identity.email == "test@example.com"
    assert UUID(signed_in.identity.tenant_id).version == 4
    assert signed_in.identity.tenant_id != signed_in.identity.user_id
    assert service.authenticate(signed_in.token) == signed_in.identity
    with pytest.raises(AuthError):
        service.authenticate(guest.token)
    with sessions() as session:
        conversation = session.get(AgentConversation, conversation_id)
        assert conversation.owner_id == signed_in.identity.user_id and conversation.tenant_id == "platform-public"


def test_guest_profile_defaults_and_successful_login_record(cloud, monkeypatch):
    import server.repository.identity as repository_module

    service, _, _, sessions = cloud
    guest = service.create_guest()
    with sessions() as session:
        user = session.get(User, guest.identity.user_id)
        assert user.nickname == "游客"
        assert (user.language, user.color_schema, user.timezone) == ("Chinese", "Bright", "Asia/Shanghai")
        assert user.is_anonymous and not user.is_superuser
        assert user.avatar is user.last_login_time is user.login_channel is None
        public = session.get(Tenant, "platform-public")
        assert public.description is public.avatar is None

    instant = datetime(2026, 10, 10, 4, 30, 0, 123_000, tzinfo=UTC)
    monkeypatch.setattr(repository_module, "now_utc", lambda: instant)
    account = service.verify_email_code(guest.identity, "profile@example.com", "123456")
    assert service.authenticate(account.token) == account.identity
    with sessions() as session:
        user = session.get(User, account.identity.user_id)
        assert user.nickname == default_public_nickname(user.id)
        assert user.nickname.startswith("用户-")
        assert "profile" not in user.nickname and "@" not in user.nickname
        assert not user.is_anonymous
        assert user.last_login_time == instant and user.login_channel == "email"

    service.logout(account.identity)
    with sessions() as session:
        assert session.get(User, account.identity.user_id).last_login_time == instant


def test_invalid_email_code_does_not_record_a_login(cloud):
    service, redis, _, sessions = cloud
    guest = service.create_guest()
    redis.results = [[0, 0]]
    with pytest.raises(AuthError, match="验证码错误"):
        service.verify_email_code(guest.identity, "failed@example.com", "123456")
    with sessions() as session:
        user = session.get(User, guest.identity.user_id)
        assert user.is_anonymous
        assert user.last_login_time is user.login_channel is None


def test_existing_account_login_preserves_profile(cloud, monkeypatch):
    import server.repository.identity as repository_module

    service, _, _, sessions = cloud
    first = service.create_guest()
    account = service.verify_email_code(first.identity, "existing@example.com", "123456")
    with sessions.begin() as session:
        user = session.get(User, account.identity.user_id)
        user.nickname, user.avatar = "我的昵称", "base64-content"
        user.language, user.color_schema, user.timezone = "English", "Dark", "UTC"
    instant = datetime(2026, 10, 10, 5, 0, 0, 456_000, tzinfo=UTC)
    monkeypatch.setattr(repository_module, "now_utc", lambda: instant)
    guest = service.create_guest()
    signed_in = service.verify_email_code(guest.identity, "existing@example.com", "123456")
    assert signed_in.identity.user_id == account.identity.user_id
    with sessions() as session:
        user = session.get(User, account.identity.user_id)
        assert (user.nickname, user.avatar) == ("我的昵称", "base64-content")
        assert (user.language, user.color_schema, user.timezone) == ("English", "Dark", "UTC")
        assert user.last_login_time == instant and user.login_channel == "email"
        assert session.get(User, guest.identity.user_id).last_login_time is None


def test_existing_email_migrates_only_current_guest_conversations(cloud):
    service, _, _, sessions = cloud
    first = service.create_guest()
    account = service.verify_email_code(first.identity, "owner@example.com", "123456")
    guest = service.create_guest()
    other_guest = service.create_guest()
    guest_conversation = _conversation(sessions, guest.identity.user_id, "platform-public")
    other_conversation = _conversation(sessions, other_guest.identity.user_id, "platform-public")
    own_conversation = _conversation(sessions, account.identity.user_id, account.identity.tenant_id)
    signed_in = service.verify_email_code(guest.identity, "owner@example.com", "123456")
    assert signed_in.identity.user_id == account.identity.user_id
    assert signed_in.identity.tenant_id == account.identity.tenant_id
    with sessions() as session:
        assert session.get(AgentConversation, guest_conversation).owner_id == account.identity.user_id
        assert session.get(AgentConversation, guest_conversation).tenant_id == "platform-public"
        assert session.get(AgentConversation, other_conversation).owner_id == other_guest.identity.user_id
        assert session.get(AgentConversation, own_conversation).tenant_id == account.identity.tenant_id
        assert session.get(User, guest.identity.user_id).status == "DISABLED"
    with pytest.raises(AuthError):
        service.authenticate(guest.token)
    assert service.authenticate(other_guest.token).is_guest
    assert service.authenticate(account.token).user_id == account.identity.user_id


def test_existing_account_login_preserves_both_conversations_when_request_keys_collide(cloud):
    service, _, _, sessions = cloud
    first = service.create_guest()
    account = service.verify_email_code(first.identity, "shared-key@example.com", "123456")
    old_conversation = _conversation(sessions, account.identity.user_id, "platform-public")
    guest = service.create_guest()
    guest_conversation = _conversation(sessions, guest.identity.user_id, "platform-public")
    with sessions.begin() as session:
        for conversation_id in (old_conversation, guest_conversation):
            session.get(AgentConversation, conversation_id).idempotency_key = "same-create-key"
    signed = service.verify_email_code(guest.identity, "shared-key@example.com", "123456")
    assert signed.identity.user_id == account.identity.user_id
    with sessions() as session:
        original = session.get(AgentConversation, old_conversation)
        transferred = session.get(AgentConversation, guest_conversation)
        assert original.owner_id == transferred.owner_id == account.identity.user_id
        assert original.idempotency_key == "same-create-key"
        assert transferred.idempotency_key != original.idempotency_key
        assert original.tenant_id == transferred.tenant_id == "platform-public"
    with pytest.raises(AuthError):
        service.authenticate(guest.token)


def test_account_permissions_and_tenant_status_are_read_from_database(cloud):
    service, _, _, sessions = cloud
    guest = service.create_guest()
    account = service.verify_email_code(guest.identity, "permissions@example.com", "123456")
    with sessions.begin() as session:
        session.execute(update(User).where(User.id == account.identity.user_id).values(is_superuser=True))
    assert service.permissions(service.authenticate(account.token)).can_configure
    with sessions.begin() as session:
        session.execute(update(User).where(User.id == account.identity.user_id).values(is_superuser=False))
    assert not service.permissions(service.authenticate(account.token)).can_configure
    with sessions.begin() as session:
        session.execute(update(Tenant).where(Tenant.id == account.identity.tenant_id).values(status="DISABLED"))
    with pytest.raises(AuthError) as unavailable:
        service.authenticate(account.token)
    assert unavailable.value.code == "TENANT_UNAVAILABLE"


def test_repository_rejects_stale_upgrade_and_disabled_account(cloud):
    service, _, _, sessions = cloud
    guest = service.create_guest()
    with pytest.raises(IdentityDeniedError):
        service.repository.login_email(guest_id=guest.identity.user_id, expected_version=2, email="a@example.com")
    account = service.verify_email_code(guest.identity, "a@example.com", "123456")
    with sessions.begin() as session:
        session.execute(update(User).where(User.id == account.identity.user_id).values(status="DISABLED"))
    other = service.create_guest()
    with pytest.raises(AuthError):
        service.verify_email_code(other.identity, "a@example.com", "123456")
    assert service.repository.get_user(other.identity.user_id).is_anonymous


def test_email_challenge_stores_only_hmac_digest_and_hashed_keys(cloud):
    service, redis, sender, _ = cloud
    guest = service.create_guest()
    assert service.send_email_code(guest.identity, "USER@example.com", "192.0.2.3") == 60
    email, code, ttl = sender.messages[0]
    assert email == "user@example.com" and len(code) == 6 and code.isascii() and code.isdigit()
    assert ttl == 300
    _, key_count, values = redis.calls[-1]
    assert key_count == 5
    assert all("user@example.com" not in key and "192.0.2.3" not in key for key in values[:key_count])
    digest = values[key_count]
    assert len(digest) == 64 and digest != hashlib.sha256(code.encode()).hexdigest()
    assert code not in values
    other = service.create_guest()
    assert service._challenge_key(guest.identity, email) != service._challenge_key(other.identity, email)


def test_mail_failure_discards_code_and_hides_provider_error(cloud):
    service, redis, sender, _ = cloud
    guest = service.create_guest()

    def fail(email, code, ttl):
        raise RuntimeError(f"provider leaked {code} smtp-password")

    sender.send_code = fail
    with pytest.raises(AuthError) as result:
        service.send_email_code(guest.identity, "user@example.com")
    assert result.value.status_code == 503 and "smtp-password" not in str(result.value)
    assert redis.calls[-1][1] == 2 and "HGET" in redis.calls[-1][0]


def test_csrf_is_bound_to_current_token_and_logout_revokes(cloud):
    service, _, _, _ = cloud
    guest = service.create_guest()
    service.verify_csrf(guest.token, guest.csrf_token)
    other = service.create_guest()
    with pytest.raises(AuthError) as result:
        service.verify_csrf(other.token, guest.csrf_token)
    assert result.value.status_code == 403
    signed_in = service.verify_email_code(guest.identity, "logout@example.com", "123456")
    logged_out = service.logout(signed_in.identity)
    assert logged_out.identity.is_guest and logged_out.identity.user_id != signed_in.identity.user_id
    with pytest.raises(AuthError):
        service.authenticate(signed_in.token)


def test_failed_guest_reservation_preserves_account_on_logout(cloud):
    service, redis, _, _ = cloud
    guest = service.create_guest()
    account = service.verify_email_code(guest.identity, "limited-logout@example.com", "123456")
    redis.results = [[0, 120]]
    with pytest.raises(AuthError) as limited:
        service.logout(account.identity, "192.0.2.30")
    assert limited.value.status_code == 429
    assert service.authenticate(account.token) == account.identity


def test_logout_rolls_back_revocation_if_guest_insert_fails(cloud):
    service, _, _, _ = cloud
    guest = service.create_guest()
    account = service.verify_email_code(guest.identity, "rollback-logout@example.com", "123456")

    def fail_guest_insert(_, __, user):
        if user.is_anonymous:
            raise RuntimeError("simulated guest insertion failure")

    event.listen(User, "before_insert", fail_guest_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            service.logout(account.identity)
    finally:
        event.remove(User, "before_insert", fail_guest_insert)
    assert service.authenticate(account.token) == account.identity


def test_cloud_api_explicit_guest_cookie_csrf_and_login(cloud):
    service, _, sender, _ = cloud
    with TestClient(_app(service)) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.post("/api/auth/guest", headers={"Origin": "https://attacker.example"}).status_code == 403
        assert client.post("/api/auth/guest", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        first = client.post("/api/auth/guest")
        assert first.status_code == 200 and first.json()["isGuest"]
        assert "HttpOnly" in first.headers["set-cookie"] and "SameSite=lax" in first.headers["set-cookie"]
        assert first.headers["cache-control"] == "no-store"
        assert client.get("/api/auth/me").json()["userId"] == first.json()["userId"]
        assert client.post("/api/auth/guest").json()["userId"] == first.json()["userId"]
        assert client.post("/api/auth/email/send", json={"email": "api@example.com"}).status_code == 403
        headers = {"X-CSRF-Token": first.json()["csrfToken"]}
        sent = client.post("/api/auth/email/send", json={"email": "api@example.com"}, headers=headers)
        assert sent.json() == {"retryAfterSeconds": 60}
        assert "code" not in sent.json()
        verified = client.post("/api/auth/email/verify", json={"email": "api@example.com", "code": sender.messages[-1][1]}, headers=headers)
        assert verified.status_code == 200 and verified.json()["isGuest"] is False
        assert client.post("/api/auth/logout", headers=headers).status_code == 403
        logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": verified.json()["csrfToken"]})
        assert logout.status_code == 200 and logout.json()["isGuest"]


@pytest.mark.parametrize("email", ["x", "a@localhost", "a\r\n@x.com", ".a@example.com", "a..b@example.com", "a@-host.com", "用户@example.com"])
def test_invalid_email_rejected(email):
    with pytest.raises(ValueError):
        normalize_email(email)


@pytest.fixture
def real_redis_cloud(identities):
    url = os.getenv("ANIVAG_TEST_AUTH_REDIS_URL")
    if not url:
        pytest.skip("set ANIVAG_TEST_AUTH_REDIS_URL to an isolated temporary Redis")
    # A random HMAC key isolates this run's challenge/rate keys without FLUSHDB.
    config = AuthConfig(mode="cloud", secret_key=uuid4().hex, redis_url=url,
                        cookie_secure=False, send_cooldown_seconds=1)
    client = create_redis_client(config)
    sender = CapturingEmailSender()
    service = AuthService(identities[0], config, redis=client, email_sender=sender)
    service.bootstrap()
    touched: set[str] = set()
    original_eval = client.eval

    def track(script, key_count, *values):
        touched.update(values[:key_count])
        return original_eval(script, key_count, *values)

    client.eval = track
    yield service, client, sender
    if touched:
        client.delete(*touched)
    close_redis_client(client)


def test_real_redis_code_is_consumed_once_under_concurrency(real_redis_cloud):
    service, redis, sender = real_redis_cloud
    guest = service.create_guest("192.0.2.10")
    email = "concurrent@example.com"
    service.send_email_code(guest.identity, email, "192.0.2.10")
    code = sender.messages[-1][1]
    keys = [service._challenge_key(guest.identity, email), *service._rate_keys("verify", guest.identity, email, "192.0.2.10")]
    arguments = [service._code_digest(guest.identity, email, code), 5, 300, 20, 30, 100]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service._eval(_VERIFY_CODE, keys, arguments), range(8)))
    assert sum(result[0] == 1 for result in results) == 1
    assert redis.exists(keys[0]) == 0


def test_real_redis_attempt_budget_cooldown_expiry_and_rate_limits(real_redis_cloud):
    service, redis, sender = real_redis_cloud
    guest = service.create_guest("192.0.2.11")
    email = "attempts@example.com"
    service.send_email_code(guest.identity, email, "192.0.2.11")
    with pytest.raises(AuthError) as cooldown:
        service.send_email_code(guest.identity, email, "192.0.2.11")
    assert cooldown.value.status_code == 429 and cooldown.value.retry_after_seconds >= 1
    actual_code = sender.messages[-1][1]
    wrong = "000000" if actual_code != "000000" else "000001"
    for attempt in range(5):
        with pytest.raises(AuthError) as result:
            service.verify_email_code(guest.identity, email, wrong, "192.0.2.11")
        assert result.value.status_code == (429 if attempt == 4 else 400)
    with pytest.raises(AuthError) as used_up:
        service.verify_email_code(guest.identity, email, actual_code, "192.0.2.11")
    assert used_up.value.code == "EMAIL_CODE_INVALID"
    # Force the key's TTL to elapsed; no wall-clock sleep or production mutation.
    other_email = "expired@example.com"
    service.send_email_code(guest.identity, other_email, "192.0.2.11")
    redis.pexpire(service._challenge_key(guest.identity, other_email), 0)
    with pytest.raises(AuthError) as expired:
        service.verify_email_code(guest.identity, other_email, sender.messages[-1][1], "192.0.2.11")
    assert expired.value.code == "EMAIL_CODE_INVALID"
    service.config = replace(service.config, guest_limit_per_ip=1)
    service.create_guest("192.0.2.12")
    with pytest.raises(AuthError) as limited:
        service.create_guest("192.0.2.12")
    assert limited.value.status_code == 429


def test_real_redis_complete_email_login_and_replay_denied(real_redis_cloud):
    service, _, sender = real_redis_cloud
    guest = service.create_guest("192.0.2.13")
    service.send_email_code(guest.identity, "login@example.com", "192.0.2.13")
    code = sender.messages[-1][1]
    logged_in = service.verify_email_code(guest.identity, "login@example.com", code, "192.0.2.13")
    assert service.authenticate(logged_in.token).email == "login@example.com"
    with pytest.raises(AuthError):
        service.verify_email_code(guest.identity, "login@example.com", code, "192.0.2.13")
    with pytest.raises(AuthError):
        service.authenticate(guest.token)


def test_real_redis_mail_failure_preserves_later_guests_cooldown(real_redis_cloud):
    service, redis, sender = real_redis_cloud
    first = service.create_guest("192.0.2.14")
    later = service.create_guest("192.0.2.15")
    email = "smtp-race@example.com"
    capture = sender.send_code

    def late_failure(_, __, ___):
        # Model an SMTP request that outlives its original send cooldown.
        redis.delete(service.cache_key("cooldown", "email", email))
        sender.send_code = capture
        service.send_email_code(later.identity, email, "192.0.2.15")
        raise RuntimeError("first SMTP call failed after the newer send")

    sender.send_code = late_failure
    with pytest.raises(AuthError) as failed:
        service.send_email_code(first.identity, email, "192.0.2.14")
    assert failed.value.code == "EMAIL_UNAVAILABLE"
    assert redis.exists(service._challenge_key(first.identity, email)) == 0
    assert redis.exists(service._challenge_key(later.identity, email)) == 1
    assert redis.exists(service.cache_key("cooldown", "email", email)) == 1
    assert service.verify_email_code(later.identity, email, sender.messages[-1][1], "192.0.2.15").identity.email == email


@pytest.mark.parametrize("dimension", ["email", "guest", "ip"])
def test_real_redis_send_and_verify_limits_apply_to_each_dimension(real_redis_cloud, dimension):
    service, redis, sender = real_redis_cloud
    guest = service.create_guest("192.0.2.20")
    email = "rate@example.com"
    service.config = replace(service.config, **{f"send_limit_per_{dimension}": 1, f"verify_limit_per_{dimension}": 1})
    service.send_email_code(guest.identity, email, "192.0.2.20")
    code = sender.messages[-1][1]
    # Vary the other two dimensions; only the selected limit is shared.
    next_guest = guest if dimension == "guest" else service.create_guest("192.0.2.21")
    next_email = email if dimension == "email" else "other-rate@example.com"
    next_ip = "192.0.2.20" if dimension == "ip" else "192.0.2.21"
    # Remove only the cooldown to verify the independent longer rate window.
    redis.delete(service.cache_key("cooldown", "email", email))
    with pytest.raises(AuthError) as send_limit:
        service.send_email_code(next_guest.identity, next_email, next_ip)
    assert send_limit.value.status_code == 429
    wrong = "000000" if code != "000000" else "000001"
    with pytest.raises(AuthError) as wrong_code:
        service.verify_email_code(guest.identity, email, wrong, "192.0.2.20")
    assert wrong_code.value.status_code == 400
    with pytest.raises(AuthError) as verify_limit:
        service.verify_email_code(next_guest.identity, next_email, code, next_ip)
    assert verify_limit.value.status_code == 429
    assert redis.exists(service._challenge_key(guest.identity, email)) == 1
