from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from ..bootstrap import ApplicationServices, build_application_services
from ..config import ServerSettings
from ..config.sse import SSEConfig
from ..router import router
from .errors import register_exception_handlers
from ..model.vo.commonVO import ApiError


def create_app(
    services: ApplicationServices | None = None,
    *,
    settings: ServerSettings | None = None,
) -> FastAPI:
    owns_services = services is None
    resolved_settings = settings
    if services is None:
        resolved_settings = resolved_settings or ServerSettings.from_environment()
        services = build_application_services(resolved_settings)
    assert services is not None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_services:
                await run_in_threadpool(services.close)

    app = FastAPI(
        title="AniVAG Server",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.sse_config = resolved_settings.sse if resolved_settings else SSEConfig()

    @app.middleware("http")
    async def request_identifier(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response
    origins = resolved_settings.allowed_origins if resolved_settings else ()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
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
            ],
            expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "ETag", "X-Request-Id"],
        )
    register_exception_handlers(app)
    app.include_router(router, responses={
        code: {"model": ApiError}
        for code in (400, 403, 404, 409, 410, 422, 500, 502)
    })
    generate_openapi = app.openapi

    def configured_openapi():
        schema = generate_openapi()
        top_k = schema["components"]["schemas"]["AgentMessageSendRequest"]["properties"]["topK"]
        integer_schema = next(item for item in top_k["anyOf"] if item.get("type") == "integer")
        integer_schema["maximum"] = services.agent.maximum_top_k
        top_k["default"] = services.agent.default_top_k
        return schema

    app.openapi = configured_openapi
    return app
