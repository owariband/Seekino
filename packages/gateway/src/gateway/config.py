from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config.yaml"


class LocalVideoFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: Literal["ollama", "llama_cpp"] = "ollama"
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    frame_fps: float = Field(ge=0.2, le=5.0)
    max_frames: int = Field(ge=1, le=128)
    max_height: int = Field(ge=64, le=2160)
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class FallbackRefinerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["ark"] = "ark"
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class DeepSeekVisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek"] = "deepseek"
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class VideoParseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_model: str = Field(min_length=1)
    video_model: str = Field(min_length=1)
    transcript_fps: float = Field(ge=0.2, le=5.0)
    video_fps: float = Field(ge=0.2, le=5.0)
    local_fallback: LocalVideoFallbackConfig
    deepseek_vision: DeepSeekVisionConfig | None = None
    fallback_refiner: FallbackRefinerConfig


class MultimodalEmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    dimensions: int = Field(ge=1, le=2048)
    fps: float = Field(ge=0.2, le=5.0)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep the YAML key stable for the existing CLI and local run configuration.
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
