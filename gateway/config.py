from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator, AliasChoices, BaseModel, ConfigDict, Field, StringConstraints,
)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


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
            "DeepSeek base_url must be an HTTPS URL without credentials, "
            "query, or fragment"
        )
    return value.rstrip("/")


_HttpsBaseUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_https_base_url),
]


class DeepSeekModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek"] = "deepseek"
    model: str = Field(min_length=1)
    base_url: _HttpsBaseUrl
    api_key_env: str = Field(min_length=1)
    reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class AgentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek"] = "deepseek"
    model: str = Field(min_length=1)
    base_url: _HttpsBaseUrl
    api_key_env: str = Field(min_length=1)
    reasoning_effort: Literal["none"] = "none"
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


def _default_agent_model_config() -> AgentModelConfig:
    return AgentModelConfig(
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        max_output_tokens=8192,
        request_timeout_seconds=180,
    )


class VideoFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    frame_fps: float = Field(ge=0.2, le=5.0)
    group_duration_ms: int = Field(ge=1_000, le=120_000)
    max_frames_per_group: int = Field(ge=1, le=128)
    max_height: int = Field(ge=64, le=2160)
    deepseek: DeepSeekModelConfig


class VideoParseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    video_model: str = Field(min_length=1)
    transcript_fps: float = Field(ge=0.2, le=5.0)
    video_fps: float = Field(ge=0.2, le=5.0)
    fallback: VideoFallbackConfig


class MultimodalEmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    dimensions: int = Field(ge=1, le=2048)
    fps: float = Field(ge=0.2, le=5.0)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep parse-only custom configs compatible; Server startup still validates
    # the resolved Agent API key before opening database or Qdrant resources.
    agent_model: AgentModelConfig = Field(
        default_factory=_default_agent_model_config,
        validation_alias=AliasChoices("agent_model", "agent_chat"),
    )
    # Keep the parser YAML key stable for the existing CLI and local run configuration.
    video_parser: VideoParseConfig
    multimodal_embedding: MultimodalEmbeddingConfig


def load_config(config_path: str | Path | None = None) -> GatewayConfig:
    configured_path = config_path or os.getenv("ANIVAG_MODEL_CONFIG")
    path = Path(configured_path or _DEFAULT_CONFIG_PATH).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"gateway config does not exist: {path}")

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in gateway config: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"gateway config must be a YAML mapping: {path}")
    return GatewayConfig.model_validate(payload)
