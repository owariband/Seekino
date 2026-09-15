from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentServiceConfig:
    history_message_limit: int = 12
    default_top_k: int = 8
    maximum_top_k: int = 20

    def __post_init__(self) -> None:
        if self.history_message_limit <= 0:
            raise ValueError("history_message_limit must be greater than zero")
        if self.default_top_k <= 0:
            raise ValueError("default_top_k must be greater than zero")
        if self.maximum_top_k < self.default_top_k:
            raise ValueError("maximum_top_k must be at least default_top_k")
        if self.maximum_top_k > 100:
            raise ValueError("maximum_top_k must not exceed 100")
