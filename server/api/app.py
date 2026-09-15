from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..bootstrap import ApplicationServices, build_application_services
from ..config import ServerSettings
from ..config.sse import SSEConfig
from ..router import router
from .errors import register_exception_handlers


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
        yield
        if owns_services:
            services.close()

    app = FastAPI(
        title="AniVAG Server",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.sse_config = resolved_settings.sse if resolved_settings else SSEConfig()
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
            ],
        )
    register_exception_handlers(app)
    app.include_router(router)
    return app
