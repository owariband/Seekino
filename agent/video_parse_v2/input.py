"""Harness 的任务输入和可持久化配置；数值默认值由上游补齐。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class _InputModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


class VisualInputConfig(_InputModel):
    mode: Literal["ordered_frames", "video"]
    fps: float = Field(gt=0)
    max_frames_per_chunk: int = Field(gt=0)
    max_height: int = Field(gt=0)


class PrefixContextConfig(_InputModel):
    series_compact_max_chars: int = Field(gt=0)
    video_compact_max_chars: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)


class _ModelPolicy(_InputModel):
    """Optional controls keep historical snapshots readable without rewriting them."""

    context_window_tokens: int | None = Field(default=None, gt=0)
    context_safety_margin_tokens: int | None = Field(default=None, ge=0)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    compact_target_tokens: int | None = Field(default=None, gt=0)
    max_input_chars: int | None = Field(default=None, gt=0)
    supports_vision_input: bool | None = None

    @model_serializer(mode="wrap")
    def serialize_policy(self, handler):
        values = handler(self)
        for name in _ModelPolicy.model_fields:
            if getattr(self, name) is None:
                values.pop(name, None)
        return values


class FallbackModelConfig(_ModelPolicy):
    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None


class GenerativeModelConfig(_ModelPolicy):
    """Credential-free identity and fixed request controls for one model stage."""

    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None
    fallback: FallbackModelConfig | None = None


class ASRModelConfig(GenerativeModelConfig):
    mode: Literal["lite", "none"]
    fps: float = Field(gt=0)


class EmbeddingModelConfig(_InputModel):
    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    encoder_model: str | None = Field(default=None, min_length=1)
    dimensions: int = Field(gt=0)
    video_fps: float = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)


class ModelSnapshot(_InputModel):
    """Effective model selection persisted with the task, without credentials."""

    asr: ASRModelConfig
    understanding: GenerativeModelConfig
    refine: GenerativeModelConfig | None = None
    ocr: GenerativeModelConfig | None = None
    prefix_context: GenerativeModelConfig | None = None
    embedding: EmbeddingModelConfig


class ExecutionConfig(_InputModel):
    parse_concurrency: int = Field(gt=0)
    refine_concurrency: int = Field(gt=0)
    index_concurrency: int = Field(gt=0)
    parse_execution_timeout_seconds: float = Field(gt=0)
    refine_execution_timeout_seconds: float = Field(gt=0)
    index_execution_timeout_seconds: float = Field(gt=0)
    max_retry_count: int = Field(ge=0)
    retry_initial_delay_seconds: float = Field(gt=0)
    retry_max_delay_seconds: float = Field(gt=0)
    max_model_calls: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_retry_delays(self) -> ExecutionConfig:
        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError(
                "retry_max_delay_seconds must be at least retry_initial_delay_seconds"
            )
        return self


class IndexingConfig(_InputModel):
    sparse_enabled: bool
    contract_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class ParseConfig(_InputModel):
    """保存到 video_parse_task.parse_config 的有效配置快照。"""

    chunk_duration: int = Field(gt=0)
    interval_duration: int = Field(gt=0)
    batch_max_size: int = Field(gt=0)
    dispatch_interval_seconds: float = Field(ge=0)
    models: ModelSnapshot
    visual_input: VisualInputConfig
    ocr_enabled: bool = False
    understanding_enabled: bool = True
    compact_enabled: bool = False
    prefix_context: PrefixContextConfig | None = None
    execution: ExecutionConfig
    indexing: IndexingConfig

    @model_validator(mode="after")
    def validate_config_limits(self) -> ParseConfig:
        # ceil(interval_duration / chunk_duration) 必须能由现有 bitmap 表示。
        if self.interval_duration > self.chunk_duration * 63:
            raise ValueError("interval_duration requires more than 63 chunks")
        if self.compact_enabled and self.prefix_context is None:
            raise ValueError("prefix_context is required when compact_enabled is true")
        if self.ocr_enabled and self.models.ocr is None:
            raise ValueError("models.ocr is required when ocr_enabled is true")
        if (
            not self.understanding_enabled
            and self.models.asr.mode == "none"
            and not self.ocr_enabled
        ):
            raise ValueError(
                "understanding_enabled=false requires ASR or OCR input"
            )
        if self.compact_enabled and self.models.prefix_context is None:
            raise ValueError(
                "models.prefix_context is required when compact_enabled is true"
            )
        return self


class ParseInput(_InputModel):
    """创建解析任务的输入；Video 归属由任务服务校验。"""

    series_id: str = Field(min_length=1)
    video_id: str = Field(min_length=1)
    parse_config: ParseConfig
