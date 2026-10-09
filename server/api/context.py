from __future__ import annotations

from fastapi import Request

from ..bootstrap import ApplicationServices
from ..config.sse import SSEConfig
from ..config.auth import AuthConfig
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.authDTO import AuthIdentity
from ..error.auth import AuthError


def get_services(request: Request) -> ApplicationServices:
    return request.app.state.services


def get_sse_config(request: Request) -> SSEConfig:
    return request.app.state.sse_config


def get_identity(request: Request) -> AuthIdentity:
    state = getattr(request, "state", None)
    cached = getattr(state, "identity", None)
    if cached is not None:
        return cached
    service = getattr(get_services(request), "auth", None)
    config = getattr(request.app.state, "auth_config", AuthConfig())
    if service is None:
        if config.mode != "local":
            raise AuthError("身份服务暂时不可用", status_code=503, code="AUTH_UNAVAILABLE")
        identity = AuthIdentity(
            user_id=LOCAL_USER_ID, email=None, is_guest=False,
            tenant_id=config.local_tenant_id, public_tenant_id=config.public_tenant_id,
            role="admin", auth_version=1, auth_level="local", mode="local",
        )
    else:
        token = request.cookies.get(service.config.cookie_name)
        identity = service.authenticate(token)
        if identity.mode == "cloud" and request.method not in {"GET", "HEAD", "OPTIONS"}:
            service.verify_csrf(token, request.headers.get("X-CSRF-Token"))
    if state is not None:
        state.identity = identity
    return identity


def revalidate_identity(request: Request) -> AuthIdentity:
    service = getattr(get_services(request), "auth", None)
    if service is None:
        return get_identity(request)
    return service.authenticate(request.cookies.get(service.config.cookie_name))
