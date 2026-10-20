from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import logging
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..bootstrap import ApplicationServices
from ..config import ServerSettings
from ..config.auth import AuthConfig
from ..config.retrieval import RetrievalConfig
from ..config.sse import SSEConfig
from ..router import router
from .errors import register_exception_handlers
from ..model.vo.commonVO import ApiError
from ..utils.logging import request_id_context

_logger = logging.getLogger(__name__)


def create_app(
    services: ApplicationServices | None = None,
    *,
    settings: ServerSettings | None = None,
) -> FastAPI:
    if services is None:
        from ..main import create_http_application

        return create_http_application(settings=settings)
    return build_app(services, settings=settings)


def build_app(
    services: ApplicationServices,
    *,
    settings: ServerSettings | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    resolved_settings = settings

    app = FastAPI(
        title="anivag Server",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.auth_config = resolved_settings.auth if resolved_settings else getattr(
        getattr(services, "auth", None), "config", AuthConfig()
    )
    app.state.sse_config = resolved_settings.sse if resolved_settings else SSEConfig()
    app.state.retrieval_config = getattr(resolved_settings, "retrieval", RetrievalConfig())
    app.state.model_config_path = getattr(resolved_settings, "model_config_path", None)

    @app.middleware("http")
    async def request_identifier(request, call_next):
        request.state.request_id = str(uuid4())
        token = request_id_context.set(request.state.request_id)
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            if app.state.auth_config.mode == "cloud" and request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "private, no-store"
            status_code = response.status_code
            response.headers["X-Request-Id"] = request.state.request_id
            return response
        finally:
            _logger.log(
                logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO,
                "http_request method=%s path=%s status=%s duration_ms=%.1f",
                request.method, request.url.path, status_code,
                (time.perf_counter() - started_at) * 1000,
            )
            request_id_context.reset(token)
    origins = resolved_settings.allowed_origins if resolved_settings else ()
    if "*" in origins:
        raise ValueError("credentialed API requests require explicit allowed origins")
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Content-Range",
                "Idempotency-Key",
                "X-Content-SHA256",
                "Accept",
                "Range",
                "If-Range",
                "If-None-Match",
                "Last-Event-ID",
                "X-CSRF-Token",
                "X-Use-PPE",
            ],
            expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "ETag", "X-Request-Id",
                            "Content-Disposition", "X-Export-Summary"],
        )
    register_exception_handlers(app)
    app.include_router(router, responses={
        code: {"model": ApiError}
        for code in (400, 401, 403, 404, 409, 410, 422, 429, 500, 502, 503)
    })
    generate_openapi = app.openapi

    def configured_openapi():
        schema = generate_openapi()
        properties = schema["components"]["schemas"]["AgentMessageSendRequest"]["properties"]
        options = services.agent.message_options()
        for field_name, option in (
            ("topK", options.top_k),
            ("maxModelCallsPerTurn", options.max_model_calls_per_turn),
            ("maxToolCallsPerTurn", options.max_tool_calls_per_turn),
        ):
            field = properties[field_name]
            integer_schema = next(
                item for item in field["anyOf"] if item.get("type") == "integer"
            )
            integer_schema["minimum"] = option.minimum
            integer_schema["maximum"] = option.maximum
            field["default"] = option.default_value
        return schema

    app.openapi = configured_openapi
    return app
