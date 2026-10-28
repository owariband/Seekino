from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
import os
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis import Redis
from redis.exceptions import ConnectionError

from server.config.auth import AuthConfig
from server.model.dto.authDTO import AuthIdentity
from server.service.access import model_query
from server.error.auth import AuthError
from server.service.auth import AuthService


@pytest.fixture
def query_service():
    url = os.getenv("SEEKINO_TEST_AUTH_REDIS_URL")
    if not url:
        pytest.skip("set SEEKINO_TEST_AUTH_REDIS_URL to an isolated test Redis")
    redis = Redis.from_url(url, decode_responses=True)
    redis.ping()
    service = AuthService(
        SimpleNamespace(),
        AuthConfig(mode="cloud", secret_key=uuid4().hex, query_limit_guest=5, query_limit_user=5),
        redis=redis, email_sender=SimpleNamespace(),
    )
    touched: set[str] = set()
    original_eval = redis.eval

    def track(script, key_count, *values):
        touched.update(values[:key_count])
        return original_eval(script, key_count, *values)

    redis.eval = track
    try:
        yield service, redis
    finally:
        if touched:
            redis.delete(*touched)
        redis.close()


def _identity(*, guest=True, user_id=None, mode="cloud"):
    user_id = user_id or str(uuid4())
    return AuthIdentity(
        user_id=user_id, email=None if guest else f"{user_id}@example.com",
        is_guest=guest, tenant_id=None if guest else str(uuid4()),
        public_tenant_id="platform-public", role="user", auth_version=1,
        auth_level="guest" if guest else "email", mode=mode,
    )


def _limited(call):
    with pytest.raises(AuthError) as error:
        call()
    assert error.value.status_code == 429
    assert error.value.code == "QUERY_LIMITED"
    assert error.value.retry_after_seconds >= 1


def test_concurrent_guest_requests_admit_only_one_and_release_after_completion(query_service):
    service, redis = query_service
    identity = _identity()
    barrier = Barrier(8)
    entered = Event()
    release = Event()

    def request():
        barrier.wait(timeout=10)
        try:
            with model_query(service, identity, "192.0.2.1"):
                entered.set()
                assert release.wait(timeout=15)
                return 200
        except AuthError as error:
            assert error.code == "QUERY_LIMITED"
            return error.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request) for _ in range(8)]
        assert entered.wait(timeout=10)
        # The seven rejected requests finish while the admitted request holds its lease.
        try:
            rejected, running = wait(futures, timeout=5)
            assert len(rejected) == 7 and len(running) == 1
        finally:
            release.set()
        statuses = [future.result(timeout=10) for future in futures]
    assert statuses.count(200) == 1 and statuses.count(429) == 7
    active = service.cache_key("query-active", "user", identity.user_id)
    assert redis.zcard(active) == 0
    with model_query(service, identity, "192.0.2.1"):
        assert redis.zcard(active) == 1
    assert redis.zcard(active) == 0


def test_query_lease_is_released_when_model_work_raises(query_service):
    service, redis = query_service
    identity = _identity()
    with pytest.raises(ValueError, match="model failed"):
        with model_query(service, identity, "192.0.2.2"):
            raise ValueError("model failed")
    assert redis.zcard(service.cache_key("query-active", "user", identity.user_id)) == 0
    with model_query(service, identity, "192.0.2.2"):
        pass


def test_expired_lease_does_not_block_or_erase_a_new_lease(query_service, monkeypatch):
    service, redis = query_service
    service.config = replace(service.config, query_lease_seconds=2)
    identity = _identity()
    active = service.cache_key("query-active", "user", identity.user_id)
    old = model_query(service, identity, "192.0.2.3")
    old.__enter__()
    try:
        assert 0 < redis.ttl(active) <= 2
        deadline = redis.zrange(active, 0, -1, withscores=True)[0][1]
        monkeypatch.setattr("server.service.access.time.time", lambda: deadline + 1)
        with model_query(service, identity, "192.0.2.3"):
            assert redis.zcard(active) == 1
            old.__exit__(None, None, None)
            assert redis.zcard(active) == 1
    finally:
        old.__exit__(None, None, None)
    assert redis.zcard(active) == 0


def test_guest_changing_identity_cannot_reset_same_ip_budget(query_service):
    service, redis = query_service
    service.config = replace(service.config, query_limit_guest=1)
    first, other = _identity(), _identity()
    with model_query(service, first, "192.0.2.4"):
        pass
    _limited(lambda: model_query(service, other, "192.0.2.4").__enter__())
    assert redis.get(service.cache_key("query-budget", "guest", "192.0.2.4")) == "1"
    with model_query(service, other, "192.0.2.5"):
        pass


def test_budget_key_has_window_ttl_and_expiry_opens_new_budget(query_service):
    service, redis = query_service
    service.config = replace(service.config, query_limit_guest=1, query_window_seconds=2)
    identity = _identity()
    ip = "192.0.2.14"
    key = service.cache_key("query-budget", "guest", ip)
    with model_query(service, identity, ip):
        pass
    assert 0 < redis.ttl(key) <= 2
    _limited(lambda: model_query(service, identity, ip).__enter__())
    redis.pexpire(key, 0)
    with model_query(service, identity, ip):
        assert redis.get(key) == "1"


def test_guest_concurrency_is_shared_by_ip_even_across_identities(query_service):
    service, _ = query_service
    with model_query(service, _identity(), "192.0.2.6"):
        _limited(lambda: model_query(service, _identity(), "192.0.2.6").__enter__())


def test_registered_users_have_separate_budgets_on_same_ip(query_service):
    service, redis = query_service
    service.config = replace(service.config, query_limit_user=1)
    first, other = _identity(guest=False), _identity(guest=False)
    with model_query(service, first, "192.0.2.7"):
        with model_query(service, other, "192.0.2.7"):
            pass
    _limited(lambda: model_query(service, first, "192.0.2.8").__enter__())
    assert redis.get(service.cache_key("query-budget", "user", first.user_id)) == "1"
    assert redis.get(service.cache_key("query-budget", "user", other.user_id)) == "1"


def test_registered_user_concurrency_limit_is_enforced_and_released(query_service):
    service, _ = query_service
    service.config = replace(service.config, concurrent_queries_user=2)
    identity = _identity(guest=False)
    with model_query(service, identity, "192.0.2.9"):
        with model_query(service, identity, "192.0.2.10"):
            _limited(lambda: model_query(service, identity, "192.0.2.11").__enter__())
        with model_query(service, identity, "192.0.2.12"):
            pass


@pytest.mark.parametrize("guest_kinds", [(False, False), (True, True), (False, True)])
def test_global_model_capacity_is_shared_across_users_ips_and_guest_levels(query_service, guest_kinds):
    service, redis = query_service
    first = _identity(guest=guest_kinds[0])
    other = _identity(guest=guest_kinds[1])
    first_ip, other_ip = "192.0.2.40", "192.0.2.41"
    other_budget = service.cache_key(
        "query-budget", "guest" if other.is_guest else "user",
        other_ip if other.is_guest else other.user_id,
    )
    with model_query(service, first, first_ip, maximum_concurrent=1):
        _limited(lambda: model_query(service, other, other_ip, maximum_concurrent=1).__enter__())
        # Capacity rejection does not spend the blocked caller's query budget.
        assert redis.get(other_budget) is None
    with model_query(service, other, other_ip, maximum_concurrent=1):
        assert redis.get(other_budget) == "1"


@pytest.mark.parametrize("available", [False, True])
def test_cloud_query_fails_closed_if_redis_is_missing_or_unavailable(available):
    class UnavailableRedis:
        def eval(self, *_arguments):
            raise ConnectionError("isolated simulated Redis outage")

    service = SimpleNamespace(redis=UnavailableRedis() if available else None, config=AuthConfig())
    service.cache_key = lambda *parts: ":".join(parts)
    ran = False
    with pytest.raises(AuthError) as error:
        with model_query(service, _identity(), "192.0.2.13"):
            ran = True
    assert not ran and error.value.status_code == 503
    assert error.value.code == "AUTH_UNAVAILABLE"


def test_local_query_does_not_require_redis():
    with model_query(None, _identity(mode="local"), "127.0.0.1"):
        pass
