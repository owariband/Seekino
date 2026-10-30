"""Configured model construction and provider dispatch at the Gateway boundary."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel

from gateway.config import (
    AgentFallbackConfig, AgentModelConfig, ArkModelSourceConfig,
    ClaudeCodeModelSourceConfig, CodexModelSourceConfig, EmbeddingModelBinding,
    GatewayConfig, ModelBinding, ModelSourceConfig, OpenAIModelSourceConfig,
    OpenAIVisionSourceConfig, WhisperXSourceConfig, resolve_source_api_key,
)
from gateway.errors import ContentFilterError
from gateway.multimodal import (
    ImageInput, MultimodalInput, TextInput, VideoInput, adapter_capabilities,
    validate_generate_request,
)
from gateway.usage import model_call_context

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from gateway.agent_model.context import ContextCheckpoint, ContextWindowPolicy
    from gateway.agent_model.deepagents import DeepAgentSessionStore
    from gateway.agent_model.factory import AgentModelFactory

_Result = TypeVar("_Result", bound=BaseModel)


class _OwnedResource:
    """Remove closed models from the client's live resource registry."""

    def __init__(
        self, delegate: Any, release: Callable[[_OwnedResource], None], *,
        source: str | None = None,
    ) -> None:
        self._delegate = delegate
        self._release = release
        self._binding_source = source
        self._lock = Lock()
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._delegate, name)
        if self._binding_source is None or not callable(value) or name not in {
            "generate", "transcribe", "embed_text", "embed_query", "embed_image", "embed_video", "embed_sparse", "embed_audio",
        }:
            return value

        @wraps(value)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with model_call_context({"src": self._binding_source}):
                return value(*args, **kwargs)

        return invoke

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._delegate.close()
        finally:
            self._release(self)


def _context_policy(binding: ModelBinding) -> ContextWindowPolicy:
    from gateway.agent_model.context import ContextWindowPolicy
    reserve = binding.max_output_tokens or 0
    fallback = binding.fallback
    while fallback is not None:
        reserve = max(reserve, fallback.max_output_tokens or 0)
        fallback = fallback.fallback
    return ContextWindowPolicy(
        context_window_tokens=binding.context_window_tokens,
        output_reserve_tokens=reserve,
        safety_margin_tokens=binding.context_safety_margin_tokens,
        auto_compact_tokens=binding.auto_compact_tokens,
        compact_target_tokens=binding.compact_target_tokens,
    )


def _shared_context_policy(
    primary: ContextWindowPolicy, fallback: ContextWindowPolicy, *,
    output_reserve_tokens: int | None = None,
) -> ContextWindowPolicy:
    """Keep a shared tool history within both providers' configured limits."""
    from gateway.agent_model.context import ContextWindowPolicy
    window = min(primary.context_window_tokens, fallback.context_window_tokens)
    output = (
        max(primary.output_reserve_tokens, fallback.output_reserve_tokens)
        if output_reserve_tokens is None else output_reserve_tokens
    )
    safety = max(primary.safety_margin_tokens, fallback.safety_margin_tokens)
    available = window - output - safety
    if available <= 2:
        raise ValueError("shared fallback context must exceed output and safety reserves")
    trigger = min(primary.auto_compact_tokens, fallback.auto_compact_tokens, available - 1)
    target = min(primary.compact_target_tokens, fallback.compact_target_tokens, trigger - 1)
    return ContextWindowPolicy(window, output, safety, trigger, target)


class GenerativeModel:
    """One caller contract; provider requests and native harnesses stay here."""

    def __init__(
        self, binding: ModelBinding, source: ModelSourceConfig, delegate: Any,
        fallback: GenerativeModel | None = None,
    ) -> None:
        self.binding = binding
        self.provider_name = source.provider
        self.model_name = binding.model
        self.max_output_tokens = binding.max_output_tokens
        self.supports_vision_input = binding.supports_vision_input
        self.visual_input_mode = (
            adapter_capabilities(source.adapter).visual_input_mode
            if binding.supports_vision_input else None
        )
        self._source = source
        self._delegate = delegate
        self._fallback = fallback
        self._native = isinstance(source, (CodexModelSourceConfig, ClaudeCodeModelSourceConfig))
        self._policy = _context_policy(binding)
        self._closed = False
        self._lock = Lock()

    def _validate_inputs(self, inputs: Sequence[MultimodalInput]) -> None:
        for item in inputs:
            if isinstance(item, TextInput):
                continue
            if isinstance(item, ImageInput) and self.visual_input_mode is not None:
                continue
            if isinstance(item, VideoInput) and self.visual_input_mode == "video":
                continue
            if isinstance(item, VideoInput) and self.visual_input_mode == "ordered_frames":
                raise ValueError(f"adapter {self._source.adapter!r} requires ordered ImageInput frames")
            raise ValueError(f"model {self.model_name!r} does not support {type(item).__name__}")

    def generate(
        self, inputs: Sequence[MultimodalInput], *, schema_name: str,
        result_type: type[_Result], tools: Sequence[BaseTool] = (),
        max_model_calls: int | None = None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        max_input_chars: int | None = None,
        fallback_inputs: Sequence[MultimodalInput] | None = None,
        fallback_on_error: Callable[[BaseException], bool] | None = None,
        tool_harness: Literal["legacy", "deepagents"] = "legacy",
        stop_feedback: Callable[[], str | None] | None = None,
    ) -> _Result:
        from gateway.agent_model.context import preflight_context
        from gateway.agent_model.errors import AgentModelTimeoutError, ModelInputTooLargeError
        from gateway.agent_model.context import ContextWindowOverflow
        with self._lock:
            if self._closed:
                raise RuntimeError("generative model is closed")
        validate_generate_request(inputs, schema_name)
        self._validate_inputs(inputs)
        if tool_harness not in {"legacy", "deepagents"}:
            raise ValueError("unsupported tool_harness")
        if stop_feedback is not None and not callable(stop_feedback):
            raise ValueError("stop_feedback must be callable")
        if stop_feedback is not None and not self._native and not (tools and tool_harness == "deepagents"):
            raise ValueError("stop_feedback requires a native model or DeepAgents with tools")
        if max_model_calls is not None and (
            isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls <= 0
        ):
            raise ValueError("max_model_calls must be positive when provided")
        timeout = self.binding.request_timeout_seconds
        if request_timeout_seconds is not None:
            if request_timeout_seconds <= 0:
                raise ValueError("request_timeout_seconds must be greater than zero")
            timeout = min(timeout, request_timeout_seconds)
        deadline = monotonic() + timeout
        control_failure: BaseException | None = None

        def check_run() -> None:
            nonlocal control_failure
            if check_control is not None:
                try:
                    check_control()
                except BaseException as error:
                    control_failure = error
                    raise

        def allow_fallback(error: BaseException) -> bool:
            filtered = error
            while filtered is not None:
                if isinstance(filtered, ContentFilterError):
                    return fallback_on_error is None or fallback_on_error(filtered)
                filtered = filtered.__cause__
            return False

        check_run()
        policy = (
            _shared_context_policy(self._policy, context_window_policy)
            if context_window_policy is not None else self._policy
        )
        input_limit = self.binding.max_input_chars
        if max_input_chars is not None:
            if isinstance(max_input_chars, bool) or not isinstance(max_input_chars, int) or max_input_chars <= 0:
                raise ValueError("max_input_chars must be positive when provided")
            input_limit = min(input_limit, max_input_chars)
        shared_fallback = bool(
            tools and self._fallback is not None and not self._native
            and not self._fallback._native and self._fallback._fallback is None
        )
        if shared_fallback:
            policy = _shared_context_policy(policy, self._fallback._policy)
            input_limit = min(input_limit, self._fallback.binding.max_input_chars)
            projected = fallback_inputs if fallback_inputs is not None else inputs
            self._fallback._validate_inputs(projected)
        content = []
        for item in inputs:
            if isinstance(item, TextInput):
                content.append({"type": "input_text", "text": item.text})
            else:
                content.append({"type": "input_image" if isinstance(item, ImageInput) else "input_video"})
        try:
            preflight_context(
                [{"role": "user", "content": content}],
                {"output_schema": result_type.model_json_schema(), "tools": [
                    {"name": tool.name, "schema": tool.get_input_schema().model_json_schema()}
                    for tool in tools
                ]}, policy=policy, maximum_input_characters=input_limit, label="Gateway generation",
            )
        except ContextWindowOverflow as error:
            raise ModelInputTooLargeError(str(error)) from error
        feedback_options = {"stop_feedback": stop_feedback} if stop_feedback is not None else {}
        common = dict(
            schema_name=schema_name, result_type=result_type, tools=tools,
            max_model_calls=max_model_calls, request_timeout_seconds=timeout,
            check_control=check_run, context_checkpoint=context_checkpoint,
            context_window_policy=policy, max_input_chars=input_limit,
            **feedback_options,
        )
        try:
            if isinstance(self._source, ClaudeCodeModelSourceConfig):
                with model_call_context({"src": self.binding.source}):
                    result = self._delegate.run_structured(
                        "\n\n".join(item.text for item in inputs), tools=tools,
                        result_type=result_type, max_model_calls=max_model_calls,
                        request_timeout_seconds=timeout, check_control=check_run,
                        context_checkpoint=context_checkpoint, max_input_chars=input_limit,
                        **feedback_options,
                    )
            elif self._native:
                with model_call_context({"src": self.binding.source}):
                    result = self._delegate.generate(inputs, **common)
            else:
                if tools:
                    arguments = {**common, **dict(
                        fallback_model=self._fallback._delegate if shared_fallback else None,
                        fallback_inputs=fallback_inputs if shared_fallback else None,
                        fallback_on_error=allow_fallback,
                    )}
                else:
                    arguments = common
                if tools and tool_harness == "deepagents":
                    from gateway.agent_model.deepagents_multimodal import generate_with_deepagents
                    invoke = partial(generate_with_deepagents, self._delegate)
                else:
                    invoke = self._delegate.generate
                # A shared session chooses its actual fallback inside the harness.
                with model_call_context({"src": None if shared_fallback else self.binding.source}):
                    result = invoke(inputs, **arguments)
        except BaseException as error:
            if control_failure is not None:
                raise control_failure
            if shared_fallback or self._fallback is None or not allow_fallback(error):
                raise
            check_run()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AgentModelTimeoutError("model deadline expired before fallback") from error
            import logging
            from gateway.agent_model.fallback import log_model_fallback
            log_model_fallback(
                logging.getLogger(__name__), primary_provider=self.provider_name,
                primary_model=self.model_name, fallback_provider=self._fallback.provider_name,
                fallback_model=self._fallback.model_name, error=error,
            )
            return self._fallback.generate(
                fallback_inputs if fallback_inputs is not None else inputs,
                **{**common, "request_timeout_seconds": remaining,
                   "context_window_policy": context_window_policy, "max_input_chars": max_input_chars},
                fallback_on_error=fallback_on_error, tool_harness=tool_harness,
            )
        check_run()
        if monotonic() >= deadline:
            raise AgentModelTimeoutError("model execution deadline expired")
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._fallback is not None:
                self._fallback.close()
        finally:
            close = getattr(self._delegate, "close", None)
            if callable(close):
                close()


class GatewayClient:
    """Resolve external model bindings and own their live Gateway resources."""

    def __init__(
        self, config: GatewayConfig, *, environ: Mapping[str, str] | None = None,
        supervisor_sessions: DeepAgentSessionStore | None = None,
    ) -> None:
        self.config = config
        self._environ = os.environ if environ is None else environ
        self._resources: set[_OwnedResource] = set()
        self._codex_executors: dict[str, ThreadPoolExecutor] = {}
        self._whisperx_runtimes: dict[tuple[str, str], Any] = {}
        owns_supervisor_sessions = supervisor_sessions is None and config.supervisor_harness == "deepagents"
        if owns_supervisor_sessions:
            from gateway.agent_model.deepagents import DeepAgentSessionStore
            supervisor_sessions = DeepAgentSessionStore()
        self._supervisor_sessions = supervisor_sessions
        self._owns_supervisor_sessions = owns_supervisor_sessions
        self._lock = Lock()
        self._closed = False

    def _own(self, resource: Any, *, source: str | None = None) -> Any:
        owned = _OwnedResource(resource, self._release, source=source)
        with self._lock:
            if not self._closed:
                self._resources.add(owned)
                return owned
        resource.close()
        raise RuntimeError("Gateway client is closed")

    def _release(self, resource: _OwnedResource) -> None:
        with self._lock:
            self._resources.discard(resource)

    def _binding(self, ref: str | ModelBinding, kind: str) -> ModelBinding:
        self._check_open()
        binding = self.config.resolve_model_binding(ref, environ=self._environ)
        actual_kind = binding.kind or "generative"
        if actual_kind != kind and not (kind == "generative" and actual_kind == "rerank"):
            raise ValueError(f"binding {ref!r} is {actual_kind}, not {kind}")
        return binding

    def _check_open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Gateway client is closed")

    def _codex_executor(self, source_name: str) -> ThreadPoolExecutor | None:
        source = self.config.source(source_name)
        if not isinstance(source, CodexModelSourceConfig):
            return None
        with self._lock:
            if self._closed:
                raise RuntimeError("Gateway client is closed")
            executor = self._codex_executors.get(source_name)
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=source.max_concurrency, thread_name_prefix="seekino-codex",
                )
                self._codex_executors[source_name] = executor
            return executor

    def _build_generative(self, binding: ModelBinding, *, mcp: bool = False) -> GenerativeModel:
        if not isinstance(mcp, bool):
            raise ValueError("mcp must be a boolean")
        selected = binding
        while selected is not None:
            selected_source = self.config.source(selected.source)
            if selected.kind not in {None, "generative", "rerank"} or not adapter_capabilities(selected_source.adapter).generative:
                raise ValueError(f"adapter {selected_source.adapter!r} cannot generate structured output")
            selected = selected.fallback
        source = self.config.source(binding.source)
        key = resolve_source_api_key(source, environ=self._environ)
        common = dict(
            model=binding.model, request_timeout_seconds=binding.request_timeout_seconds,
            max_output_tokens=binding.max_output_tokens,
        )
        if isinstance(source, ArkModelSourceConfig):
            from gateway.ark.multimodal import ArkMultimodalModel
            delegate = ArkMultimodalModel(
                **common, api_key=key, reasoning_effort=binding.reasoning_effort,
            )
        elif isinstance(source, OpenAIModelSourceConfig) and source.profile == "deepseek":
            from gateway.openai.multimodal import DeepSeekMultimodalModel
            delegate = DeepSeekMultimodalModel(
                **common, base_url=source.base_url, api_key=key,
                reasoning_effort=binding.reasoning_effort or "none",
            )
        elif isinstance(source, (OpenAIModelSourceConfig, OpenAIVisionSourceConfig)):
            from gateway.openai.vision import OpenAIVisionModel
            if binding.reasoning_effort not in {None, "none"}:
                raise ValueError("Chat vision generative models do not support reasoning_effort")
            options = (
                dict(token_parameter=source.token_parameter, response_format=source.response_format)
                if isinstance(source, OpenAIVisionSourceConfig) else {}
            )
            delegate = OpenAIVisionModel(
                **common, provider=source.provider, base_url=source.base_url, api_key=key, **options,
            )
        elif isinstance(source, (CodexModelSourceConfig, ClaudeCodeModelSourceConfig)):
            common.update(
                reasoning_effort=binding.reasoning_effort or "none",
                max_input_chars=binding.max_input_chars, context_window_policy=_context_policy(binding),
                max_concurrency=source.max_concurrency,
            )
            if isinstance(source, CodexModelSourceConfig):
                from gateway.codex.agent import CodexAgentModel
                delegate = CodexAgentModel(
                    **common, codex_bin=source.codex_bin,
                    executor=self._codex_executor(binding.source),
                    mcp=mcp,
                )
            else:
                from gateway.claude_code.agent import ClaudeAgentModel
                delegate = ClaudeAgentModel(
                    **common, auth_mode=source.auth_mode, api_key=key, cli_path=source.cli_path,
                )
        else:
            raise ValueError(f"unsupported generative adapter {source.adapter!r}")
        try:
            fallback = self._build_generative(binding.fallback, mcp=mcp) if binding.fallback is not None else None
            return GenerativeModel(binding, source, delegate, fallback)
        except BaseException:
            close = getattr(delegate, "close", None)
            if callable(close):
                close()
            raise

    def create_generative_model(self, ref: str | ModelBinding, *, mcp: bool = False) -> GenerativeModel:
        return self._own(self._build_generative(self._binding(ref, "generative"), mcp=mcp))

    def create_embedding_model(self, ref: str | EmbeddingModelBinding = "embedding") -> Any:
        from gateway.embedding import build_embedding_model
        self._check_open()
        binding = self.config.resolve_embedding_binding(ref, environ=self._environ)
        source = self.config.source(binding.source)
        return self._own(
            build_embedding_model(binding, source, api_key=resolve_source_api_key(source, environ=self._environ)),
            source=binding.source,
        )

    def describe_embedding(self, ref: str | EmbeddingModelBinding = "embedding") -> Any:
        from gateway.embedding import describe_embedding_model
        self._check_open()
        binding = self.config.resolve_embedding_binding(ref, environ=self._environ)
        return describe_embedding_model(binding, self.config.source(binding.source))

    def create_transcription_model(self, ref: str | ModelBinding = "transcription") -> Any:
        from gateway.transcription import build_transcription_model
        binding = self._binding(ref, "transcription")
        source = self.config.source(binding.source)
        options = {}
        if isinstance(source, WhisperXSourceConfig):
            from gateway.local.whisperx import WhisperXRuntime
            with self._lock:
                if self._closed:
                    raise RuntimeError("Gateway client is closed")
                key = (binding.source, binding.model)
                runtime = self._whisperx_runtimes.get(key)
                if runtime is None:
                    runtime = WhisperXRuntime(binding.model, source)
                    self._whisperx_runtimes[key] = runtime
            options["local_runtime"] = runtime
        return self._own(
            build_transcription_model(binding, source, api_key=resolve_source_api_key(source, environ=self._environ), **options),
            source=binding.source,
        )

    def create_speaker_embedding_model(self, ref: str | ModelBinding = "speaker") -> Any:
        from gateway.speaker_embedding import build_speaker_embedding_model
        binding = self._binding(ref, "speaker_embedding")
        return self._own(
            build_speaker_embedding_model(binding, self.config.source(binding.source)),
            source=binding.source,
        )

    def create_rerank_model(self, ref: str | ModelBinding = "rerank") -> Any:
        from gateway.rerank import StructuredRerankModel
        binding = self._binding(ref, "rerank")
        delegate = self._own(self._build_generative(binding))
        return StructuredRerankModel(delegate)

    def create_agent_factory(self, ref: str | ModelBinding = "agent") -> AgentModelFactory:
        from gateway.agent_model.factory import AgentModelFactory
        binding = self._binding(ref, "generative")
        fallback = binding.fallback
        if fallback is not None and fallback.fallback is not None:
            raise ValueError("Agent models support one configured fallback")
        config = AgentModelConfig(
            source=binding.source, model=binding.model,
            supports_vision_input=binding.supports_vision_input,
            max_output_tokens=binding.max_output_tokens,
            request_timeout_seconds=binding.request_timeout_seconds,
            fallback=(AgentFallbackConfig(**fallback.model_dump(include={
                "source", "model", "supports_vision_input", "max_output_tokens",
                "request_timeout_seconds", "reasoning_effort",
            })) if fallback is not None else None),
        )
        return self._own(AgentModelFactory(
            config, self.config.model_sources,
            environ=self._environ, binding=binding,
            codex_executor=self._codex_executor(binding.source),
            supervisor_sessions=self._supervisor_sessions,
        ))

    def create(
        self, kind: Literal["generative", "transcription", "embedding", "rerank", "speaker_embedding"],
        ref: str | ModelBinding,
    ) -> Any:
        constructors = {
            "generative": self.create_generative_model,
            "transcription": self.create_transcription_model,
            "embedding": self.create_embedding_model,
            "rerank": self.create_rerank_model,
            "speaker_embedding": self.create_speaker_embedding_model,
        }
        try:
            constructor = constructors[kind]
        except KeyError as error:
            raise ValueError(f"unsupported model kind {kind!r}") from error
        return constructor(ref)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            resources, self._resources = tuple(self._resources), set()
            executors, self._codex_executors = tuple(self._codex_executors.values()), {}
            local_runtimes, self._whisperx_runtimes = tuple(self._whisperx_runtimes.values()), {}
        failure = None
        for resource in resources:
            try:
                resource.close()
            except BaseException as error:
                failure = failure or error
        for executor in executors:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except BaseException as error:
                failure = failure or error
        for runtime in local_runtimes:
            try:
                runtime.close()
            except BaseException as error:
                failure = failure or error
        if self._owns_supervisor_sessions:
            try:
                self._supervisor_sessions.close()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure
