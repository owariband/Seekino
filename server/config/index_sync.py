from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IndexSyncConfig:
    retry_base_seconds: int = 30
    retry_max_seconds: int = 900

    def __post_init__(self) -> None:
        if self.retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be greater than zero")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError(
                "retry_max_seconds must be greater than or equal to retry_base_seconds"
            )
