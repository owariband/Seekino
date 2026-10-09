from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

from ..constants.auth import AUTH_REDIS_KEY_PREFIX, AUTH_TOKEN_ALGORITHM, AUTH_TOKEN_TYPE, MAX_AUTH_TOKEN_LENGTH
from ..constants.database import ID_LENGTH
from ..error.auth import AuthError


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unbase64(value: str) -> bytes:
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _base64(decoded) != value:
        raise ValueError("noncanonical token encoding")
    return decoded


def encode_auth_token(payload: dict[str, Any], signing_key: bytes) -> str:
    header = {"alg": AUTH_TOKEN_ALGORITHM, "typ": AUTH_TOKEN_TYPE}
    parts = [_base64(json.dumps(value, separators=(",", ":")).encode("utf-8")) for value in (header, payload)]
    signing_input = ".".join(parts)
    signature = _base64(hmac.digest(signing_key, signing_input.encode("ascii"), "sha256"))
    return f"{signing_input}.{signature}"


def decode_auth_token(token: str | None, signing_key: bytes) -> dict[str, Any]:
    if not token:
        raise AuthError("请先建立游客身份或登录")
    try:
        if len(token) > MAX_AUTH_TOKEN_LENGTH:
            raise ValueError
        header_part, payload_part, signature_part = token.split(".")
        expected = hmac.digest(signing_key, f"{header_part}.{payload_part}".encode("ascii"), "sha256")
        if not hmac.compare_digest(_unbase64(signature_part), expected):
            raise ValueError
        header = json.loads(_unbase64(header_part))
        payload = json.loads(_unbase64(payload_part))
        if header != {"alg": AUTH_TOKEN_ALGORITHM, "typ": AUTH_TOKEN_TYPE} or not isinstance(payload, dict):
            raise ValueError
        if not isinstance(payload.get("sub"), str) or not payload["sub"] or len(payload["sub"]) > ID_LENGTH:
            raise ValueError
        if type(payload.get("exp")) is not int or type(payload.get("auth_version")) is not int or payload["auth_version"] < 1:
            raise ValueError
        if payload["exp"] <= int(time.time()) or payload.get("auth_level") not in {"guest", "email"}:
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise AuthError("登录状态已失效", code="AUTH_EXPIRED") from None


def csrf_digest(token: str | None, signing_key: bytes, *, local: bool = False) -> str:
    basis = "local" if local else token
    if not basis:
        raise AuthError("缺少登录凭据")
    return hmac.new(signing_key, f"csrf:{basis}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_digest(token: str | None, supplied: str | None, signing_key: bytes, *, local: bool = False) -> None:
    expected = csrf_digest(token, signing_key, local=local)
    if (not supplied or not re.fullmatch(r"[0-9a-f]{64}", supplied, flags=re.ASCII)
            or not hmac.compare_digest(expected, supplied)):
        raise AuthError("请求验证失败，请刷新后重试", status_code=403, code="CSRF_INVALID")


def auth_cache_key(signing_key: bytes, purpose: str, kind: str, value: str) -> str:
    digest = hmac.new(signing_key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{AUTH_REDIS_KEY_PREFIX}:{purpose}:{kind}:{digest}"


def auth_code_digest(signing_key: bytes, user_id: str, version: int, email: str, code: str) -> str:
    return hmac.new(signing_key, f"otp:{user_id}:{version}:{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()
