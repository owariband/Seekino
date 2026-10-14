from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AgentServiceConfig:
    history_message_limit: int = 12
    default_top_k: int = 3
    maximum_top_k: int = 20
    max_loop_turns: int = 3
    max_model_calls_per_turn: int = 6
    maximum_model_calls_per_turn: int = 12
    max_tool_calls_per_turn: int = 24
    maximum_tool_calls_per_turn: int = 48
    limits_enabled: bool = False
    total_timeout_enabled: bool = True
    query_reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    evaluator_reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    chat_reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    supervisor_reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    web_search_max_results: int = 5
    web_search_timeout_seconds: float = 12.0
    max_output_tokens: int = 8192
    max_prompt_chars: int = 200_000
    context_window_tokens: int = 64_000
    context_safety_margin_tokens: int = 4_096
    auto_compact_tokens: int = 48_000
    compact_target_tokens: int = 24_000
    max_tool_result_bytes: int = 16_384
    max_read_chars_per_call: int = 16_384
    max_catalog_items_per_page: int = 20
    stage_timeout_seconds: float = 480
    evaluator_stage_timeout_seconds: float = 480
    chat_timeout_seconds: float = 480
    supervisor_timeout_seconds: float = 480
    execution_timeout_seconds: float = 480
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
        if not isinstance(self.limits_enabled, bool):
            raise ValueError("limits_enabled must be a boolean")
        if not isinstance(self.total_timeout_enabled, bool):
            raise ValueError("total_timeout_enabled must be a boolean")
        for name in (
            "query_reasoning_effort", "evaluator_reasoning_effort",
            "chat_reasoning_effort", "supervisor_reasoning_effort",
        ):
            if getattr(self, name) not in ("none", "low", "high", "max"):
                raise ValueError(f"{name} must be none, low, high, or max")
        for name in (
            "max_model_calls_per_turn", "maximum_model_calls_per_turn",
            "max_tool_calls_per_turn", "maximum_tool_calls_per_turn",
            "max_output_tokens", "max_prompt_chars", "max_tool_result_bytes",
            "max_read_chars_per_call",
            "context_window_tokens", "context_safety_margin_tokens",
            "auto_compact_tokens", "compact_target_tokens",
            "max_catalog_items_per_page", "maximum_concurrent_kicks",
            "web_search_max_results",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.maximum_model_calls_per_turn < self.max_model_calls_per_turn:
            raise ValueError(
                "maximum_model_calls_per_turn must be at least "
                "max_model_calls_per_turn"
            )
        if self.maximum_tool_calls_per_turn < self.max_tool_calls_per_turn:
            raise ValueError(
                "maximum_tool_calls_per_turn must be at least "
                "max_tool_calls_per_turn"
            )
        if self.max_tool_result_bytes < 512:
            raise ValueError("max_tool_result_bytes must be at least 512")
        if self.max_catalog_items_per_page > 100:
            raise ValueError("max_catalog_items_per_page must not exceed 100")
        if self.web_search_max_results > 20:
            raise ValueError("web_search_max_results must not exceed 20")
        if self.web_search_timeout_seconds > 60:
            raise ValueError("web_search_timeout_seconds must not exceed 60")
        maximum_input_tokens = (
            self.context_window_tokens
            - self.max_output_tokens
            - self.context_safety_margin_tokens
        )
        if maximum_input_tokens <= 0:
            raise ValueError(
                "context_window_tokens must exceed max_output_tokens and "
                "context_safety_margin_tokens"
            )
        if self.auto_compact_tokens > maximum_input_tokens:
            raise ValueError(
                "auto_compact_tokens must not exceed the maximum input budget"
            )
        if self.compact_target_tokens >= self.auto_compact_tokens:
            raise ValueError(
                "compact_target_tokens must be lower than auto_compact_tokens"
            )
        for name in (
            "stage_timeout_seconds", "evaluator_stage_timeout_seconds",
            "chat_timeout_seconds", "supervisor_timeout_seconds",
            "execution_timeout_seconds",
            "web_search_timeout_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
