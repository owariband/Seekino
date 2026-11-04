"""Independent execution limits for Video metadata summarization."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class VideoSummarizeConfig:
    enabled: bool = True
    concurrency: int = 1
    max_retries: int = 2
    execution_timeout_seconds: float = 600
    frame_max_height: int = 720
    frame_timeout_seconds: float = 30
    maximum_result_bytes: int = 32_000
    maximum_read_chars: int = 12_000
    maximum_catalog_items: int = 100
    retry_initial_delay_seconds: float = 30
    retry_max_delay_seconds: float = 900

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        for name in (
            "concurrency", "frame_max_height", "maximum_result_bytes", "maximum_read_chars", "maximum_catalog_items",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        for name in (
            "execution_timeout_seconds", "frame_timeout_seconds",
            "retry_initial_delay_seconds", "retry_max_delay_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError("retry_max_delay_seconds must not be below retry_initial_delay_seconds")

    @classmethod
    def from_environment(cls) -> "VideoSummarizeConfig":
        values = {}
        for name, default in asdict(cls()).items():
            env_name = "SEEKINO_SUMMARIZE_" + name.upper()
            raw = os.getenv(env_name)
            if raw is None:
                continue
            if isinstance(default, bool):
                normalized = raw.strip().casefold()
                if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                    raise ValueError(f"{env_name} must be a boolean")
                values[name] = normalized in {"1", "true", "yes", "on"}
            else:
                try:
                    values[name] = float(raw) if name.endswith("_seconds") else int(raw)
                except ValueError as exc:
                    raise ValueError(f"{env_name} has an invalid value") from exc
        return cls(**values)
