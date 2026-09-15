from __future__ import annotations

from typing import Literal

from ...schema import ContractModel
from .commonVO import UnixTimeMs


class HealthView(ContractModel):
    status: Literal["ok"] = "ok"
    server_time: UnixTimeMs
