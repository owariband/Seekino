from __future__ import annotations

import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import Literal

from gateway.ark.agent import (
    ArkFunctionCallingFallback,
    ArkFunctionCallingModel,
    ArkStructuredFallback,
    ArkStructuredToolAgentModel,
)
from gateway.claude_code.agent import ClaudeAgentModel
from gateway.codex.agent import CodexAgentModel
from gateway.config import (
    AgentModelConfig,
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    ModelSourceConfig,
    OpenAIModelSourceConfig,
    source_api_key_env_name,
)
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.contracts import AgentLoopModel, StructuredToolAgentModel
from gateway.openai.agent import OpenAIAgentModel
from gateway.openai.structured import OpenAIStructuredToolAgentModel

AgentReasoningEffort = Literal["none", "low", "high", "max"]


class AgentModelFactory:
    """Build role models from validated source references."""

    def __init__(
        self,
        config: AgentModelConfig,
        model_sources: Mapping[str, ModelSourceConfig],
        *,
        fallback_model: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._primary = model_sources[config.source]
        fallback_config = config.fallback
        if (
            fallback_config is not None
            and fallback_config.source == config.source
        ):
            raise ValueError(
                "agent_model.fallback.source must differ from agent_model.source"
            )
        self._fallback = (
            model_sources[fallback_config.source]
            if fallback_config is not None else None
        )
        if fallback_model is not None and fallback_config is None:
            raise ValueError(
                "fallback model override requires agent_model.fallback.source"
            )
        self._fallback_model = (
            fallback_model
            if fallback_model is not None
            else fallback_config.model if fallback_config is not None else None
        )
        _validate_fallback_pair(self._primary, self._fallback)
        if (
            fallback_config is not None
            and fallback_config.reasoning_effort not in (None, "none")
        ):
            raise ValueError(
                "Ark Agent fallback does not support reasoning_effort"
            )
        environment = os.environ if environ is None else environ
        self._api_keys = {
            config.source: _resolve_api_key(self._primary, environment),
        }
        if fallback_config is not None and self._fallback is not None:
            self._api_keys[fallback_config.source] = _resolve_api_key(
                self._fallback,
                environment,
            )
        self._codex_executor = (
            ThreadPoolExecutor(
                max_workers=self._primary.max_concurrency,
                thread_name_prefix="anivag-codex",
            )
            if isinstance(self._primary, CodexModelSourceConfig)
            else None
        )
        self._claude_concurrency_gate = (
            BoundedSemaphore(self._primary.max_concurrency)
            if isinstance(self._primary, ClaudeCodeModelSourceConfig)
            else None
        )
        self._close_lock = Lock()
        self._closed = False

    @property
    def provider_name(self) -> str:
        return self._primary.provider

    @property
    def primary_source(self) -> ModelSourceConfig:
        return self._primary

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._codex_executor
            self._codex_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def build_structured(
        self,
        *,
        model: str,
        reasoning_effort: AgentReasoningEffort,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int,
        context_window_policy: ContextWindowPolicy,
    ) -> StructuredToolAgentModel:
        source = self._primary
        common = {
            "model": model,
            "request_timeout_seconds": request_timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "max_input_chars": max_input_chars,
            "context_window_policy": context_window_policy,
        }
        if isinstance(source, OpenAIModelSourceConfig):
            api_key = self._required_key(self._config.source)
            return OpenAIStructuredToolAgentModel(
                **common,
                provider_name=source.provider,
                api_profile=source.profile,
                base_url=source.base_url,
                api_key=api_key,
                reasoning_effort=reasoning_effort,
                fallback_adapter=self._structured_fallback(
                    request_timeout_seconds=request_timeout_seconds,
                    max_output_tokens=max_output_tokens,
                ),
            )
        if isinstance(source, ArkModelSourceConfig):
            return ArkStructuredToolAgentModel(
                **common,
                fallback_adapter=self._structured_fallback(
                    request_timeout_seconds=request_timeout_seconds,
                    max_output_tokens=max_output_tokens,
                ),
                api_key=self._required_key(self._config.source),
            )
        if isinstance(source, CodexModelSourceConfig):
            return CodexAgentModel(
                **common,
                reasoning_effort=reasoning_effort,
                codex_bin=source.codex_bin,
                max_concurrency=source.max_concurrency,
                executor=self._codex_executor,
            )
        if isinstance(source, ClaudeCodeModelSourceConfig):
            return ClaudeAgentModel(
                **common,
                reasoning_effort=reasoning_effort,
                auth_mode=source.auth_mode,
                api_key=self._api_keys[self._config.source],
                cli_path=source.cli_path,
                max_concurrency=source.max_concurrency,
                concurrency_gate=self._claude_concurrency_gate,
            )
        raise TypeError(f"unsupported Agent source: {type(source).__name__}")

    def build_loop(
        self,
        *,
        model: str,
        reasoning_effort: AgentReasoningEffort,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int,
        context_window_policy: ContextWindowPolicy,
    ) -> AgentLoopModel:
        source = self._primary
        if isinstance(source, OpenAIModelSourceConfig):
            return OpenAIAgentModel(
                model,
                base_url=source.base_url,
                api_key=self._required_key(self._config.source),
                max_output_tokens=max_output_tokens,
                request_timeout_seconds=request_timeout_seconds,
                provider_name=source.provider,
                profile=source.profile,
                reasoning_effort=reasoning_effort,
                fallback_adapter=self._loop_fallback(
                    request_timeout_seconds=request_timeout_seconds,
                    max_output_tokens=max_output_tokens,
                ),
                context_window_policy=context_window_policy,
                supports_vision_input=self._config.supports_vision_input,
            )
        if isinstance(source, ArkModelSourceConfig):
            return ArkFunctionCallingModel(
                model,
                fallback_model=self._same_ark_fallback_model(),
                max_output_tokens=max_output_tokens,
                request_timeout_seconds=request_timeout_seconds,
                fallback_max_output_tokens=self._fallback_max_output_tokens(
                    max_output_tokens
                ),
                fallback_request_timeout_seconds=self._fallback_request_timeout(
                    request_timeout_seconds
                ),
                context_window_policy=context_window_policy,
                api_key=self._required_key(self._config.source),
                supports_vision_input=self._config.supports_vision_input,
                fallback_supports_vision_input=(
                    self._config.fallback.supports_vision_input
                    if self._config.fallback is not None else False
                ),
            )
        if isinstance(source, CodexModelSourceConfig):
            adapter = CodexAgentModel(
                model=model,
                reasoning_effort=reasoning_effort,
                request_timeout_seconds=request_timeout_seconds,
                max_output_tokens=max_output_tokens,
                max_input_chars=max_input_chars,
                context_window_policy=context_window_policy,
                codex_bin=source.codex_bin,
                max_concurrency=source.max_concurrency,
                executor=self._codex_executor,
            )
            adapter.supports_vision_input = self._config.supports_vision_input
            return adapter
        if isinstance(source, ClaudeCodeModelSourceConfig):
            adapter = ClaudeAgentModel(
                model=model,
                reasoning_effort=reasoning_effort,
                request_timeout_seconds=request_timeout_seconds,
                max_output_tokens=max_output_tokens,
                max_input_chars=max_input_chars,
                context_window_policy=context_window_policy,
                auth_mode=source.auth_mode,
                api_key=self._api_keys[self._config.source],
                cli_path=source.cli_path,
                max_concurrency=source.max_concurrency,
                concurrency_gate=self._claude_concurrency_gate,
            )
            adapter.supports_vision_input = self._config.supports_vision_input
            return adapter
        raise TypeError(f"unsupported Agent source: {type(source).__name__}")

    def _loop_fallback(
        self,
        *,
        request_timeout_seconds: float,
        max_output_tokens: int,
    ) -> ArkFunctionCallingFallback | None:
        if self._fallback is None or self._fallback_model is None:
            return None
        assert isinstance(self._fallback, ArkModelSourceConfig)
        assert self._config.fallback is not None
        return ArkFunctionCallingFallback(
            self._fallback_model,
            max_output_tokens=self._fallback_max_output_tokens(max_output_tokens),
            request_timeout_seconds=self._fallback_request_timeout(
                request_timeout_seconds
            ),
            api_key=self._required_key(self._config.fallback.source),
            supports_vision_input=self._config.fallback.supports_vision_input,
        )

    def _structured_fallback(
        self,
        *,
        request_timeout_seconds: float,
        max_output_tokens: int,
    ) -> ArkStructuredFallback | None:
        if self._fallback is None or self._fallback_model is None:
            return None
        assert isinstance(self._fallback, ArkModelSourceConfig)
        assert self._config.fallback is not None
        return ArkStructuredFallback(
            self._fallback_model,
            max_output_tokens=self._fallback_max_output_tokens(max_output_tokens),
            request_timeout_seconds=self._fallback_request_timeout(
                request_timeout_seconds
            ),
            api_key=self._required_key(self._config.fallback.source),
        )

    def _fallback_max_output_tokens(self, primary: int) -> int:
        fallback = self._config.fallback
        if fallback is None or fallback.max_output_tokens is None:
            return primary
        return fallback.max_output_tokens

    def _fallback_request_timeout(self, primary: float) -> float:
        fallback = self._config.fallback
        if fallback is None or fallback.request_timeout_seconds is None:
            return primary
        return fallback.request_timeout_seconds

    def _same_ark_fallback_model(self) -> str | None:
        return self._fallback_model if self._fallback is not None else None

    def _required_key(self, source_name: str) -> str:
        api_key = self._api_keys[source_name]
        if api_key is None:
            raise RuntimeError(f"source {source_name!r} does not have an API key")
        return api_key


def _validate_fallback_pair(
    primary: ModelSourceConfig,
    fallback: ModelSourceConfig | None,
) -> None:
    if fallback is None:
        return
    if isinstance(primary, OpenAIModelSourceConfig) and isinstance(
        fallback, ArkModelSourceConfig
    ):
        return
    if isinstance(primary, ArkModelSourceConfig) and isinstance(
        fallback, ArkModelSourceConfig
    ):
        if primary.api_key_env != fallback.api_key_env:
            raise ValueError(
                "Ark primary and fallback must use the same api_key_env"
            )
        return
    raise ValueError(
        "unsupported Agent fallback adapters: "
        f"{primary.adapter} -> {fallback.adapter}"
    )


def _resolve_api_key(
    source: ModelSourceConfig,
    environ: Mapping[str, str],
) -> str | None:
    environment_name = source_api_key_env_name(source)
    if environment_name is None:
        return None
    api_key = environ.get(environment_name, "").strip()
    if not api_key:
        raise RuntimeError(
            "Agent model API key environment variable "
            f"{environment_name!r} is not configured"
        )
    return api_key


__all__ = ["AgentModelFactory", "AgentReasoningEffort"]
