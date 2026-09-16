from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    directory: Path = Path("outputs/server/logs")
    level: str = "info"
    maximum_file_size_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5

    def __post_init__(self) -> None:
        if self.level not in {"trace", "debug", "info", "warning", "error", "critical"}:
            raise ValueError("invalid log level")
        if self.maximum_file_size_bytes <= 0 or self.backup_count <= 0:
            raise ValueError("log file size and backup count must be positive")
