from __future__ import annotations

from fastapi import Request

from ..bootstrap import ApplicationServices
from ..config.sse import SSEConfig


def get_services(request: Request) -> ApplicationServices:
    return request.app.state.services


def get_sse_config(request: Request) -> SSEConfig:
    return request.app.state.sse_config
