from __future__ import annotations

import math
import os
from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from engine.common.config import VideoParseRuntimeConfig
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
from gateway.config import GatewayConfig
from gateway.multimodal import adapter_capabilities

from ..schema import ContractModel
from .media import ImageUploadConfig
from .upload import UploadServiceConfig


_MODEL_POLICY_FIELDS = (
    "context_window_tokens", "context_safety_margin_tokens", "auto_compact_tokens",
    "compact_target_tokens", "max_input_chars",
)


def _model_policy(model) -> dict[str, Any]:
    return {name: getattr(model, name) for name in _MODEL_POLICY_FIELDS}


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
    max_output_tokens: int | None = Field(default=None, ge=256, le=262_144)
    concurrency: int = Field(ge=1)
    request_timeout_seconds: float | None = Field(default=None, gt=0)


class ParseVisionConfig(ContractModel):
    source: str = Field(default="ark_default", min_length=1)
    model: str = Field(min_length=1)
    input_mode: Literal["video", "ordered_frames"] = "video"
    fps: float = Field(ge=0.2, le=5.0)
    max_frames_per_window: int | None = Field(default=None, ge=1, le=600)
    max_output_tokens: int | None = Field(default=None, ge=256, le=262_144)
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    context_window_tokens: int | None = Field(default=None, gt=0)
    context_safety_margin_tokens: int | None = Field(default=None, ge=0)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    compact_target_tokens: int | None = Field(default=None, gt=0)
    max_input_chars: int | None = Field(default=None, gt=0)

    @model_serializer(mode="wrap")
    def serialize_vision(self, handler: Any) -> dict[str, Any]:
        vision = handler(self)
        # Preserve request fingerprints made before this optional field existed.
        if self.reasoning_effort is None:
            vision.pop("reasoning_effort", None)
            vision.pop("reasoningEffort", None)
        for name in _MODEL_POLICY_FIELDS + ("request_timeout_seconds",):
            if getattr(self, name) is None:
                vision.pop(name, None)
                alias = self.__class__.model_fields[name].alias
                if alias is not None:
                    vision.pop(alias, None)
        return vision

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
    max_output_tokens: int = Field(ge=256, le=262_144)
    request_timeout_seconds: float = Field(gt=0)
    context_window_tokens: int | None = Field(default=None, gt=0)
    context_safety_margin_tokens: int | None = Field(default=None, ge=0)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    compact_target_tokens: int | None = Field(default=None, gt=0)
    max_input_chars: int | None = Field(default=None, gt=0)

    @model_serializer(mode="wrap")
    def serialize_policy(self, handler):
        values = handler(self)
        for name in _MODEL_POLICY_FIELDS:
            if getattr(self, name) is None:
                values.pop(name, None)
                alias = self.__class__.model_fields[name].alias
                if alias is not None:
                    values.pop(alias, None)
        return values


class ParseEmbeddingConfig(ContractModel):
    source: str = Field(default="ark_default", min_length=1)
    model: str = Field(min_length=1)
    encoder_model: str | None = Field(default=None, min_length=1)
    dimensions: int = Field(ge=1, le=2048)
    video_fps: float = Field(ge=0.2, le=5.0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)


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
                    request_timeout_seconds=self.asr.request_timeout_seconds or self.execution.request_timeout_seconds,
                    max_output_tokens=self.asr.max_output_tokens,
                    mode=self.asr.mode,
                    fps=self.asr.fps,
                ),
                understanding=VideoParseV2GenerativeModelConfig(
                    source=self.vision.source,
                    model=self.vision.model,
                    request_timeout_seconds=self.vision.request_timeout_seconds or self.execution.request_timeout_seconds,
                    max_output_tokens=self.vision.max_output_tokens,
                    supports_vision_input=True,
                    **_model_policy(self.vision),
                    reasoning_effort=self.vision.reasoning_effort,
                    fallback=(
                        VideoParseV2FallbackModelConfig(
                            source=self.fallback.source,
                            model=self.fallback.model,
                            request_timeout_seconds=(
                                self.fallback.request_timeout_seconds
                            ),
                            max_output_tokens=self.fallback.max_output_tokens,
                            reasoning_effort=self.fallback.reasoning_effort,
                            supports_vision_input=True,
                            **_model_policy(self.fallback),
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
                    encoder_model=self.embedding.encoder_model,
                    dimensions=self.embedding.dimensions,
                    video_fps=self.embedding.video_fps,
                    request_timeout_seconds=self.embedding.request_timeout_seconds or self.execution.request_timeout_seconds,
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
        embedding_model: str | None,
        embedding_dimensions: int,
        embedding_fps: float | None,
    ) -> "ParseConfig":
        parser = gateway.video_parser
        fallback = parser.fallback
        asr_binding = gateway.resolve_model_binding("transcription")
        vision_binding = gateway.resolve_model_binding("vision")
        if not vision_binding.supports_vision_input:
            raise ValueError("configured vision model must declare image input support")
        embedding_binding = gateway.resolve_embedding_binding(environ={})
        fallback_enabled = (
            runtime.fallback_enabled
            if runtime.fallback_enabled is not None
            else vision_binding.fallback is not None
        )
        fallback_binding = vision_binding.fallback
        if fallback_binding is None and "vision_fallback" in gateway.normalized_models:
            fallback_binding = gateway.resolve_model_binding("vision_fallback")
        if fallback_enabled and fallback_binding is None:
            raise ValueError("enabled visual fallback requires a configured model")
        # Disabled fallbacks carry harmless shape defaults and are omitted from the task.
        fallback_binding = fallback_binding or vision_binding
        vision_input_mode = runtime.video_input_mode
        vision_max_frames = runtime.video_max_frames_per_window
        capabilities = adapter_capabilities(gateway.source(vision_binding.source).adapter)
        if capabilities.visual_input_mode == "ordered_frames":
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
                source=asr_binding.source,
                mode=runtime.asr_mode,
                model=runtime.transcript_model or asr_binding.model,
                fps=runtime.transcript_fps or float(os.getenv("ANIVAG_TRANSCRIPT_FPS") or parser.transcript_fps),
                concurrency=runtime.asr_concurrency,
                max_output_tokens=runtime.transcript_max_output_tokens or asr_binding.max_output_tokens,
                request_timeout_seconds=asr_binding.request_timeout_seconds,
            ),
            vision=ParseVisionConfig(
                source=vision_binding.source,
                model=runtime.video_model or vision_binding.model,
                fps=runtime.video_fps or float(os.getenv("ANIVAG_VIDEO_FPS") or parser.video_fps),
                input_mode=vision_input_mode,
                max_frames_per_window=vision_max_frames,
                max_output_tokens=runtime.video_max_output_tokens or vision_binding.max_output_tokens,
                reasoning_effort=vision_binding.reasoning_effort,
                request_timeout_seconds=vision_binding.request_timeout_seconds,
                **_model_policy(vision_binding),
            ),
            context=ParseContextConfig(
                max_events=runtime.max_context_events,
                max_chars=runtime.max_context_chars,
                background_image_max_edge=images.model_max_edge,
            ),
            fallback=ParseFallbackConfig(
                source=fallback_binding.source,
                enabled=fallback_enabled,
                model=fallback_binding.model,
                frame_fps=fallback.frame_fps,
                group_duration_seconds=fallback.group_duration_seconds,
                max_frames_per_group=fallback.max_frames_per_group,
                max_height=fallback.max_height,
                reasoning_effort=fallback_binding.reasoning_effort or "none",
                max_output_tokens=fallback_binding.max_output_tokens or 8192,
                request_timeout_seconds=fallback_binding.request_timeout_seconds,
                **_model_policy(fallback_binding),
            ),
            embedding=resolve_embedding_config(
                gateway,
                model=embedding_model if embedding_model is not None else embedding_binding.model,
                dimensions=embedding_dimensions,
                video_fps=embedding_fps if embedding_fps is not None else embedding_binding.video_fps,
            ),
            execution=ParseExecutionConfig(
                request_timeout_seconds=runtime.request_timeout_seconds,
                max_attempts=upload.max_job_attempts,
            ),
        )


def resolve_embedding_config(
    gateway: GatewayConfig, *, dimensions: int,
    model: str | None = None, video_fps: float | None = None,
) -> ParseEmbeddingConfig:
    environment = dict(os.environ)
    if model is not None:
        environment.pop("ANIVAG_VIDEO_EMBEDDING_MODEL", None)
    if video_fps is not None:
        environment.pop("ANIVAG_VIDEO_EMBEDDING_FPS", None)
    definition = gateway.resolve_embedding_binding(environ=environment)
    updates = {"dimensions": dimensions}
    if model is not None:
        updates["model"] = model
    if video_fps is not None:
        updates["video_fps"] = video_fps
    definition = gateway.resolve_embedding_binding(definition.model_copy(update=updates))
    return ParseEmbeddingConfig(
        source=definition.source, model=definition.model,
        encoder_model=definition.encoder_model, dimensions=definition.dimensions,
        video_fps=definition.video_fps,
        request_timeout_seconds=definition.request_timeout_seconds,
    )


def default_video_parse_v2_config(gateway: GatewayConfig | None = None) -> VideoParseV2Config:
    """Freeze local external model configuration when no server profile was injected."""
    if gateway is None:
        from gateway.config import load_config

        gateway = load_config()
    embedding = gateway.resolve_embedding_binding()
    return ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(asr_concurrency=8),
        gateway=gateway,
        upload=UploadServiceConfig(),
        images=ImageUploadConfig(),
        embedding_model=None,
        embedding_dimensions=embedding.dimensions,
        embedding_fps=None,
    ).to_video_parse_v2()
