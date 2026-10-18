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
from gateway.codex.supervisor import CodexSupervisorModel
from gateway.config import (
    AgentModelConfig,
    ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig,
    CodexModelSourceConfig,
    ModelBinding,
    ModelSourceConfig,
    OpenAIModelSourceConfig,
    resolve_source_api_key,
)
from gateway.agent_model.context import ContextWindowPolicy
from gateway.agent_model.contracts import (
    AgentLoopModel,
    StructuredToolAgentModel,
    SupervisorAgentModel,
)
from gateway.agent_model.deepagents import (
    DeepAgentSessionStore,
    DeepAgentsSupervisorModel,
)
from gateway.openai.agent import OpenAIAgentModel
from gateway.openai.structured import OpenAIStructuredToolAgentModel

AgentReasoningEffort = Literal["none", "low", "high", "max"]


class _BoundAgentModel:
    """Apply resolved request controls and API compaction hints."""

    def __init__(self, delegate, *, timeout: float, max_input_chars: int | None):
        self._delegate = delegate
        self._timeout = timeout
        self._max_input_chars = max_input_chars

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def _options(self, options):
        options = dict(options)
        timeout = options.get("request_timeout_seconds")
        characters = options.get("max_input_chars")
        options["request_timeout_seconds"] = min(
            self._timeout, self._timeout if timeout is None else timeout,
        )
        options["max_input_chars"] = (
            None if self._max_input_chars is None else min(
                self._max_input_chars, self._max_input_chars if characters is None else characters,
            )
        )
        return options

    def run_structured(self, *args, **options):
        return self._delegate.run_structured(*args, **self._options(options))

    def run_agent_loop(self, *args, **options):
        return self._delegate.run_agent_loop(*args, **self._options(options))

    def run_agent(self, *args, **options):
        return self._delegate.run_agent(*args, **self._options(options))

    def close(self):
        close = getattr(self._delegate, "close", None)
        if close is not None:
            close()


class AgentModelFactory:
    """Build role models from validated source references."""

    def __init__(
        self,
        config: AgentModelConfig,
        model_sources: Mapping[str, ModelSourceConfig],
        *,
        fallback_model: str | None = None,
        environ: Mapping[str, str] | None = None,
        binding: ModelBinding | None = None,
        codex_executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._config = config
        if binding is not None and binding.source != config.source:
            raise ValueError("Agent binding source must match agent_model.source")
        self._binding = binding
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
        self._owns_codex_executor = codex_executor is None
        self._codex_executor = (
            codex_executor or ThreadPoolExecutor(
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
        self._supervisor_sessions = DeepAgentSessionStore()

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
        try:
            if executor is not None and self._owns_codex_executor:
                executor.shutdown(wait=False, cancel_futures=True)
        finally:
            self._supervisor_sessions.close()

    def _controls(self, *, request_timeout_seconds, max_output_tokens, max_input_chars, context_window_policy, mcp):
        if self._closed:
            raise RuntimeError("Agent model factory is closed")
        if not isinstance(mcp, bool):
            raise ValueError("mcp must be a boolean")
        native_context = isinstance(self._primary, (CodexModelSourceConfig, ClaudeCodeModelSourceConfig))
        if self._binding is not None:
            from gateway.model_factory import _context_policy, _shared_context_policy
            binding = self._binding
            request_timeout_seconds = min(request_timeout_seconds, binding.request_timeout_seconds)
            max_output_tokens = min(max_output_tokens, binding.max_output_tokens)
            if not native_context:
                max_input_chars = min(max_input_chars, binding.max_input_chars)
                # API fallbacks share the compaction hints for their active request.
                context_window_policy = _shared_context_policy(
                    context_window_policy, _context_policy(binding), output_reserve_tokens=max_output_tokens,
                )
                fallback = binding.fallback
                while fallback is not None:
                    max_input_chars = min(max_input_chars, fallback.max_input_chars)
                    context_window_policy = _shared_context_policy(
                        context_window_policy, _context_policy(fallback), output_reserve_tokens=max_output_tokens,
                    )
                    fallback = fallback.fallback
        if native_context:
            # Native runtimes own compaction and their model's actual capacity.
            max_input_chars = None
            context_window_policy = None
        return dict(
            request_timeout_seconds=request_timeout_seconds, max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars, context_window_policy=context_window_policy,
            mcp=mcp,
        )

    def _bound(self, model, controls):
        if self._binding is None:
            return model
        return _BoundAgentModel(model, timeout=controls["request_timeout_seconds"],
                                max_input_chars=controls["max_input_chars"])

    def build_deep_agent(
        self,
        *,
        model: str,
        reasoning_effort: AgentReasoningEffort,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int,
        context_window_policy: ContextWindowPolicy,
        total_timeout_enabled: bool = True,
        mcp: bool = False,
    ) -> SupervisorAgentModel:
        """Bind the internal Supervisor harness to the configured API model."""
        controls = self._controls(
            request_timeout_seconds=request_timeout_seconds, max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars, context_window_policy=context_window_policy,
            mcp=mcp,
        )
        if not isinstance(
            self._primary, (OpenAIModelSourceConfig, ArkModelSourceConfig)
        ):
            raise ValueError(
                "Deep Agents Supervisor requires an OpenAI-compatible or Ark "
                "agent_model.source; use an API model for the internal "
                "Supervisor and select native Codex/Claude through Bridge"
            )
        with self._close_lock:
            if self._closed:
                raise RuntimeError("Agent model factory is closed")
            adapter = self.build_loop(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    **controls,
                )
            # The internal harness selects its transport by concrete adapter type.
            if isinstance(adapter, _BoundAgentModel):
                adapter = adapter._delegate
            return self._bound(DeepAgentsSupervisorModel(
                agent_model=adapter,
                session_store=self._supervisor_sessions,
                model=model,
                provider_name=self.provider_name,
                request_timeout_seconds=controls["request_timeout_seconds"],
                max_input_chars=controls["max_input_chars"],
                context_window_policy=controls["context_window_policy"],
                supports_vision_input=self._config.supports_vision_input,
                total_timeout_enabled=total_timeout_enabled,
            ), controls)

    def build_supervisor(
        self,
        *,
        harness: Literal["deepagents", "codex"],
        model: str,
        reasoning_effort: AgentReasoningEffort,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int,
        context_window_policy: ContextWindowPolicy,
        total_timeout_enabled: bool = True,
        mcp: bool = False,
    ) -> SupervisorAgentModel:
        options = dict(
            model=model, reasoning_effort=reasoning_effort,
            request_timeout_seconds=request_timeout_seconds,
            max_output_tokens=max_output_tokens, max_input_chars=max_input_chars,
            context_window_policy=context_window_policy,
            mcp=mcp,
        )
        if harness == "deepagents":
            return self.build_deep_agent(**options, total_timeout_enabled=total_timeout_enabled)
        if harness != "codex":
            raise ValueError("Supervisor harness must be deepagents or codex")
        if not isinstance(self._primary, CodexModelSourceConfig):
            raise ValueError("Codex Supervisor requires a codex_app_server source")
        controls = self._controls(
            request_timeout_seconds=request_timeout_seconds, max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars, context_window_policy=context_window_policy,
            mcp=mcp,
        )
        adapter = self._build_loop(
            model=model, reasoning_effort=reasoning_effort,
            **controls, total_timeout_enabled=total_timeout_enabled,
        )
        return CodexSupervisorModel(agent_model=self._bound(adapter, controls))

    def build_structured(
        self,
        *,
        model: str,
        reasoning_effort: AgentReasoningEffort,
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int,
        context_window_policy: ContextWindowPolicy,
        mcp: bool = False,
    ) -> StructuredToolAgentModel:
        controls = self._controls(
            request_timeout_seconds=request_timeout_seconds, max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars, context_window_policy=context_window_policy,
            mcp=mcp,
        )
        adapter = self._build_structured(model=model, reasoning_effort=reasoning_effort, **controls)
        adapter.supports_vision_input = self._config.supports_vision_input
        return self._bound(adapter, controls)

    def _build_structured(
        self, *, model, reasoning_effort, request_timeout_seconds, max_output_tokens,
        max_input_chars, context_window_policy, mcp,
    ):
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
                mcp=mcp,
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
        mcp: bool = False,
    ) -> AgentLoopModel:
        controls = self._controls(
            request_timeout_seconds=request_timeout_seconds, max_output_tokens=max_output_tokens,
            max_input_chars=max_input_chars, context_window_policy=context_window_policy,
            mcp=mcp,
        )
        return self._bound(self._build_loop(model=model, reasoning_effort=reasoning_effort, **controls), controls)

    def _build_loop(
        self, *, model, reasoning_effort, request_timeout_seconds, max_output_tokens,
        max_input_chars, context_window_policy, mcp, total_timeout_enabled=True,
    ):
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
                total_timeout_enabled=total_timeout_enabled,
                mcp=mcp,
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
            supports_vision_input=self._config.fallback.supports_vision_input,
        )

    def _fallback_max_output_tokens(self, primary: int) -> int:
        fallback = self._config.fallback
        if fallback is None or fallback.max_output_tokens is None:
            return primary
        return min(primary, fallback.max_output_tokens) if self._binding is not None else fallback.max_output_tokens

    def _fallback_request_timeout(self, primary: float) -> float:
        fallback = self._config.fallback
        if fallback is None or fallback.request_timeout_seconds is None:
            return primary
        return min(primary, fallback.request_timeout_seconds) if self._binding is not None else fallback.request_timeout_seconds

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
    return resolve_source_api_key(source, environ=environ)


__all__ = ["AgentModelFactory", "AgentReasoningEffort"]
