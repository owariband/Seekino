from __future__ import annotations

from contextlib import contextmanager, suppress
import time
from typing import Iterator, Literal
from uuid import uuid4

from redis.exceptions import RedisError

from ..model.dto.authDTO import AuthIdentity
from ..model.dto.libraryDTO import SeriesState
from ..model.video import SeriesVisibility
from ..error.auth import AuthError
from .auth import AuthService


def require_tenant_read(identity: AuthIdentity, tenant_id: str) -> None:
    if identity.mode == "local":
        return
    if tenant_id not in {identity.tenant_id, identity.public_tenant_id}:
        raise AuthError("请求的资源不存在或无法访问", status_code=404, code="NOT_FOUND")


def require_tenant_write(identity: AuthIdentity, tenant_id: str) -> None:
    if identity.mode == "local":
        return
    if identity.is_guest or identity.auth_level != "email":
        raise AuthError("登录后可使用此功能", status_code=403, code="LOGIN_REQUIRED")
    if identity.tenant_id is None:
        raise AuthError("当前账号仅可体验公开资料", status_code=403, code="PERMISSION_DENIED")
    if tenant_id == identity.tenant_id or (
        identity.role == "admin" and tenant_id == identity.public_tenant_id
    ):
        return
    raise AuthError("请求的资源不存在或无法访问", status_code=404, code="NOT_FOUND")


def require_admin(identity: AuthIdentity) -> None:
    if identity.mode == "local" or (
        identity.auth_level == "email" and identity.role == "admin" and not identity.is_guest
    ):
        return
    raise AuthError("此功能需要管理员权限", status_code=403, code="PERMISSION_DENIED")


def is_series_member(identity: AuthIdentity, tenant_id: str) -> bool:
    return identity.mode == "local" or tenant_id == identity.tenant_id or (
        tenant_id == identity.public_tenant_id and identity.role == "admin"
        and identity.auth_level == "email" and not identity.is_guest
    )


def series_access(identity: AuthIdentity, series: SeriesState) -> Literal["member", "public"]:
    if is_series_member(identity, series.tenant_id):
        return "member"
    status = getattr(series.status, "value", series.status)
    if not (
        series.visibility == SeriesVisibility.PUBLIC and series.publisher_active
        and status == "READY" and series.video_count > 0
        and series.ready_video_count == series.video_count and series.pending_upload_count == 0
    ):
        raise AuthError("请求的资源不存在或无法访问", status_code=404, code="NOT_FOUND")
    return "public"


_ADMIT_QUERY = """
for i = 2, 4 do
    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', ARGV[1])
end
if tonumber(redis.call('GET', KEYS[1]) or '0') >= tonumber(ARGV[3]) then
    return {0, math.max(1, redis.call('TTL', KEYS[1]))}
end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) or
   redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[4]) then
    return {0, 5}
end
if tonumber(ARGV[7]) > 0 and redis.call('ZCARD', KEYS[4]) >= tonumber(ARGV[7]) then
    return {0, 5}
end
if redis.call('INCR', KEYS[1]) == 1 then redis.call('EXPIRE', KEYS[1], ARGV[5]) end
for i = 2, 4 do
    redis.call('ZADD', KEYS[i], tonumber(ARGV[1]) + tonumber(ARGV[6]), ARGV[2])
    redis.call('EXPIRE', KEYS[i], ARGV[6])
end
return {1, 0}
"""


@contextmanager
def model_query(service: AuthService | None, identity: AuthIdentity, ip: str,
                *, maximum_concurrent: int | None = None) -> Iterator[None]:
    """Reserve a request budget and an expiring concurrency lease before model work."""
    if identity.mode == "local":
        yield
        return
    if service is None or service.redis is None:
        raise AuthError("服务暂时不可用", status_code=503, code="AUTH_UNAVAILABLE")
    config = service.config
    kind = "guest" if identity.is_guest else "user"
    budget_identity = ip if identity.is_guest else identity.user_id
    keys = [
        service.cache_key("query-budget", kind, budget_identity),
        service.cache_key("query-active", "user", identity.user_id),
        service.cache_key("query-active", kind, budget_identity),
        service.cache_key("query-active", "service", "agent"),
    ]
    lease = str(uuid4())
    try:
        accepted, retry = service.redis.eval(
            _ADMIT_QUERY, len(keys), *keys, time.time(), lease,
            config.query_limit_guest if identity.is_guest else config.query_limit_user,
            config.concurrent_queries_guest if identity.is_guest else config.concurrent_queries_user,
            config.query_window_seconds, config.query_lease_seconds, maximum_concurrent or 0,
        )
    except RedisError as exc:
        raise AuthError("服务暂时不可用", status_code=503, code="AUTH_UNAVAILABLE") from exc
    if not int(accepted):
        raise AuthError("体验额度已用完或已有回答正在生成，请稍后再试", status_code=429,
                        code="QUERY_LIMITED", retry_after_seconds=int(retry))
    try:
        yield
    finally:
        with suppress(RedisError):
            service.redis.zrem(keys[1], lease)
            service.redis.zrem(keys[2], lease)
            service.redis.zrem(keys[3], lease)
