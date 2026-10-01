from __future__ import annotations

import math
import os
from typing import Literal

from pydantic import Field, model_validator

from engine.parsev1.runtime import VideoParseRuntimeConfig
from agent.video_parse_v2.input import (
    ASRModelConfig as VideoParseV2ASRModelConfig,
    EmbeddingModelConfig as VideoParseV2EmbeddingModelConfig,
    ExecutionConfig as VideoParseV2ExecutionConfig,
    FallbackModelConfig as VideoParseV2FallbackModelConfig,
    GenerativeModelConfig as VideoParseV2GenerativeModelConfig,
    ModelSnapshot as VideoParseV2ModelSnapshot,
    ParseConfig as VideoParseV2Config,
    VisualInputConfig as VideoParseV2VisualInputConfig,
)
from gateway.config import (
    ArkModelSourceConfig,
    GatewayConfig,
    OpenAIModelSourceConfig,
    OpenAIVisionSourceConfig,
)

from ..schema import ContractModel
from .media import ImageUploadConfig
from .upload import UploadServiceConfig


class ParseWindowConfig(ContractModel):
    chunk_duration_seconds: int = Field(ge=1)
    context_overlap_seconds: int = Field(ge=0)
    analysis_max_height: int = Field(ge=1)
    canonical_max_height: int = Field(ge=1)


class ParseASRConfig(ContractModel):
    source: str = Field(default="ark_default", min_length=1)
    mode: Literal["lite", "none"]
    model: str = Field(min_length=1)
    fps: float = Field(ge=0.2, le=5.0)
    max_output_tokens: int | None = Field(default=None, ge=256, le=32_768)
    concurrency: int = Field(ge=1)


class ParseVisionConfig(ContractModel):
    source: str = Field(default="ark_default", min_length=1)
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
    source: str = Field(default="deepseek_api", min_length=1)
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
    source: str = Field(default="ark_default", min_length=1)
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

    def to_video_parse_v2(self) -> VideoParseV2Config:
        """Freeze this effective server profile into the V2 task contract.

        The two ParseConfig types intentionally remain separate: the server
        profile also carries V1 model and indexing settings, while the V2
        harness persists only the values that affect its execution plan.
        """

        maximum_frames = self.vision.max_frames_per_window
        if maximum_frames is None:
            maximum_frames = min(
                600,
                max(
                    1,
                    math.ceil(
                        self.window.chunk_duration_seconds * self.vision.fps
                    ),
                ),
            )
        return VideoParseV2Config(
            chunk_duration=self.window.chunk_duration_seconds,
            interval_duration=self.window.chunk_duration_seconds * 3,
            batch_max_size=10,
            dispatch_interval_seconds=0,
            models=VideoParseV2ModelSnapshot(
                asr=VideoParseV2ASRModelConfig(
                    source=self.asr.source,
                    model=self.asr.model,
                    request_timeout_seconds=self.execution.request_timeout_seconds,
                    max_output_tokens=self.asr.max_output_tokens,
                    mode=self.asr.mode,
                    fps=self.asr.fps,
                ),
                understanding=VideoParseV2GenerativeModelConfig(
                    source=self.vision.source,
                    model=self.vision.model,
                    request_timeout_seconds=self.execution.request_timeout_seconds,
                    max_output_tokens=self.vision.max_output_tokens,
                    fallback=(
                        VideoParseV2FallbackModelConfig(
                            source=self.fallback.source,
                            model=self.fallback.model,
                            request_timeout_seconds=(
                                self.fallback.request_timeout_seconds
                            ),
                            max_output_tokens=self.fallback.max_output_tokens,
                            reasoning_effort=self.fallback.reasoning_effort,
                        )
                        if self.fallback.enabled
                        else None
                    ),
                ),
                refine=None,
                ocr=None,
                prefix_context=None,
                embedding=VideoParseV2EmbeddingModelConfig(
                    source=self.embedding.source,
                    model=self.embedding.model,
                    dimensions=self.embedding.dimensions,
                    video_fps=self.embedding.video_fps,
                    request_timeout_seconds=self.execution.request_timeout_seconds,
                ),
            ),
            visual_input=VideoParseV2VisualInputConfig(
                mode=self.vision.input_mode,
                fps=self.vision.fps,
                max_frames_per_chunk=maximum_frames,
                max_height=self.window.analysis_max_height,
            ),
            ocr_enabled=False,
            understanding_enabled=True,
            compact_enabled=False,
            prefix_context=None,
            execution=VideoParseV2ExecutionConfig(
                max_concurrent_model_calls=self.asr.concurrency,
                max_retry_count=max(0, self.execution.max_attempts - 1),
                retry_initial_delay_seconds=1,
                retry_max_delay_seconds=30,
                max_model_calls=100,
            ),
        )

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
        fallback_enabled = (
            runtime.fallback_enabled
            if runtime.fallback_enabled is not None
            else _environment_bool("ANIVAG_FALLBACK_ENABLED", fallback.enabled)
        )
        vision_input_mode = runtime.video_input_mode
        vision_max_frames = runtime.video_max_frames_per_window
        if isinstance(gateway.stage_source("vision"), OpenAIVisionSourceConfig):
            vision_input_mode = "ordered_frames"
            vision_max_frames = vision_max_frames or parser.max_frames_per_window
        return cls(
            window=ParseWindowConfig(
                chunk_duration_seconds=runtime.chunk_duration_seconds,
                context_overlap_seconds=runtime.context_overlap_seconds,
                analysis_max_height=runtime.max_height,
                canonical_max_height=runtime.max_height,
            ),
            asr=ParseASRConfig(
                source=_stage_source_name(gateway, "transcription"),
                mode=runtime.asr_mode,
                model=runtime.transcript_model or os.getenv("ANIVAG_TRANSCRIPT_MODEL") or parser.transcript_model,
                fps=runtime.transcript_fps or float(os.getenv("ANIVAG_TRANSCRIPT_FPS") or parser.transcript_fps),
                concurrency=runtime.asr_concurrency,
                max_output_tokens=runtime.transcript_max_output_tokens or parser.transcription_max_output_tokens,
            ),
            vision=ParseVisionConfig(
                source=_stage_source_name(gateway, "vision"),
                model=runtime.video_model or os.getenv("ANIVAG_VIDEO_MODEL") or parser.video_model,
                fps=runtime.video_fps or float(os.getenv("ANIVAG_VIDEO_FPS") or parser.video_fps),
                input_mode=vision_input_mode,
                max_frames_per_window=vision_max_frames,
                max_output_tokens=runtime.video_max_output_tokens or parser.vision_max_output_tokens,
            ),
            context=ParseContextConfig(
                max_events=runtime.max_context_events,
                max_chars=runtime.max_context_chars,
                background_image_max_edge=images.model_max_edge,
            ),
            fallback=ParseFallbackConfig(
                # A disabled fallback is not part of the frozen execution path,
                # so an otherwise valid deployment need not configure its
                # provider as a named model source.
                source=(
                    _fallback_source_name(gateway)
                    if fallback_enabled
                    else "deepseek_api"
                ),
                enabled=fallback_enabled,
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
                source=_embedding_source_name(gateway),
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


def _stage_source_name(gateway: GatewayConfig, stage: str) -> str:
    configured = (
        gateway.video_parser.transcription_source
        if stage == "transcription"
        else gateway.video_parser.vision_source
    )
    if configured is not None:
        return configured
    effective = gateway.stage_source(stage)
    for name, source in gateway.model_sources.items():
        if source == effective:
            return name
    raise ValueError(f"effective {stage} model source has no stable configured name")


def _embedding_source_name(gateway: GatewayConfig) -> str:
    for name, source in gateway.model_sources.items():
        if isinstance(source, ArkModelSourceConfig):
            return name
    raise ValueError("multimodal embedding requires a configured Ark model source")


def _fallback_source_name(gateway: GatewayConfig) -> str:
    fallback = gateway.video_parser.fallback.deepseek
    if fallback.vision_source is not None:
        return fallback.vision_source
    for name, source in gateway.model_sources.items():
        if (
            isinstance(source, OpenAIModelSourceConfig)
            and source.provider == fallback.provider
            and source.base_url == fallback.base_url
            and source.api_key_env == fallback.api_key_env
        ):
            return name
    raise ValueError("enabled parse fallback has no stable configured model source")


def default_video_parse_v2_config() -> VideoParseV2Config:
    """Return the explicit V2 fallback used when no V1 profile was injected.

    Production bootstrap supplies an effective server ParseConfig. This
    fallback gives a newly created upload without an injected V1 profile a
    complete snapshot; it is never used to restore an existing task.
    """

    return VideoParseV2Config(
        chunk_duration=60,
        interval_duration=180,
        batch_max_size=10,
        dispatch_interval_seconds=0,
        models=VideoParseV2ModelSnapshot(
            asr=VideoParseV2ASRModelConfig(
                source="ark_default",
                model="doubao-seed-2-0-lite-260428",
                request_timeout_seconds=600,
                max_output_tokens=8192,
                mode="lite",
                fps=1.0,
            ),
            understanding=VideoParseV2GenerativeModelConfig(
                source="ark_default",
                model="doubao-seed-2-1-pro-260628",
                request_timeout_seconds=600,
                max_output_tokens=8192,
            ),
            refine=None,
            ocr=None,
            prefix_context=None,
            embedding=VideoParseV2EmbeddingModelConfig(
                source="ark_default",
                model="doubao-embedding-vision-250615",
                dimensions=1024,
                video_fps=1.0,
                request_timeout_seconds=600,
            ),
        ),
        visual_input=VideoParseV2VisualInputConfig(
            mode="video",
            fps=1.0,
            max_frames_per_chunk=60,
            max_height=720,
        ),
        ocr_enabled=False,
        understanding_enabled=True,
        compact_enabled=False,
        prefix_context=None,
        execution=VideoParseV2ExecutionConfig(
            max_concurrent_model_calls=8,
            max_retry_count=2,
            retry_initial_delay_seconds=1,
            retry_max_delay_seconds=30,
            max_model_calls=100,
        ),
    )
