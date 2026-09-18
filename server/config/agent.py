from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentServiceConfig:
    history_message_limit: int = 12
    default_top_k: int = 8
    maximum_top_k: int = 20
    max_loop_turns: int = 3
    max_model_calls_per_turn: int = 6
    max_tool_calls_per_turn: int = 4
    max_output_tokens: int = 4096
    max_prompt_chars: int = 200_000
    # Covers up to three Query/Evaluator turns plus final Chat. Individual
    # Provider requests keep their own, shorter timeout.
    execution_timeout_seconds: float = 600
    maximum_concurrent_kicks: int = 4

    def __post_init__(self) -> None:
        if self.history_message_limit <= 0:
            raise ValueError("history_message_limit must be greater than zero")
        if self.default_top_k <= 0:
            raise ValueError("default_top_k must be greater than zero")
        if self.maximum_top_k < self.default_top_k:
            raise ValueError("maximum_top_k must be at least default_top_k")
        if self.maximum_top_k > 100:
            raise ValueError("maximum_top_k must not exceed 100")
        if not 1 <= self.max_loop_turns <= 3:
            raise ValueError("max_loop_turns must be between 1 and 3")
        for name in (
            "max_model_calls_per_turn", "max_tool_calls_per_turn",
            "max_output_tokens", "max_prompt_chars", "maximum_concurrent_kicks",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if (
            not math.isfinite(self.execution_timeout_seconds)
            or self.execution_timeout_seconds <= 0
        ):
            raise ValueError("execution_timeout_seconds must be finite and positive")
