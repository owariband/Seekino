from __future__ import annotations

import os
from typing import Literal

from pydantic import Field, model_validator

from engine.parse.runtime import VideoParseRuntimeConfig
from gateway.config import GatewayConfig

from ..schema import ContractModel
from .media import ImageUploadConfig
from .upload import UploadServiceConfig


class ParseWindowConfig(ContractModel):
    chunk_duration_seconds: int = Field(ge=1)
    context_overlap_seconds: int = Field(ge=0)
    analysis_max_height: int = Field(ge=1)
    canonical_max_height: int = Field(ge=1)


class ParseASRConfig(ContractModel):
    mode: Literal["lite", "none"]
    model: str = Field(min_length=1)
    fps: float = Field(ge=0.2, le=5.0)
    max_output_tokens: int | None = Field(default=None, ge=256, le=32_768)
    concurrency: int = Field(ge=1)


class ParseVisionConfig(ContractModel):
    model: str = Field(min_length=1)
    input_mode: Literal["video", "ordered_frames"] = "video"
    fps: float = Field(ge=0.2, le=5.0)
    max_frames_per_window: int | None = Field(default=None, ge=1, le=600)
    max_output_tokens: int | None = Field(default=None, ge=256, le=32_768)

    @model_validator(mode="after")
    def validate_frame_mode(self) -> "ParseVisionConfig":
        if (self.input_mode == "ordered_frames") != (
            self.max_frames_per_window is not None
        ):
            raise ValueError(
                "max_frames_per_window is required only for ordered_frames"
            )
        return self


class ParseContextConfig(ContractModel):
    max_events: int = Field(ge=1)
    max_chars: int = Field(ge=256)
    background_text_enabled: bool = True
    background_text_max_chars: int | None = Field(default=None, ge=256, le=200_000)
    background_images_enabled: bool = True
    max_background_images: int | None = Field(default=None, ge=1, le=64)
    background_image_max_edge: int = Field(ge=1)


class ParseFallbackConfig(ContractModel):
    enabled: bool
    model: str = Field(min_length=1)
    frame_fps: float = Field(ge=0.2, le=5.0)
    group_duration_seconds: int = Field(ge=1, le=120)
    max_frames_per_group: int = Field(ge=1, le=128)
    max_height: int = Field(ge=64, le=2160)
    reasoning_effort: Literal["none", "low", "high", "max"]
    max_output_tokens: int = Field(ge=256, le=32_768)
    request_timeout_seconds: float = Field(gt=0)


class ParseEmbeddingConfig(ContractModel):
    model: str = Field(min_length=1)
    dimensions: int = Field(ge=1, le=2048)
    video_fps: float = Field(ge=0.2, le=5.0)


class ParseExecutionConfig(ContractModel):
    request_timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1)


class ParseConfig(ContractModel):
    version: Literal[2] = 2
    window: ParseWindowConfig
    asr: ParseASRConfig
    vision: ParseVisionConfig
    context: ParseContextConfig
    fallback: ParseFallbackConfig
    embedding: ParseEmbeddingConfig
    execution: ParseExecutionConfig

    def require_active_index(
        self, *, embedding_model: str, dimensions: int, image_max_edge: int
    ) -> None:
        if self.embedding.model != embedding_model or self.embedding.dimensions != dimensions:
            raise ValueError(
                "embedding model and dimensions must match the active vector index"
            )
        if self.context.background_image_max_edge > image_max_edge:
            raise ValueError(
                "background_image_max_edge cannot exceed the stored model image size"
            )

    @classmethod
    def from_defaults(
        cls,
        *,
        runtime: VideoParseRuntimeConfig,
        gateway: GatewayConfig,
        upload: UploadServiceConfig,
        images: ImageUploadConfig,
        embedding_model: str,
        embedding_dimensions: int,
        embedding_fps: float,
    ) -> "ParseConfig":
        parser = gateway.video_parser
        fallback = parser.fallback
        deepseek = fallback.deepseek
        return cls(
            window=ParseWindowConfig(
                chunk_duration_seconds=runtime.chunk_duration_seconds,
                context_overlap_seconds=runtime.context_overlap_seconds,
                analysis_max_height=runtime.max_height,
                canonical_max_height=runtime.max_height,
            ),
            asr=ParseASRConfig(
                mode=runtime.asr_mode,
                model=runtime.transcript_model or os.getenv("ANIVAG_TRANSCRIPT_MODEL") or parser.transcript_model,
                fps=runtime.transcript_fps or float(os.getenv("ANIVAG_TRANSCRIPT_FPS") or parser.transcript_fps),
                concurrency=runtime.asr_concurrency,
            ),
            vision=ParseVisionConfig(
                model=runtime.video_model or os.getenv("ANIVAG_VIDEO_MODEL") or parser.video_model,
                fps=runtime.video_fps or float(os.getenv("ANIVAG_VIDEO_FPS") or parser.video_fps),
            ),
            context=ParseContextConfig(
                max_events=runtime.max_context_events,
                max_chars=runtime.max_context_chars,
                background_image_max_edge=images.model_max_edge,
            ),
            fallback=ParseFallbackConfig(
                enabled=(
                    runtime.fallback_enabled
                    if runtime.fallback_enabled is not None
                    else _environment_bool("ANIVAG_FALLBACK_ENABLED", fallback.enabled)
                ),
                model=deepseek.model,
                frame_fps=fallback.frame_fps,
                group_duration_seconds=fallback.group_duration_seconds,
                max_frames_per_group=fallback.max_frames_per_group,
                max_height=fallback.max_height,
                reasoning_effort=deepseek.reasoning_effort,
                max_output_tokens=deepseek.max_output_tokens,
                request_timeout_seconds=deepseek.request_timeout_seconds,
            ),
            embedding=ParseEmbeddingConfig(
                model=embedding_model,
                dimensions=embedding_dimensions,
                video_fps=embedding_fps,
            ),
            execution=ParseExecutionConfig(
                request_timeout_seconds=runtime.request_timeout_seconds,
                max_attempts=upload.max_job_attempts,
            ),
        )


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")
