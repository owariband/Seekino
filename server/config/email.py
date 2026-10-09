from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    use_ssl: bool = False
    start_tls: bool = True
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (isinstance(self.port, bool) or not isinstance(self.port, int)
                or not 1 <= self.port <= 65535
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("SMTP port and timeout must be valid")
        if self.use_ssl and self.start_tls:
            raise ValueError("SMTP SSL and STARTTLS cannot both be enabled")
        if self.host and (not self.sender or "\n" in self.sender or "\r" in self.sender):
            raise ValueError("configured SMTP requires a valid sender")
