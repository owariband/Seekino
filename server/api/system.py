from __future__ import annotations

from fastapi import Request, Response

from ..utils.time import now_unix_time_ms
from ..model.vo.systemVO import (
    HealthComponentsView,
    HealthView,
    ParseConsumerHealthView,
    RuntimeConfigView,
)
from .context import get_services


def runtime_config(request: Request, response: Response) -> RuntimeConfigView:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "x-use-ppe"
    return RuntimeConfigView(
        display_mode="debug" if request.headers.get("x-use-ppe") == "1" else "release",
    )


def health(request: Request) -> HealthView:
    consumer = get_services(request).parse_consumer
    if consumer is None:
        consumer_health = ParseConsumerHealthView(status="stopped")
    elif consumer.failure is not None:
        consumer_health = ParseConsumerHealthView(
            status="failed",
            failure=type(consumer.failure).__name__,
        )
    elif consumer.running:
        consumer_health = ParseConsumerHealthView(status="running")
    else:
        consumer_health = ParseConsumerHealthView(status="stopped")
    return HealthView(
        status="ok" if consumer_health.status == "running" else "degraded",
        server_time=now_unix_time_ms(),
        components=HealthComponentsView(parse_consumer=consumer_health),
    )
