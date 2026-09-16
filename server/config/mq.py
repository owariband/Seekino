from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class MQConsumerConfig:
    consumer_id: str
    lease_duration: timedelta = timedelta(minutes=5)
    heartbeat_interval: timedelta = timedelta(minutes=1)
    poll_interval_seconds: float = 2.0
    retry_base: timedelta = timedelta(seconds=30)
    retry_max: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if not self.consumer_id.strip():
            raise ValueError("consumer_id must not be empty")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not timedelta(0) < self.heartbeat_interval < self.lease_duration:
            raise ValueError(
                "heartbeat_interval must be positive and smaller than lease_duration"
            )
        if (
            not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be finite and positive")
        if self.retry_base <= timedelta(0):
            raise ValueError("retry_base must be positive")
        if self.retry_max < self.retry_base:
            raise ValueError("retry_max must be greater than or equal to retry_base")
