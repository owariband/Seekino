from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEConfig:
    poll_interval_seconds: float = 1.0
    keepalive_interval_seconds: float = 15.0

    def __post_init__(self) -> None:
        for name in ("poll_interval_seconds", "keepalive_interval_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")
