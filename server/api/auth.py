from __future__ import annotations

import time
from urllib.parse import urlsplit

from fastapi import Request, Response

from ..model.dto.authDTO import AuthCredentials, AuthIdentity, EmailCodeSendRequest, EmailCodeVerifyRequest
from ..model.vo.authVO import AuthIdentityView, EmailCodeSendView
from ..error.auth import AuthError
from ..service.auth import AuthService
from .context import get_services


def _service(request: Request) -> AuthService:
    service = getattr(get_services(request), "auth", None)
    if service is None:
        raise AuthError("身份服务暂时不可用", status_code=503, code="AUTH_UNAVAILABLE")
    return service


def _token(request: Request, service: AuthService) -> str | None:
    return getattr(request.state, "auth_token", None) or request.cookies.get(service.config.cookie_name)


def _identity(request: Request, service: AuthService) -> AuthIdentity:
    identity = getattr(request.state, "identity", None)
    return identity if identity is not None else service.authenticate(_token(request, service))


def _check_write(request: Request, service: AuthService) -> AuthIdentity:
    identity = _identity(request, service)
    if service.config.mode == "cloud":
        service.verify_csrf(_token(request, service), request.headers.get("X-CSRF-Token"))
    return identity


def _view(service: AuthService, identity: AuthIdentity, csrf_token: str) -> AuthIdentityView:
    return AuthIdentityView(user_id=identity.user_id, email=identity.email, is_guest=identity.is_guest, tenant_id=identity.tenant_id, public_tenant_id=identity.public_tenant_id, mode=identity.mode, csrf_token=csrf_token, permissions=service.permissions(identity))


def _apply_credentials(request: Request, response: Response, service: AuthService, credentials: AuthCredentials) -> AuthIdentityView:
    request.state.identity = credentials.identity
    request.state.auth_token = credentials.token
    request.state.auth_credentials = credentials
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(service.config.cookie_name, credentials.token, max_age=max(0, credentials.expires_at - int(time.time())), httponly=True, secure=service.config.cookie_secure, samesite="lax", path="/")
    return _view(service, credentials.identity, credentials.csrf_token)


def get_me(request: Request, response: Response) -> AuthIdentityView:
    service = _service(request)
    identity = _identity(request, service)
    response.headers["Cache-Control"] = "no-store"
    return _view(service, identity, service.csrf_token(_token(request, service)))


def create_guest(request: Request, response: Response) -> AuthIdentityView:
    service = _service(request)
    if service.config.mode == "cloud":
        origin = request.headers.get("Origin")
        same_origin = True
        if origin:
            try:
                parsed = urlsplit(origin)
                same_origin = (
                    parsed.scheme == request.url.scheme
                    and parsed.hostname == request.url.hostname
                    and (parsed.port or (443 if parsed.scheme == "https" else 80))
                    == (request.url.port or (443 if request.url.scheme == "https" else 80))
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and parsed.username is None and parsed.password is None
                )
            except ValueError:
                same_origin = False
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or not same_origin:
            raise AuthError("游客身份需从本站创建", status_code=403, code="CSRF_INVALID")
    token = _token(request, service)
    if token:
        try:
            identity = service.authenticate(token)
        except AuthError as exc:
            if exc.status_code != 401:
                raise
        else:
            # Repeated bootstrap requests preserve the current guest/account.
            response.headers["Cache-Control"] = "no-store"
            return _view(service, identity, service.csrf_token(token))
    return _apply_credentials(request, response, service, service.create_guest(request.client.host if request.client else None))


def send_email_code(body: EmailCodeSendRequest, request: Request, response: Response) -> EmailCodeSendView:
    service = _service(request)
    identity = _check_write(request, service)
    retry = service.send_email_code(identity, body.email, request.client.host if request.client else None)
    response.headers["Cache-Control"] = "no-store"
    return EmailCodeSendView(retry_after_seconds=retry)


def verify_email_code(body: EmailCodeVerifyRequest, request: Request, response: Response) -> AuthIdentityView:
    service = _service(request)
    identity = _check_write(request, service)
    credentials = service.verify_email_code(identity, body.email, body.code, request.client.host if request.client else None)
    return _apply_credentials(request, response, service, credentials)


def logout(request: Request, response: Response) -> AuthIdentityView:
    service = _service(request)
    identity = _check_write(request, service)
    return _apply_credentials(request, response, service, service.logout(identity, request.client.host if request.client else None))
