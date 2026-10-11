from __future__ import annotations

from typing import Literal

from ...schema import ContractModel
from .commonVO import UnixTimeMs


class RuntimeConfigView(ContractModel):
    display_mode: Literal["release", "debug"]


class ParseConsumerHealthView(ContractModel):
    status: Literal["running", "failed", "stopped"]
    failure: str | None = None


class HealthComponentsView(ContractModel):
    parse_consumer: ParseConsumerHealthView


class HealthView(ContractModel):
    status: Literal["ok", "degraded"]
    server_time: UnixTimeMs
    components: HealthComponentsView
