"""Process composition: configuration, callers, business services and HTTP binding."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
import os
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
import uvicorn

from . import caller
from .bootstrap import (
    ApplicationServices,
    build_application_services,
    prepare_service_configuration,
)
from .config import ServerSettings
from .utils.logging import configure_logging


@dataclass(slots=True)
class ApplicationRuntime:
    services: ApplicationServices
    resources: caller.CallerResources
    _closed: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        self.services.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.services.close()
        finally:
            self.resources.close()


def init_application(settings: ServerSettings) -> ApplicationRuntime:
    gateway_config, parse_defaults = prepare_service_configuration(settings)
    resources = caller.init(
        settings,
        semantic_dimensions=parse_defaults.embedding.dimensions,
        vision_dimensions=parse_defaults.embedding.dimensions,
    )
    try:
        services = build_application_services(
            settings,
            resources,
            gateway_config=gateway_config,
            parse_defaults=parse_defaults,
        )
    except BaseException:
        resources.close()
        raise
    return ApplicationRuntime(services=services, resources=resources)


def create_http_application(*, settings: ServerSettings | None = None) -> FastAPI:
    from .api.app import build_app

    settings = settings if settings is not None else ServerSettings.from_environment()
    runtime = init_application(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await run_in_threadpool(runtime.start)
            yield
        finally:
            await run_in_threadpool(runtime.close)

    try:
        app = build_app(runtime.services, settings=settings, lifespan=lifespan)
    except BaseException:
        runtime.close()
        raise
    app.state.runtime = runtime
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m server.api",
        description="Run the anivag HTTP API process.",
    )
    parser.add_argument("--host", default=os.getenv("ANIVAG_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ANIVAG_HTTP_PORT", "9621")))
    parser.add_argument("--log-level", type=str.casefold, default=os.getenv("ANIVAG_LOG_LEVEL", "info"))
    args = parser.parse_args()
    settings = ServerSettings.from_environment()
    configure_logging(
        "api",
        environment=settings.environment,
        config=replace(settings.logging, level=args.log_level.casefold()),
    )
    uvicorn.run(
        partial(create_http_application, settings=settings),
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
