from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    model_serializer,
    model_validator,
)

from gateway.constants import (
    ARK_ADAPTER,
    ARK_PROVIDER,
    CLAUDE_CODE_PROVIDER,
    CLAUDE_SDK_ADAPTER,
    CODEX_APP_SERVER_ADAPTER,
    CODEX_PROVIDER,
    DEEPSEEK_PROVIDER,
    KIMI_FIXED_TEMPERATURE,
    OPENAI_ADAPTER,
    OPENAI_DEEPSEEK_PROFILE,
    OPENAI_KIMI_PROFILE,
    OPENAI_STANDARD_PROFILE,
    ReasoningEffort,
)
from gateway.multimodal import adapter_capabilities

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_DEFAULT_PRIMARY_SOURCE = "deepseek_api"
_DEFAULT_FALLBACK_SOURCE = "ark_default"
_DEFAULT_AGENT_MODEL = "deepseek-flash"
_DEFAULT_ARK_MODEL = "doubao-seed-2-0-lite-260428"
_LEGACY_PARSE_ARK_SOURCE = "legacy_parse_ark_source"


def _https_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "base_url must be an HTTPS URL without credentials, query, or fragment"
        )
    return value.rstrip("/")


_HttpsBaseUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_https_base_url),
]


def _audio_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        _https_base_url("https:" + value[len("http:"):])
        return value.rstrip("/")
    return _https_base_url(value)


_COMPOSE_EMBEDDING_BASE_URL = "http://embedding:18845"


def _llama_cpp_base_url(value: str) -> str:
    """Allow the operator's private Compose endpoint for the llama.cpp adapter."""
    if value.removesuffix("/").lower() == _COMPOSE_EMBEDDING_BASE_URL:
        return _COMPOSE_EMBEDDING_BASE_URL
    return _audio_base_url(value)


_AudioBaseUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_audio_base_url),
]
_LlamaCppBaseUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_llama_cpp_base_url),
]
_NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class DeepSeekModelConfig(BaseModel):
    """Parse fallback configuration, independent from Chat Agent sources."""

    model_config = ConfigDict(extra="forbid")

    provider: _NonEmptyString = "deepseek"
    protocol: Literal["deepseek_responses", "openai_chat"] = "deepseek_responses"
    vision_source: _NonEmptyString | None = None
    model: _NonEmptyString
    base_url: _HttpsBaseUrl
    api_key_env: _NonEmptyString | None
    reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    max_output_tokens: int = Field(ge=256, le=262_144)
    request_timeout_seconds: float = Field(gt=0)


class ChatSourceCredentials(BaseModel):
    display_name: _NonEmptyString | None = Field(default=None, exclude=True)
    encrypted_api_key: _NonEmptyString | None = Field(default=None, repr=False, exclude=True)
    _config_path: Path | None = PrivateAttr(default=None)
    _source_name: str = PrivateAttr(default="")

    def credential_destination(self) -> tuple[str, ...]:
        return (self._source_name, self.adapter, getattr(self, "protocol", ""), getattr(self, "base_url", ""))

    def saved_key(self) -> str | None:
        if self.encrypted_api_key is None:
            return None
        if self._config_path is None:
            raise RuntimeError("模型凭证缺少配置文件路径")
        from .model_credentials import unseal
        return unseal(self._config_path, self.credential_destination(), self.encrypted_api_key)


class ChatAPISourceConfig(ChatSourceCredentials):
    """A registered HTTP model service. The binding kind selects generation, ASR or embeddings."""
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["chat_api"] = "chat_api"
    provider: _NonEmptyString = "chat_api"
    protocol: Literal["openai_chat", "openai_responses", "anthropic_messages", "gemini"]
    base_url: _NonEmptyString
    api_key_env: _NonEmptyString | None = None


class OpenAIModelSourceConfig(ChatSourceCredentials):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[OPENAI_ADAPTER] = OPENAI_ADAPTER
    provider: _NonEmptyString
    profile: Literal[
        OPENAI_STANDARD_PROFILE,
        OPENAI_DEEPSEEK_PROFILE,
        OPENAI_KIMI_PROFILE,
    ] = OPENAI_STANDARD_PROFILE
    base_url: _HttpsBaseUrl
    api_key_env: _NonEmptyString | None


class ArkModelSourceConfig(ChatSourceCredentials):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[ARK_ADAPTER] = ARK_ADAPTER
    provider: Literal[ARK_PROVIDER] = ARK_PROVIDER
    api_key_env: _NonEmptyString | None = "ARK_API_KEY"


class OllamaEmbeddingSourceConfig(ChatSourceCredentials):
    """Ollama's native text embedding API; local HTTP needs no API key."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["ollama_embedding"] = "ollama_embedding"
    provider: Literal["ollama"] = "ollama"
    api_key_env: _NonEmptyString | None = Field(default=None, exclude_if=lambda v: v is None)
    base_url: _AudioBaseUrl


class LlamaCppEmbeddingSourceConfig(BaseModel):
    """Local llama.cpp OpenAI-compatible embedding endpoint."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["llama_cpp_embedding"] = "llama_cpp_embedding"
    provider: Literal["llama_cpp"] = "llama_cpp"
    base_url: _LlamaCppBaseUrl
    document_base_urls: tuple[_LlamaCppBaseUrl, ...] = Field(default=(), exclude_if=lambda urls: not urls)


class OpenAIAudioSourceConfig(ChatSourceCredentials):
    """Audio Transcriptions protocol, independent of Chat Completions."""

    model_config = ConfigDict(extra="forbid")
    adapter: Literal["openai_audio"] = "openai_audio"
    provider: _NonEmptyString
    base_url: _AudioBaseUrl
    api_key_env: _NonEmptyString | None
    response_format: Literal["verbose_json", "json"] = "verbose_json"


class WhisperXSourceConfig(BaseModel):
    """Optional in-process ASR and forced alignment using user-installed weights."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["whisperx"] = "whisperx"
    provider: Literal["local"] = "local"
    device: Literal["cpu", "cuda"] = "cpu"
    compute_type: Literal["int8", "float32", "float16", "int8_float16"] = "int8"
    batch_size: int = Field(default=8, ge=1, le=64, strict=True)
    cpu_threads: int = Field(default=6, ge=1, le=64, strict=True)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    alignment_models: dict[Annotated[str, Field(pattern=r"^[a-z]{2,3}$")], _NonEmptyString] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_local_alignment(self) -> "WhisperXSourceConfig":
        if self.device == "cpu" and self.compute_type not in {"int8", "float32"}:
            raise ValueError("WhisperX CPU requires int8 or float32")
        if self.language is not None and self.language not in self.alignment_models:
            raise ValueError("WhisperX language requires a configured alignment model")
        return self


class PyannoteSourceConfig(BaseModel):
    """Offline audio diarization; no person names or video/task state."""

    model_config = ConfigDict(extra="forbid")
    adapter: Literal["pyannote"] = "pyannote"
    provider: Literal["local"] = "local"
    device: Literal["cpu", "mps", "cuda"] = "mps"
    cpu_threads: int = Field(default=6, ge=1, le=64, strict=True)
    segmentation_batch_size: int = Field(default=32, ge=1, strict=True)
    embedding_batch_size: int = Field(default=32, ge=1, strict=True)


class OpenAIVisionSourceConfig(BaseModel):
    """Chat Completions with images and structured JSON output."""

    model_config = ConfigDict(extra="forbid")
    adapter: Literal["openai_vision"] = "openai_vision"
    provider: _NonEmptyString
    base_url: _HttpsBaseUrl
    api_key_env: _NonEmptyString
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    response_format: Literal["json_schema", "json_object"] = "json_schema"


class CampPlusModelSourceConfig(BaseModel):
    """Local CPU speaker encoder; media selection remains with the caller."""

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["campplus"] = "campplus"
    provider: Literal["local"] = "local"
    checkpoint_path: _NonEmptyString
    cpu_threads: int = Field(default=6, ge=1, le=64, strict=True)
    max_audio_seconds: float = Field(default=30, ge=0.4, le=300)


class CodexModelSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[CODEX_APP_SERVER_ADAPTER] = CODEX_APP_SERVER_ADAPTER
    provider: Literal[CODEX_PROVIDER] = CODEX_PROVIDER
    codex_bin: _NonEmptyString | None = None
    max_concurrency: int = Field(default=4, ge=1, le=64)


class ClaudeCodeModelSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[CLAUDE_SDK_ADAPTER] = CLAUDE_SDK_ADAPTER
    provider: Literal[CLAUDE_CODE_PROVIDER] = CLAUDE_CODE_PROVIDER
    auth_mode: Literal["local_subscription", "api_key"] = "local_subscription"
    api_key_env: _NonEmptyString | None = None
    cli_path: _NonEmptyString | None = None
    max_concurrency: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def validate_api_key_auth(self) -> ClaudeCodeModelSourceConfig:
        if self.auth_mode == "api_key" and self.api_key_env is None:
            raise ValueError("api_key_env is required when auth_mode is api_key")
        return self


ModelSourceConfig = (
    OpenAIModelSourceConfig
    | ChatAPISourceConfig
    | ArkModelSourceConfig
    | CodexModelSourceConfig
    | ClaudeCodeModelSourceConfig
    | OpenAIAudioSourceConfig
    | WhisperXSourceConfig
    | PyannoteSourceConfig
    | OpenAIVisionSourceConfig
    | CampPlusModelSourceConfig
    | OllamaEmbeddingSourceConfig
    | LlamaCppEmbeddingSourceConfig
)
_ModelSourceConfigField = Annotated[
    ModelSourceConfig,
    Field(discriminator="adapter"),
]


class ModelBinding(BaseModel):
    """Canonical model selection, independent from its business caller."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    source: _NonEmptyString
    stream: bool = Field(default=True, strict=True)
    temperature: float = Field(default=0.0, ge=0, le=2, strict=True, allow_inf_nan=False)
    model: _NonEmptyString
    kind: Literal["generative", "rerank", "transcription", "embedding", "speaker_embedding", "diarization"] | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, ge=256, le=262144)
    reasoning_effort: ReasoningEffort | None = None
    supports_vision_input: bool = False
    fallback: ModelBinding | None = None
    context_window_tokens: int | None = Field(default=None, ge=2)
    context_safety_margin_tokens: int | None = Field(default=None, ge=1)
    auto_compact_tokens: int | None = Field(default=None, ge=1)
    compact_target_tokens: int | None = Field(default=None, ge=1)
    max_input_chars: int | None = Field(default=None, ge=1)


class EmbeddingModelBinding(ModelBinding):
    kind: Literal["embedding"] = "embedding"
    dimensions: int = Field(ge=1, le=2048)
    video_fps: float = Field(ge=0.2, le=5.0)
    encoder_model: _NonEmptyString | None = None
    input_mode: Literal["multimodal", "text"] = "multimodal"
    query_instruction: _NonEmptyString | None = None


class AgentFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: _NonEmptyString
    stream: bool = Field(default=True, strict=True)
    temperature: float = Field(default=0.0, ge=0, le=2, strict=True, allow_inf_nan=False)
    supports_vision_input: bool = False
    model: _NonEmptyString
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, ge=256, le=262_144)
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None


class AgentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: _NonEmptyString
    stream: bool = Field(default=True, strict=True)
    temperature: float = Field(default=0.0, ge=0, le=2, strict=True, allow_inf_nan=False)
    supports_vision_input: bool = False
    model: _NonEmptyString
    max_output_tokens: int = Field(ge=256, le=262_144)
    request_timeout_seconds: float = Field(gt=0)
    fallback: AgentFallbackConfig | None = None


def _default_model_sources() -> dict[str, ModelSourceConfig]:
    return {
        _DEFAULT_PRIMARY_SOURCE: OpenAIModelSourceConfig(
            provider=DEEPSEEK_PROVIDER,
            profile=OPENAI_DEEPSEEK_PROFILE,
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        _DEFAULT_FALLBACK_SOURCE: ArkModelSourceConfig(),
    }


def _default_agent_model_config() -> AgentModelConfig:
    return AgentModelConfig(
        source=_DEFAULT_PRIMARY_SOURCE,
        model=_DEFAULT_AGENT_MODEL,
        max_output_tokens=8192,
        request_timeout_seconds=180,
        fallback=AgentFallbackConfig(
            source=_DEFAULT_FALLBACK_SOURCE,
            model=_DEFAULT_ARK_MODEL,
        ),
    )


class VideoFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    frame_fps: float = Field(ge=0.2, le=5.0)
    group_duration_seconds: int = Field(ge=1, le=120)
    max_frames_per_group: int = Field(ge=1, le=128)
    max_height: int = Field(ge=64, le=2160)
    deepseek: DeepSeekModelConfig | None = None


class VideoParseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: _NonEmptyString
    video_model: _NonEmptyString
    transcript_fps: float = Field(ge=0.2, le=5.0)
    video_fps: float = Field(ge=0.2, le=5.0)
    fallback: VideoFallbackConfig
    transcription_source: _NonEmptyString | None = None
    vision_source: _NonEmptyString | None = None
    transcription_timeout_seconds: float | None = Field(default=None, gt=0)
    vision_timeout_seconds: float | None = Field(default=None, gt=0)
    transcription_max_output_tokens: int | None = Field(default=None, ge=256, le=32768)
    vision_max_output_tokens: int | None = Field(default=None, ge=256, le=262144)
    vision_reasoning_effort: Literal["none", "low", "high", "max"] | None = None
    max_frames_per_window: int = Field(default=32, ge=1, le=600)


class MultimodalEmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: _NonEmptyString
    dimensions: int = Field(ge=1, le=2048)
    fps: float = Field(ge=0.2, le=5.0)


def _tracing_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("tracing base_url must be an HTTP(S) URL without credentials, query, or fragment")
    return value.rstrip("/")


class TracingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: Annotated[_NonEmptyString, AfterValidator(_tracing_base_url)] | None = None
    public_key_env: _NonEmptyString = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: _NonEmptyString = "LANGFUSE_SECRET_KEY"
    environment: _NonEmptyString = "development"
    release: _NonEmptyString | None = None
    export_timeout_seconds: float = Field(default=5, gt=0, le=60)
    close_timeout_seconds: float = Field(default=5, ge=0.2, le=60)
    queue_size: int = Field(default=2048, ge=1, le=100_000)


def _query_hook_url(value: str) -> str:
    parsed = urlsplit(value)
    parsed.port  # Validate malformed or out-of-range ports as well.
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("query hook URL must be HTTP(S) without credentials, query, or fragment")
    return value.rstrip("/")


class QueryHookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    rewriter_enabled: bool = Field(default=True, strict=True)
    router_endpoint: Annotated[_HttpsBaseUrl, AfterValidator(_query_hook_url)] = (
        "https://openrouter.ai/api/alpha/decisions"
    )
    router_model: _NonEmptyString = "typesafe/jev-1.14"
    router_api_key_env: _NonEmptyString = "OPENROUTER_JEV_API_KEY"
    router_timeout_seconds: float = Field(default=3, gt=0)
    rewriter_timeout_seconds: float = Field(default=4, gt=0)
    max_output_tokens: int = Field(default=256, ge=1, strict=True)
    history_message_limit: int = Field(default=6, ge=1, strict=True)
    history_max_chars: int = Field(default=2000, ge=1, strict=True)
    query_max_chars: int = Field(default=2000, ge=1, strict=True)
    total_timeout_seconds: float = Field(default=8, gt=0)


class WebSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["ddgs", "deepseek"] = "ddgs"
    backend: _NonEmptyString = "auto"
    base_url: _HttpsBaseUrl = "https://api.deepseek.com"
    api_key_env: _NonEmptyString = "DEEPSEEK_API_KEY"


ParseModelRole = Literal["vision", "transcription", "ocr", "embedding", "rerank"]
ModelRole = Literal["chat", "vision", "transcription", "ocr", "embedding", "rerank"]


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_version: Literal[1, 2] = 1
    supervisor_harness: Literal["deepagents", "codex"] = "deepagents"
    chat_default_model: _NonEmptyString | None = None
    model_roles: dict[ParseModelRole, _NonEmptyString] = Field(default_factory=dict)
    supervisor_choices: dict[_NonEmptyString, _NonEmptyString] = Field(default_factory=dict)

    model_sources: dict[_NonEmptyString, _ModelSourceConfigField] = Field(
        default_factory=_default_model_sources
    )
    agent_model: AgentModelConfig = Field(default_factory=_default_agent_model_config)
    video_parser: VideoParseConfig
    multimodal_embedding: MultimodalEmbeddingConfig
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    query_hook: QueryHookConfig = Field(default_factory=QueryHookConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    # Runtime tuning is validated by the Server, which owns its constraints.
    agent_runtime: dict[str, object] = Field(default_factory=dict)
    normalized_models: dict[str, EmbeddingModelBinding | ModelBinding] = Field(
        default_factory=dict
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_agent_config(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        return _normalize_gateway_payload(_normalize_model_bindings(value))

    @model_serializer(mode="wrap")
    def serialize_supervisor_selection(self, handler):
        values = handler(self)
        if "supervisor_harness" not in self.model_fields_set:
            values.pop("supervisor_harness", None)
        return values

    @model_validator(mode="after")
    def validate_agent_sources(self) -> GatewayConfig:
        speaker_threads = {
            source.cpu_threads for source in self.model_sources.values()
            if isinstance(source, CampPlusModelSourceConfig)
        }
        if len(speaker_threads) > 1:
            raise ValueError("campplus sources in one process must use the same cpu_threads")
        if self.agent_model.source not in self.model_sources:
            raise ValueError(
                f"agent_model.source {self.agent_model.source!r} is not present "
                "in model_sources"
            )
        fallback = self.agent_model.fallback
        if fallback is not None and fallback.source not in self.model_sources:
            raise ValueError(
                f"agent_model.fallback.source {fallback.source!r} is not present "
                "in model_sources"
            )
        if fallback is not None and fallback.source == self.agent_model.source and "agent" not in self.normalized_models:
            raise ValueError(
                "agent_model.fallback.source must differ from agent_model.source"
            )
        self._normalize_canonical_models()
        for ref, binding in self.normalized_models.items():
            self._validate_binding(binding, ref=ref)
        self._validate_supervisor_binding()
        for ref in self.supervisor_choices:
            if ref in {"default", "supervisor"} or len(ref) > 128 or ref not in self.normalized_models:
                raise ValueError(f"supervisor_choices must reference a configured alternative in models: {ref!r}")
            binding = self.normalized_models[ref]
            if binding.kind not in {None, "generative"}:
                raise ValueError(f"supervisor_choices.{ref} requires a generative model")
        return self

    def _validate_supervisor_binding(self, ref: str = "supervisor") -> None:
        binding = self.normalized_models.get(ref)
        if binding is None:
            # Legacy model-component configurations keep their existing
            # validation; the internal Deep Agents builder checks its source.
            if "supervisor_harness" not in self.model_fields_set:
                return
            binding = self.normalized_models["agent"]
        if isinstance(binding, EmbeddingModelBinding) or binding.kind not in {None, "generative"}:
            raise ValueError("models.supervisor must be a generative model binding")
        source = self.source(binding.source)
        if self.supervisor_harness == "codex":
            if not isinstance(source, CodexModelSourceConfig):
                raise ValueError("codex supervisor_harness requires a codex_app_server source")
            if binding.fallback is not None:
                raise ValueError("codex supervisor_harness does not support fallback")
            return
        if not isinstance(source, (OpenAIModelSourceConfig, ArkModelSourceConfig)):
            raise ValueError("deepagents supervisor_harness requires an OpenAI-compatible or Ark API source")
        if binding.fallback is not None:
            if binding.fallback.kind not in {None, "generative"}:
                raise ValueError("Supervisor fallback must be a generative model binding")
            if not isinstance(self.source(binding.fallback.source), ArkModelSourceConfig):
                raise ValueError("deepagents Supervisor fallback requires an Ark API source")
            if binding.fallback.fallback is not None:
                raise ValueError("Supervisor models support one configured fallback")

    def configured_supervisor_choices(
        self, *, discover: bool = False, environ: Mapping[str, str] | None = None,
    ) -> dict[str, ModelBinding]:
        """Preserve explicit chat bindings first; local model settings can discover additional compatible models."""
        choices = {}
        refs = dict.fromkeys([*self.supervisor_choices, *self.normalized_models] if discover else self.supervisor_choices)
        for ref in refs:
            raw = self.normalized_models[ref]
            if isinstance(raw, EmbeddingModelBinding) or raw.kind not in {None, "generative"}:
                continue
            if discover and ref in {"default", "supervisor"}:
                continue  # The running Supervisor already has its effective primary binding.
            source = self.source(raw.source)
            if isinstance(source, ChatAPISourceConfig):
                choices[ref] = self.resolve_model_binding(ref, environ=environ)
                continue
            if self.supervisor_harness == "codex":
                if not isinstance(source, CodexModelSourceConfig):
                    continue
            elif not isinstance(source, (OpenAIModelSourceConfig, ArkModelSourceConfig)):
                continue
            try:
                self._validate_supervisor_binding(ref)
            except ValueError:
                if ref in self.supervisor_choices:
                    raise
                # A parse binding can have a fallback that is unsuitable for chat.
                continue
            choices[ref] = self.resolve_model_binding(ref, environ=environ)
        return choices

    def chat_credentials_ready(self, binding: ModelBinding, *, environ: Mapping[str, str] | None = None) -> bool:
        environment = os.environ if environ is None else environ
        for part in (binding, binding.fallback):
            if part is None:
                continue
            source = self.source(part.source)
            if isinstance(source, ChatSourceCredentials) and source.encrypted_api_key:
                continue
            name = source_api_key_env_name(source)
            if name and not environment.get(name, "").strip():
                return False
            if not name and isinstance(source, (ArkModelSourceConfig, OpenAIModelSourceConfig)):
                return False
        return True

    def available_supervisor_choices(self, *, environ: Mapping[str, str] | None = None) -> dict[str, ModelBinding]:
        return {ref: binding for ref, binding in self.configured_supervisor_choices(environ=environ).items()
                if self.chat_credentials_ready(binding, environ=environ)}

    def source(self, name: str) -> ModelSourceConfig:
        try:
            return self.model_sources[name]
        except KeyError as error:
            raise ValueError(f"model source {name!r} is not present in model_sources") from error

    def _legacy_ark_source_name(self) -> str:
        implicit = ArkModelSourceConfig()
        if self.model_sources.get("ark_default") == implicit:
            return "ark_default"
        configured = self.model_sources.get(_LEGACY_PARSE_ARK_SOURCE)
        if configured is not None and configured != implicit:
            raise ValueError(f"{_LEGACY_PARSE_ARK_SOURCE} conflicts with legacy Ark credentials")
        self.model_sources[_LEGACY_PARSE_ARK_SOURCE] = implicit
        return _LEGACY_PARSE_ARK_SOURCE

    def _normalize_canonical_models(self) -> None:
        """Keep old configuration dialects at this boundary only."""
        bindings = dict(self.normalized_models)
        bindings.setdefault("agent", ModelBinding.model_validate({
            **self.agent_model.model_dump(), "kind": "generative",
        }))
        parser = self.video_parser
        for ref, model, source_name in (
            ("transcription", parser.transcript_model, parser.transcription_source),
            ("vision", parser.video_model, parser.vision_source),
        ):
            if ref in bindings:
                continue
            bindings[ref] = ModelBinding(
                source=source_name or self._legacy_ark_source_name(), model=model,
                kind="transcription" if ref == "transcription" else "generative",
                supports_vision_input=ref == "vision",
                request_timeout_seconds=getattr(parser, f"{ref}_timeout_seconds"),
                max_output_tokens=getattr(parser, f"{ref}_max_output_tokens"),
                reasoning_effort=parser.vision_reasoning_effort if ref == "vision" else None,
            )
        if "vision_fallback" not in bindings and parser.fallback.deepseek is not None:
            fallback = parser.fallback.deepseek
            source_name = fallback.vision_source
            if source_name is None:
                source_name = "legacy_vision_fallback"
                source = OpenAIModelSourceConfig(
                    provider=fallback.provider, profile=OPENAI_DEEPSEEK_PROFILE,
                    base_url=fallback.base_url, api_key_env=fallback.api_key_env,
                )
                configured = self.model_sources.get(source_name)
                if configured is not None and configured != source:
                    raise ValueError("legacy_vision_fallback conflicts with legacy fallback configuration")
                self.model_sources[source_name] = source
            bindings["vision_fallback"] = ModelBinding(
                source=source_name, model=fallback.model, kind="generative",
                supports_vision_input=True,
                request_timeout_seconds=fallback.request_timeout_seconds,
                max_output_tokens=fallback.max_output_tokens,
                reasoning_effort=fallback.reasoning_effort,
            )
        if "embedding" not in bindings:
            embedding = self.multimodal_embedding
            bindings["embedding"] = EmbeddingModelBinding(
                source=self._legacy_ark_source_name(), model=embedding.model,
                dimensions=embedding.dimensions, video_fps=embedding.fps,
            )
        if "rerank" not in bindings:
            bindings["rerank"] = bindings["agent"].model_copy(update={
                "kind": "rerank", "fallback": None,
            })
        vision = bindings["vision"]
        if "fallback" not in vision.model_fields_set:
            bindings["vision"] = vision.model_copy(update={
                "fallback": bindings.get("vision_fallback"),
            })
        self.normalized_models = bindings

    def _validate_binding(self, binding: ModelBinding, *, ref: str) -> None:
        source = self.source(binding.source)
        capabilities = adapter_capabilities(source.adapter)
        if isinstance(source, (CodexModelSourceConfig, ClaudeCodeModelSourceConfig)) and not binding.stream:
            raise ValueError(f"{ref}: {source.adapter} uses a native streaming transport and does not support stream=false")
        if isinstance(source, (CodexModelSourceConfig, ClaudeCodeModelSourceConfig)) and binding.temperature != 0:
            raise ValueError(f"{ref}: {source.adapter} does not support temperature configuration")
        kind = "embedding" if isinstance(binding, EmbeddingModelBinding) else binding.kind or "generative"
        if kind in {"embedding", "transcription", "speaker_embedding", "diarization"} and binding.fallback is not None:
            raise ValueError(f"{ref} {kind} does not support fallback")
        if kind == "embedding" and binding.supports_vision_input:
            raise ValueError(f"{ref} embedding does not use generative supports_vision_input")
        if isinstance(source, ChatAPISourceConfig) and kind in {"embedding", "transcription"} and source.protocol != "openai_chat":
            raise ValueError(f"{ref}: {source.protocol} does not support {kind}")
        if not getattr(capabilities, kind):
            raise ValueError(f"{ref} does not support adapter {source.adapter!r} for {kind}")
        if kind == "embedding" and not isinstance(binding, EmbeddingModelBinding):
            raise ValueError(f"{ref} embedding binding requires dimensions and video_fps")
        if isinstance(binding, EmbeddingModelBinding):
            if isinstance(source, (OllamaEmbeddingSourceConfig, LlamaCppEmbeddingSourceConfig, ChatAPISourceConfig)):
                if binding.input_mode != "text":
                    raise ValueError(f"{source.adapter} requires input_mode=text")
            elif binding.query_instruction is not None:
                raise ValueError("query_instruction is only supported by local text embedding adapters")
        if binding.supports_vision_input and capabilities.visual_input_mode is None:
            raise ValueError(f"adapter {source.adapter!r} does not support image input")
        supports_reasoning = capabilities.supports_reasoning_effort and not (
            isinstance(source, OpenAIModelSourceConfig) and source.profile == OPENAI_STANDARD_PROFILE
        )
        if not supports_reasoning and binding.reasoning_effort not in (None, "none"):
            raise ValueError(f"{source.adapter} does not support reasoning_effort")
        if not capabilities.supports_output_limit and binding.max_output_tokens is not None:
            raise ValueError(f"{source.adapter} does not support max_output_tokens")
        if kind in {"speaker_embedding", "diarization"} and any(getattr(binding, name) is not None for name in (
            "context_window_tokens", "context_safety_margin_tokens", "auto_compact_tokens",
            "compact_target_tokens", "max_input_chars",
        )):
            raise ValueError(f"{ref} {kind} does not use generative context budgets")
        if binding.fallback is not None:
            self._validate_binding(binding.fallback, ref=f"{ref}.fallback")

    def _complete_binding(self, binding: ModelBinding) -> ModelBinding:
        source = self.source(binding.source)
        capabilities = adapter_capabilities(source.adapter)
        runtime = self.agent_runtime
        values = binding.model_dump()
        if isinstance(source, OpenAIModelSourceConfig) and source.profile == OPENAI_KIMI_PROFILE:
            values["temperature"] = KIMI_FIXED_TEMPERATURE
        defaults = {
            "request_timeout_seconds": 600,
            "context_window_tokens": 128_000 if binding.supports_vision_input else runtime.get("context_window_tokens", 128_000),
            "context_safety_margin_tokens": 2048 if binding.supports_vision_input else runtime.get("context_safety_margin_tokens", 2048),
            "max_input_chars": 400_000 if binding.supports_vision_input else runtime.get("max_prompt_chars", 400_000),
        }
        if binding.kind in {"speaker_embedding", "diarization"}:
            return binding.model_copy(update={
                "request_timeout_seconds": binding.request_timeout_seconds or defaults["request_timeout_seconds"],
            })
        if (
            not isinstance(binding, EmbeddingModelBinding)
            and binding.kind != "transcription" and capabilities.supports_output_limit
        ):
            defaults["max_output_tokens"] = runtime.get("max_output_tokens", self.agent_model.max_output_tokens)
        for key, value in defaults.items():
            if values[key] is None:
                values[key] = value
        if binding.fallback is not None:
            values["fallback"] = self._complete_binding(binding.fallback).model_dump()
        output_reserve = values["max_output_tokens"] or 0
        fallback = values["fallback"]
        while fallback is not None:
            output_reserve = max(output_reserve, fallback["max_output_tokens"] or 0)
            fallback = fallback["fallback"]
        available = values["context_window_tokens"] - output_reserve - values["context_safety_margin_tokens"]
        if available <= 1:
            raise ValueError("context_window_tokens must exceed the output budget and safety margin")
        if values["auto_compact_tokens"] is None:
            values["auto_compact_tokens"] = (
                max(2, available * 9 // 10) if binding.supports_vision_input
                else runtime.get("auto_compact_tokens", max(2, available * 9 // 10))
            )
        if values["compact_target_tokens"] is None:
            values["compact_target_tokens"] = (
                max(1, available * 3 // 4) if binding.supports_vision_input
                else runtime.get("compact_target_tokens", max(1, available * 3 // 4))
            )
        if not 0 < values["compact_target_tokens"] < values["auto_compact_tokens"] < available:
            raise ValueError("context compaction budgets must satisfy target < trigger < available input tokens")
        return type(binding).model_validate(values)

    def resolve_model_binding(
        self, ref: str | ModelBinding, *, environ: Mapping[str, str] | None = None,
    ) -> ModelBinding:
        """Resolve configured references; frozen bindings never reapply environment overrides."""
        if isinstance(ref, ModelBinding):
            self._validate_binding(ref, ref="frozen model")
            return self._complete_binding(ref)
        aliases = {"query": "agent", "evaluator": "agent", "chat": "agent", "supervisor": "agent"}
        configured_ref = ref if ref in self.normalized_models else aliases.get(ref, ref)
        if configured_ref == "agent_fallback" and ref not in self.normalized_models:
            binding = self.normalized_models["agent"].fallback
            if binding is None:
                raise ValueError("agent fallback is not configured")
        else:
            try:
                binding = self.normalized_models[configured_ref]
            except KeyError as error:
                raise ValueError(f"model binding {ref!r} is not configured") from error
        environment = os.environ if environ is None else environ
        model_keys = {
            "transcription": ("SEEKINO_TRANSCRIPT_MODEL",),
            "vision": ("SEEKINO_VIDEO_MODEL",),
            "agent": ("SEEKINO_AGENT_CHAT_MODEL", "SEEKINO_DEEPSEEK_CHAT_MODEL"),
            "chat": ("SEEKINO_AGENT_CHAT_MODEL", "SEEKINO_DEEPSEEK_CHAT_MODEL"),
            "query": ("SEEKINO_AGENT_QUERY_MODEL",),
            "evaluator": ("SEEKINO_AGENT_EVALUATOR_MODEL",),
            "agent_fallback": ("SEEKINO_AGENT_FALLBACK_MODEL",),
            "rerank": ("SEEKINO_RERANK_MODEL",),
        }
        keys = () if ref in self.model_roles else model_keys.get(ref, ())
        selected = next((environment[key].strip() for key in keys if environment.get(key, "").strip()), None)
        updates = {"model": selected} if selected else {}
        if configured_ref == "agent" and binding.fallback is not None:
            fallback_model = environment.get("SEEKINO_AGENT_FALLBACK_MODEL", "").strip()
            if fallback_model:
                updates["fallback"] = binding.fallback.model_copy(update={"model": fallback_model})
        if configured_ref == "vision":
            enabled = environment.get("SEEKINO_FALLBACK_ENABLED")
            fallback_enabled = self.video_parser.fallback.enabled
            if enabled is not None:
                normalized = enabled.strip().lower()
                if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                    raise ValueError("SEEKINO_FALLBACK_ENABLED must be a boolean value")
                fallback_enabled = normalized in {"1", "true", "yes", "on"}
            updates["fallback"] = (
                binding.fallback if "fallback" in binding.model_fields_set
                else self.normalized_models.get("vision_fallback")
            ) if fallback_enabled else None
        binding = binding.model_copy(update=updates)
        self._validate_binding(binding, ref=ref)
        return self._complete_binding(binding)

    def resolve_embedding_binding(
        self, ref: str | EmbeddingModelBinding = "embedding", *,
        environ: Mapping[str, str] | None = None,
    ) -> EmbeddingModelBinding:
        binding = self.resolve_model_binding(ref, environ=environ)
        if not isinstance(binding, EmbeddingModelBinding):
            raise ValueError(f"model binding {ref!r} is not an embedding binding")
        environment = os.environ if environ is None else environ
        selected = environment.get("SEEKINO_VIDEO_EMBEDDING_MODEL", "").strip() if ref == "embedding" and "embedding" not in self.model_roles else ""
        fps = environment.get("SEEKINO_VIDEO_EMBEDDING_FPS", "").strip() if isinstance(ref, str) and ref == "embedding" else ""
        # Import only after config types are initialized: model_sources consumes them.
        from gateway.model_sources import ModelReference, ModelReferenceType
        configured = self.normalized_models["embedding"]
        default_encoder = configured.encoder_model or configured.model
        if ModelReferenceType.for_model(default_encoder) == ModelReferenceType.ENDPOINT:
            default_encoder = None
        reference = ModelReference.resolve(
            selected or binding.model, encoder_model=binding.encoder_model,
            default_encoder_model=default_encoder,
        )
        values = binding.model_dump()
        values.update(
            model=reference.request_model,
            encoder_model=reference.model if reference.type == ModelReferenceType.ENDPOINT else None,
            video_fps=float(fps) if fps else binding.video_fps,
        )
        return EmbeddingModelBinding.model_validate(values)

    def stage_source(self, stage: str) -> ModelSourceConfig:
        names = {
            "transcription": self.video_parser.transcription_source,
            "vision": self.video_parser.vision_source,
        }
        name = names[stage]
        # Old parsing/embedding never used Agent sources, even a custom ark_default.
        if name is None:
            return ArkModelSourceConfig()
        if name not in self.model_sources:
            raise ValueError(f"{stage}.source {name!r} is not present in model_sources")
        return self.model_sources[name]

    def selected_agent_sources(self) -> tuple[ModelSourceConfig, ...]:
        """Return only sources referenced by the active Agent configuration."""
        binding = self.normalized_models.get("agent")
        if binding is not None:
            selected = []
            while binding is not None:
                source = self.source(binding.source)
                if source not in selected:
                    selected.append(source)
                binding = binding.fallback
            return tuple(selected)
        selected = [self.model_sources[self.agent_model.source]]
        fallback = self.agent_model.fallback
        if fallback is not None and fallback.source != self.agent_model.source:
            selected.append(self.model_sources[fallback.source])
        return tuple(selected)


def source_api_key_env_name(source: ModelSourceConfig) -> str | None:
    if isinstance(
        source,
        (
            OpenAIModelSourceConfig,
            ChatAPISourceConfig,
            OllamaEmbeddingSourceConfig,
            ArkModelSourceConfig,
            OpenAIAudioSourceConfig,
            OpenAIVisionSourceConfig,
        ),
    ):
        return source.api_key_env
    if (
        isinstance(source, ClaudeCodeModelSourceConfig)
        and source.auth_mode == "api_key"
    ):
        return source.api_key_env
    return None


def resolve_source_api_key(
    source: ModelSourceConfig, *, environ: Mapping[str, str] | None = None,
) -> str | None:
    if isinstance(source, ChatSourceCredentials) and source.encrypted_api_key:
        return source.saved_key()
    name = source_api_key_env_name(source)
    if name is None:
        return None
    environment = os.environ if environ is None else environ
    key = environment.get(name, "").strip()
    if not key:
        raise RuntimeError(f"model source requires API key environment variable {name!r}")
    return key


def agent_api_key_env_names(config: GatewayConfig) -> tuple[str, ...]:
    """Return credentials used by the selected primary/fallback sources only."""

    names = {
        name
        for source in config.selected_agent_sources()
        if (name := source_api_key_env_name(source)) is not None
    }
    return tuple(sorted(names))


def required_api_key_env_names(config: GatewayConfig) -> tuple[str, ...]:
    """Credentials for active sources, including the enabled parse fallback."""
    names = set(agent_api_key_env_names(config))
    supervisor = config.normalized_models.get("supervisor")
    while supervisor is not None:
        name = source_api_key_env_name(config.source(supervisor.source))
        if name:
            names.add(name)
        supervisor = supervisor.fallback
    for ref in ("transcription", "vision", "embedding", "rerank"):
        binding = config.normalized_models[ref]
        while binding is not None:
            name = source_api_key_env_name(config.source(binding.source))
            if name:
                names.add(name)
            if ref == "vision" and not config.video_parser.fallback.enabled:
                break
            binding = binding.fallback
    return tuple(sorted(names))


def _normalize_model_bindings(value: Mapping[object, object]) -> dict[object, object]:
    """Translate new YAML at the boundary; existing consumers keep their DTOs."""
    result = dict(value)
    if "models" not in result:
        if result.get("model_roles") and "normalized_models" not in result:
            raise ValueError("model_roles requires version 2 model bindings")
        return result
    if result.get("config_version") != 2:
        raise ValueError("models requires config_version: 2")
    models = result.pop("models")
    if not isinstance(models, Mapping):
        raise ValueError("models must be a mapping")
    if "model_sources" not in result:
        raise ValueError("models requires model_sources")
    if "normalized_models" in result:
        raise ValueError("configure only models or normalized_models")
    bindings = {}
    for ref, raw in models.items():
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("model references must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise ValueError(f"models.{ref} must be a model binding")
        values = dict(raw)
        if ref == "transcription":
            values.setdefault("kind", "transcription")
        elif ref == "rerank":
            values.setdefault("kind", "rerank")
        if ref in {"vision", "vision_fallback"}:
            # Preserve pre-capability YAML; arbitrary references must declare images.
            values.setdefault("supports_vision_input", True)
        binding_type = (
            EmbeddingModelBinding if values.get("kind") == "embedding" or ref == "embedding"
            else ModelBinding
        )
        bindings[ref] = binding_type.model_validate(values)
    roles = result.get("model_roles", {})
    if not isinstance(roles, Mapping):
        raise ValueError("model_roles must be a mapping")
    registered = bindings.copy()
    for role, ref in roles.items():
        if role not in {"vision", "transcription", "ocr", "embedding", "rerank"} or not isinstance(ref, str) or ref not in registered:
            raise ValueError("用途必须引用一个已注册的模型")
        selected = registered[ref]
        kind = selected.kind or "generative"
        if role in {"vision", "ocr"}:
            if kind != "generative" or not selected.supports_vision_input:
                raise ValueError("画面解析和 OCR 需要支持图像输入的生成模型")
            if role == "vision" and ref != "vision" and "fallback" not in selected.model_fields_set:
                # A replacement registration inherits only its own fallback.
                selected = selected.model_copy(update={"fallback": None})
        elif role == "rerank" and kind in {"generative", "rerank"}:
            selected = selected.model_copy(update={"kind": "rerank"})
        elif kind != role:
            raise ValueError("模型类型不支持所选用途")
        bindings[role] = selected
    result["normalized_models"] = bindings
    if "agent" in models:
        if "agent_model" in result or "agent_chat" in result:
            raise ValueError(
                "configure only models.agent or legacy agent_model/agent_chat"
            )
        binding = bindings["agent"]
        # These DTOs remain a projection for callers migrating from the old API.
        fields = set(AgentModelConfig.model_fields)
        projected = {key: value for key, value in binding.model_dump().items() if key in fields}
        projected["max_output_tokens"] = binding.max_output_tokens or 8192
        projected["request_timeout_seconds"] = binding.request_timeout_seconds or 180
        if binding.fallback is not None:
            projected["fallback"] = {
                key: value for key, value in binding.fallback.model_dump().items()
                if key in AgentFallbackConfig.model_fields
            }
        result["agent_model"] = projected
    if not isinstance(result.get("video_parser", {}), Mapping):
        raise ValueError("video_parser must be a mapping")
    parser = dict(result.get("video_parser", {}))
    for stage, legacy in (("transcription", "transcript"), ("vision", "video")):
        if stage not in bindings:
            continue
        binding = bindings[stage]
        if any(
            key in parser
            for key in (
                f"{legacy}_model",
                f"{stage}_source",
                f"{stage}_timeout_seconds",
                f"{stage}_max_output_tokens",
                f"{stage}_reasoning_effort",
            )
        ):
            raise ValueError(
                f"configure only models.{stage} or legacy video_parser.{legacy}_model"
            )
        parser[f"{legacy}_model"] = binding.model
        parser[f"{stage}_source"] = binding.source
        parser[f"{stage}_timeout_seconds"] = binding.request_timeout_seconds
        parser[f"{stage}_max_output_tokens"] = binding.max_output_tokens
        if stage == "vision":
            parser["vision_reasoning_effort"] = binding.reasoning_effort
    if "vision_fallback" in models:
        if not isinstance(parser.get("fallback", {}), Mapping):
            raise ValueError("video_parser.fallback must be a mapping")
        fallback = dict(parser.get("fallback", {}))
        if "deepseek" in fallback:
            raise ValueError(
                "configure only models.vision_fallback or video_parser.fallback.deepseek"
            )
        binding = bindings["vision_fallback"]
        sources = result["model_sources"]
        source = sources.get(binding.source) if isinstance(sources, Mapping) else None
        if not isinstance(source, Mapping):
            raise ValueError(
                "models.vision_fallback.source is not present in model_sources"
            )
        adapter = source.get("adapter")
        # Only represent compatible legacy dialects; canonical models carry every adapter.
        if adapter == "openai_vision" or (
            adapter == "openai" and source.get("profile") == "deepseek"
        ):
            fallback["deepseek"] = {
                "provider": source.get("provider"),
                "protocol": "openai_chat" if adapter == "openai_vision" else "deepseek_responses",
                "vision_source": binding.source,
                "model": binding.model,
                "base_url": source.get("base_url"),
                "api_key_env": source.get("api_key_env"),
                "max_output_tokens": binding.max_output_tokens or 8192,
                "request_timeout_seconds": binding.request_timeout_seconds or 1200,
                "reasoning_effort": binding.reasoning_effort or "none",
            }
        else:
            fallback["deepseek"] = None
        parser["fallback"] = fallback
    if "embedding" in bindings:
        if "multimodal_embedding" in result:
            raise ValueError("configure only models.embedding or legacy multimodal_embedding")
        embedding = bindings["embedding"]
        result["multimodal_embedding"] = {
            "model": embedding.model, "dimensions": embedding.dimensions,
            "fps": embedding.video_fps,
        }
    result["video_parser"] = parser
    return result


def _normalize_gateway_payload(value: Mapping[object, object]) -> dict[object, object]:
    normalized = dict(value)
    has_agent_model = "agent_model" in normalized
    has_agent_chat = "agent_chat" in normalized
    if has_agent_model and has_agent_chat:
        raise ValueError("configure only agent_model; agent_chat is a legacy alias")

    agent_key = "agent_model" if has_agent_model else "agent_chat"
    raw_agent = normalized.get(agent_key)
    if raw_agent is not None and not isinstance(raw_agent, Mapping):
        return normalized

    has_sources = "model_sources" in normalized
    uses_legacy_shape = isinstance(raw_agent, Mapping) and "provider" in raw_agent
    if has_sources and uses_legacy_shape:
        raise ValueError(
            "agent_model.provider cannot be combined with model_sources; "
            "use agent_model.source"
        )

    if has_sources:
        if has_agent_chat:
            normalized["agent_model"] = normalized.pop("agent_chat")
        return normalized

    if isinstance(raw_agent, Mapping) and "source" in raw_agent:
        raise ValueError(
            "model_sources is required when agent_model uses a source reference"
        )

    transcript_model = _legacy_transcript_model(normalized)
    sources, agent_model = _legacy_agent_config(raw_agent, transcript_model)
    normalized["model_sources"] = sources
    normalized["agent_model"] = agent_model
    if has_agent_chat:
        normalized.pop("agent_chat", None)
    return normalized


def _legacy_transcript_model(payload: Mapping[object, object]) -> str:
    video_parser = payload.get("video_parser")
    if isinstance(video_parser, Mapping):
        transcript_model = video_parser.get("transcript_model")
        if isinstance(transcript_model, str) and transcript_model.strip():
            return transcript_model.strip()
    return _DEFAULT_ARK_MODEL


def _legacy_agent_config(
    raw_agent: Mapping[object, object] | None,
    transcript_model: str,
) -> tuple[dict[str, object], dict[str, object]]:
    legacy = dict(raw_agent or {})
    provider = legacy.pop("provider", DEEPSEEK_PROVIDER)
    if provider == "claude-code":
        provider = CLAUDE_CODE_PROVIDER

    model = legacy.pop("model", _DEFAULT_AGENT_MODEL)
    max_output_tokens = legacy.pop("max_output_tokens", 8192)
    request_timeout_seconds = legacy.pop("request_timeout_seconds", 180)
    reasoning_effort = legacy.pop("reasoning_effort", "none")
    if reasoning_effort != "none":
        raise ValueError(
            "legacy agent_model.reasoning_effort only supports 'none'; "
            "configure per-role reasoning in agent_runtime"
        )
    primary_name = "legacy_agent"
    fallback: dict[str, object] | None = None

    if provider == DEEPSEEK_PROVIDER:
        primary = {
            "adapter": OPENAI_ADAPTER,
            "provider": DEEPSEEK_PROVIDER,
            "profile": OPENAI_DEEPSEEK_PROFILE,
            "base_url": legacy.pop("base_url", "https://api.deepseek.com"),
            "api_key_env": legacy.pop("api_key_env", "DEEPSEEK_API_KEY"),
            **legacy,
        }
        sources: dict[str, object] = {
            primary_name: primary,
            "legacy_ark_fallback": {
                "adapter": ARK_ADAPTER,
                "provider": ARK_PROVIDER,
                "api_key_env": "ARK_API_KEY",
            },
        }
        fallback = {
            "source": "legacy_ark_fallback",
            "model": transcript_model,
        }
    elif provider == CODEX_PROVIDER:
        sources = {
            primary_name: {
                "adapter": CODEX_APP_SERVER_ADAPTER,
                "provider": CODEX_PROVIDER,
                **legacy,
            }
        }
    elif provider == CLAUDE_CODE_PROVIDER:
        sources = {
            primary_name: {
                "adapter": CLAUDE_SDK_ADAPTER,
                "provider": CLAUDE_CODE_PROVIDER,
                **legacy,
            }
        }
    else:
        # Preserve the unknown value so Pydantic reports it at the boundary.
        sources = {
            primary_name: {
                "adapter": provider,
                "provider": provider,
                **legacy,
            }
        }

    agent_model: dict[str, object] = {
        "source": primary_name,
        "model": model,
        "max_output_tokens": max_output_tokens,
        "request_timeout_seconds": request_timeout_seconds,
    }
    if fallback is not None:
        agent_model["fallback"] = fallback
    return sources, agent_model


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    configured_path = config_path or os.getenv("SEEKINO_MODEL_CONFIG")
    return Path(configured_path or _DEFAULT_CONFIG_PATH).expanduser().resolve()


def resolve_agent_runtime_path(model_path: Path) -> Path | None:
    configured = os.getenv("SEEKINO_AGENT_RUNTIME_CONFIG")
    if not configured and os.getenv("SEEKINO_AUTH_MODE", "local").strip().lower() != "cloud":
        return None
    path = Path(configured).expanduser().resolve() if configured else model_path.with_name("agent-runtime.yaml")
    if path == model_path:
        raise ValueError("Agent runtime and model configuration must use separate files")
    return path


def standalone_agent_runtime(document: object) -> dict:
    if (
        not isinstance(document, dict)
        or set(document) != {"agent_runtime"}
        or not isinstance(document["agent_runtime"], dict)
    ):
        raise ValueError("独立 Agent 设置文件只接受 agent_runtime 映射")
    return document["agent_runtime"]


def load_config(config_path: str | Path | None = None) -> GatewayConfig:
    path = resolve_config_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"gateway config does not exist: {path}")

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in gateway config: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"gateway config must be a YAML mapping: {path}")
    return config_from_document(payload, path)


def config_from_document(payload: Mapping, path: Path) -> GatewayConfig:
    runtime_path = resolve_agent_runtime_path(path)
    if runtime_path is not None and runtime_path.is_file():
        runtime = standalone_agent_runtime(yaml.safe_load(runtime_path.read_bytes()))
        # Model bindings inherit context and output values from the same Agent settings as Chat.
        payload = {**payload, "agent_runtime": runtime}
    config = GatewayConfig.model_validate(payload)
    for name, source in config.model_sources.items():
        if isinstance(source, ChatSourceCredentials) and source.encrypted_api_key:
            source._config_path = path
            source._source_name = name
    return config
