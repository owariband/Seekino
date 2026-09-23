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
    StringConstraints,
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
    OPENAI_ADAPTER,
    OPENAI_DEEPSEEK_PROFILE,
    OPENAI_STANDARD_PROFILE,
)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_DEFAULT_PRIMARY_SOURCE = "deepseek_api"
_DEFAULT_FALLBACK_SOURCE = "ark_default"
_DEFAULT_AGENT_MODEL = "deepseek-flash"
_DEFAULT_ARK_MODEL = "doubao-seed-2-0-lite-260428"


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
_NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class DeepSeekModelConfig(BaseModel):
    """Parse fallback configuration, independent from Chat Agent sources."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek"] = "deepseek"
    model: _NonEmptyString
    base_url: _HttpsBaseUrl
    api_key_env: _NonEmptyString
    reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class OpenAIModelSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[OPENAI_ADAPTER] = OPENAI_ADAPTER
    provider: _NonEmptyString
    profile: Literal[OPENAI_STANDARD_PROFILE, OPENAI_DEEPSEEK_PROFILE] = (
        OPENAI_STANDARD_PROFILE
    )
    base_url: _HttpsBaseUrl
    api_key_env: _NonEmptyString


class ArkModelSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal[ARK_ADAPTER] = ARK_ADAPTER
    provider: Literal[ARK_PROVIDER] = ARK_PROVIDER
    api_key_env: _NonEmptyString = "ARK_API_KEY"


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
    | ArkModelSourceConfig
    | CodexModelSourceConfig
    | ClaudeCodeModelSourceConfig
)
_ModelSourceConfigField = Annotated[
    ModelSourceConfig,
    Field(discriminator="adapter"),
]


class AgentFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: _NonEmptyString
    model: _NonEmptyString


class AgentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: _NonEmptyString
    model: _NonEmptyString
    max_output_tokens: int = Field(ge=256, le=32_768)
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
    deepseek: DeepSeekModelConfig


class VideoParseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: _NonEmptyString
    video_model: _NonEmptyString
    transcript_fps: float = Field(ge=0.2, le=5.0)
    video_fps: float = Field(ge=0.2, le=5.0)
    fallback: VideoFallbackConfig


class MultimodalEmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: _NonEmptyString
    dimensions: int = Field(ge=1, le=2048)
    fps: float = Field(ge=0.2, le=5.0)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_sources: dict[_NonEmptyString, _ModelSourceConfigField] = Field(
        default_factory=_default_model_sources
    )
    agent_model: AgentModelConfig = Field(default_factory=_default_agent_model_config)
    video_parser: VideoParseConfig
    multimodal_embedding: MultimodalEmbeddingConfig
    # Runtime tuning is validated by the Server, which owns its constraints.
    agent_runtime: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_agent_config(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        return _normalize_gateway_payload(value)

    @model_validator(mode="after")
    def validate_agent_sources(self) -> GatewayConfig:
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
        if fallback is not None and fallback.source == self.agent_model.source:
            raise ValueError(
                "agent_model.fallback.source must differ from agent_model.source"
            )
        return self

    def selected_agent_sources(self) -> tuple[ModelSourceConfig, ...]:
        """Return only sources referenced by the active Agent configuration."""

        selected = [self.model_sources[self.agent_model.source]]
        fallback = self.agent_model.fallback
        if fallback is not None and fallback.source != self.agent_model.source:
            selected.append(self.model_sources[fallback.source])
        return tuple(selected)


def source_api_key_env_name(source: ModelSourceConfig) -> str | None:
    if isinstance(source, (OpenAIModelSourceConfig, ArkModelSourceConfig)):
        return source.api_key_env
    if (
        isinstance(source, ClaudeCodeModelSourceConfig)
        and source.auth_mode == "api_key"
    ):
        return source.api_key_env
    return None


def agent_api_key_env_names(config: GatewayConfig) -> tuple[str, ...]:
    """Return credentials used by the selected primary/fallback sources only."""

    names = {
        name
        for source in config.selected_agent_sources()
        if (name := source_api_key_env_name(source)) is not None
    }
    return tuple(sorted(names))


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
    configured_path = config_path or os.getenv("ANIVAG_MODEL_CONFIG")
    return Path(configured_path or _DEFAULT_CONFIG_PATH).expanduser().resolve()


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
    return GatewayConfig.model_validate(payload)
