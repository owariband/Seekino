from __future__ import annotations

import secrets
import re
import time
from typing import Any

from redis.exceptions import RedisError

from ..config.auth import AuthConfig
from ..constants.identity import LOCAL_USER_ID
from ..error.auth import AuthError, IdentityConflictError, IdentityDeniedError
from ..model.dto.authDTO import AuthCredentials, AuthIdentity
from ..model.user import User
from ..model.vo.authVO import AuthPermissions
from ..repository.identity import IdentityRepository
from ..utils.auth import auth_cache_key, auth_code_digest, csrf_digest, decode_auth_token, encode_auth_token, verify_csrf_digest
from ..utils.email import SMTPEmailSender, normalize_email


# A challenge is bound to the guest and email. All limits and challenge writes
# run in one Redis operation so concurrent requests cannot bypass the cooldown.
_SEND_CODE = """
local cooldown = redis.call('TTL', KEYS[2])
if cooldown >= 0 then return {0, math.max(1, cooldown)} end
local retry = 0
for i = 3, 5 do
    local count = tonumber(redis.call('GET', KEYS[i]) or '0')
    if count >= tonumber(ARGV[i + 2]) then
        retry = math.max(retry, math.max(1, redis.call('TTL', KEYS[i])))
    end
end
if retry > 0 then return {-1, retry} end
for i = 3, 5 do
    if redis.call('INCR', KEYS[i]) == 1 then
        redis.call('EXPIRE', KEYS[i], ARGV[4])
    end
end
redis.call('HSET', KEYS[1], 'digest', ARGV[1], 'attempts', 0, 'generation', ARGV[8])
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[8], 'EX', ARGV[3])
return {1, tonumber(ARGV[3])}
"""

_DISCARD_FAILED_CODE = """
if redis.call('HGET', KEYS[1], 'generation') == ARGV[1] then
    redis.call('DEL', KEYS[1])
    if redis.call('GET', KEYS[2]) == ARGV[1] then
        redis.call('DEL', KEYS[2])
    end
end
return 1
"""

# Successful verification consumes the challenge atomically. Failed attempts
# cannot race a correct attempt or preserve a challenge past its attempt budget.
_VERIFY_CODE = """
local retry = 0
for i = 2, 4 do
    local count = tonumber(redis.call('GET', KEYS[i]) or '0')
    if count >= tonumber(ARGV[i + 2]) then
        retry = math.max(retry, math.max(1, redis.call('TTL', KEYS[i])))
    end
end
if retry > 0 then return {-1, retry} end
for i = 2, 4 do
    if redis.call('INCR', KEYS[i]) == 1 then
        redis.call('EXPIRE', KEYS[i], ARGV[3])
    end
end
local digest = redis.call('HGET', KEYS[1], 'digest')
if not digest then return {0, 0} end
if digest ~= ARGV[1] then
    local attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
    if attempts >= tonumber(ARGV[2]) then
        local ttl = redis.call('TTL', KEYS[1])
        redis.call('DEL', KEYS[1])
        return {-2, math.max(1, ttl)}
    end
    return {0, 0}
end
redis.call('DEL', KEYS[1])
return {1, 0}
"""

_CREATE_GUEST_LIMIT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
    return {0, math.max(1, redis.call('TTL', KEYS[1]))}
end
if redis.call('INCR', KEYS[1]) == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return {1, 0}
"""


class AuthService:
    def __init__(self, repository: IdentityRepository, config: AuthConfig, *, redis: Any = None, email_sender: Any = None) -> None:
        self.repository = repository
        self.config = config
        self._signing_key = config.secret_key.encode("utf-8") or secrets.token_bytes(32)
        self.redis = redis
        if config.mode == "cloud" and self.redis is None:
            raise ValueError("cloud authentication requires a caller-provided Redis client")
        if config.mode == "cloud" and email_sender is None and not config.smtp.host:
            raise ValueError("cloud email login requires SMTP configuration")
        self._email_sender = email_sender or SMTPEmailSender(config.smtp)

    def bootstrap(self) -> None:
        self.repository.bootstrap(local_tenant_id=self.config.local_tenant_id, public_tenant_id=self.config.public_tenant_id, mode=self.config.mode)

    def local_identity(self) -> AuthIdentity:
        if self.config.mode != "local":
            raise AuthError("本地身份仅在 local 模式可用")
        user = self.repository.get_user(LOCAL_USER_ID)
        if user is None or user.status != "ACTIVE" or user.is_guest or user.role != "admin":
            raise AuthError("本地身份不可用", status_code=503, code="AUTH_UNAVAILABLE")
        return AuthIdentity(user.user_id, user.email, False, self.config.local_tenant_id, self.config.public_tenant_id, user.role, user.auth_version, "local", "local")

    def authenticate(self, token: str | None) -> AuthIdentity:
        if self.config.mode == "local":
            return self.local_identity()
        payload = decode_auth_token(token, self._signing_key)
        user = self.repository.get_user(payload["sub"])
        if user is None or user.status != "ACTIVE" or user.auth_version != payload["auth_version"]:
            raise AuthError("登录状态已失效", code="AUTH_EXPIRED")
        if payload["auth_level"] != ("guest" if user.is_guest else "email"):
            raise AuthError("登录状态已失效", code="AUTH_EXPIRED")
        return self._identity(user)

    def create_guest(self, ip_address: str | None = None) -> AuthCredentials:
        if self.config.mode == "local":
            return self.issue_credentials(self.local_identity())
        self._limit_guest(ip_address)
        return self.issue_credentials(self._identity(self.repository.create_guest()))

    def issue_credentials(self, identity: AuthIdentity) -> AuthCredentials:
        now = int(time.time())
        expires_at = now + self.config.token_ttl_seconds
        payload = {"sub": identity.user_id, "iat": now, "exp": expires_at, "auth_version": identity.auth_version, "auth_level": identity.auth_level, "jti": secrets.token_urlsafe(18)}
        token = encode_auth_token(payload, self._signing_key)
        return AuthCredentials(identity, token, self.csrf_token(token), expires_at)

    def csrf_token(self, token: str | None) -> str:
        return csrf_digest(token, self._signing_key, local=self.config.mode == "local")

    def verify_csrf(self, token: str | None, supplied: str | None) -> None:
        verify_csrf_digest(token, supplied, self._signing_key, local=self.config.mode == "local")

    def send_email_code(self, identity: AuthIdentity, email: str, ip_address: str | None = None) -> int:
        self._require_guest(identity)
        email = normalize_email(email)
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = self._code_digest(identity, email, code)
        generation = secrets.token_hex(16)
        keys = [self._challenge_key(identity, email), self.cache_key("cooldown", "email", email), *self._rate_keys("send", identity, email, ip_address)]
        config = self.config
        result, retry = self._eval(_SEND_CODE, keys, [digest, config.otp_ttl_seconds, config.send_cooldown_seconds, config.send_window_seconds, config.send_limit_per_email, config.send_limit_per_guest, config.send_limit_per_ip, generation])
        if result != 1:
            raise self._limited(retry)
        try:
            self._email_sender.send_code(email, code, config.otp_ttl_seconds)
        except Exception:
            # Discard only this send's challenge. A mail provider's error must
            # never expose the message body or credentials to the client.
            self._eval(_DISCARD_FAILED_CODE, keys[:2], [generation])
            raise AuthError("验证码邮件发送失败，请稍后重试", status_code=503, code="EMAIL_UNAVAILABLE") from None
        return retry

    def verify_email_code(self, identity: AuthIdentity, email: str, code: str, ip_address: str | None = None) -> AuthCredentials:
        self._require_guest(identity)
        email = normalize_email(email)
        if not re.fullmatch(r"[0-9]{6}", code, flags=re.ASCII):
            raise ValueError("验证码必须为六位数字")
        config = self.config
        result, retry = self._eval(_VERIFY_CODE, [self._challenge_key(identity, email), *self._rate_keys("verify", identity, email, ip_address)], [self._code_digest(identity, email, code), config.otp_attempt_limit, config.verify_window_seconds, config.verify_limit_per_email, config.verify_limit_per_guest, config.verify_limit_per_ip])
        if result < 0:
            raise self._limited(retry)
        if result != 1:
            raise AuthError("验证码错误或已过期", status_code=400, code="EMAIL_CODE_INVALID")
        try:
            user = self.repository.login_email(guest_id=identity.user_id, expected_version=identity.auth_version, email=email, role="admin" if email in config.admin_emails else "user")
        except IdentityDeniedError:
            raise AuthError("登录状态已失效", code="AUTH_EXPIRED") from None
        except IdentityConflictError:
            raise AuthError("登录请求发生冲突，请重新获取验证码", status_code=409, code="AUTH_CONFLICT") from None
        return self.issue_credentials(self._identity(user))

    def logout(self, identity: AuthIdentity, ip_address: str | None = None) -> AuthCredentials:
        if self.config.mode == "local":
            return self.issue_credentials(self.local_identity())
        self._limit_guest(ip_address)
        try:
            guest = self.repository.revoke_and_create_guest(identity.user_id, identity.auth_version)
        except IdentityDeniedError:
            raise AuthError("登录状态已失效", code="AUTH_EXPIRED") from None
        return self.issue_credentials(self._identity(guest))

    @staticmethod
    def permissions(identity: AuthIdentity) -> AuthPermissions:
        return AuthPermissions(can_upload=not identity.is_guest, can_manage=not identity.is_guest, can_configure=identity.role == "admin")

    def _identity(self, user: User) -> AuthIdentity:
        tenant = None if user.is_guest else self.repository.get_owned_tenant(user.user_id)
        if not user.is_guest and tenant is None:
            raise AuthError("个人资料空间不可用", status_code=403, code="TENANT_UNAVAILABLE")
        return AuthIdentity(user.user_id, user.email, user.is_guest, tenant.tenant_id if tenant is not None else None, self.config.public_tenant_id, user.role, user.auth_version, "guest" if user.is_guest else "email", "cloud")

    def _require_guest(self, identity: AuthIdentity) -> None:
        if self.config.mode != "cloud" or not identity.is_guest:
            raise AuthError("请使用游客身份进行邮箱登录", status_code=403, code="GUEST_REQUIRED")
        user = self.repository.get_user(identity.user_id)
        if user is None or user.status != "ACTIVE" or not user.is_guest or user.auth_version != identity.auth_version:
            raise AuthError("登录状态已失效", code="AUTH_EXPIRED")

    def cache_key(self, purpose: str, kind: str, value: str) -> str:
        return auth_cache_key(self._signing_key, purpose, kind, value)

    def _challenge_key(self, identity: AuthIdentity, email: str) -> str:
        return self.cache_key("code", "guest-email", f"{identity.user_id}:{identity.auth_version}:{email}")

    def _rate_keys(self, purpose: str, identity: AuthIdentity, email: str, ip_address: str | None) -> list[str]:
        return [self.cache_key(purpose, "email", email), self.cache_key(purpose, "guest", identity.user_id), self.cache_key(purpose, "ip", ip_address or "unknown")]

    def _code_digest(self, identity: AuthIdentity, email: str, code: str) -> str:
        return auth_code_digest(self._signing_key, identity.user_id, identity.auth_version, email, code)

    def _limit_guest(self, ip_address: str | None) -> None:
        result, retry = self._eval(_CREATE_GUEST_LIMIT, [self.cache_key("guest", "ip", ip_address or "unknown")], [self.config.guest_limit_per_ip, self.config.guest_window_seconds])
        if result != 1:
            raise self._limited(retry)

    def _eval(self, script: str, keys: list[str], arguments: list[Any]) -> Any:
        try:
            return self.redis.eval(script, len(keys), *keys, *arguments)
        except RedisError:
            raise AuthError("认证服务暂不可用，请稍后重试", status_code=503, code="AUTH_UNAVAILABLE") from None

    @staticmethod
    def _limited(retry: int) -> AuthError:
        return AuthError("请求过于频繁，请稍后重试", status_code=429, code="AUTH_RATE_LIMITED", retry_after_seconds=max(1, int(retry)))

