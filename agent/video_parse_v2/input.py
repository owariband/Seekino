"""Harness 的任务输入和可持久化配置；数值默认值由上游补齐。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from gateway.constants import ReasoningEffort


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
    max_height: int = Field(gt=0)
    # Historical V2 snapshots have no visual context outside their core chunk.
    context_overlap_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_frame_cap(cls, values):
        if isinstance(values, dict):
            return {key: value for key, value in values.items() if key != "max_frames_per_chunk"}
        return values

    @model_serializer(mode="wrap")
    def serialize_visual_input(self, handler):
        values = handler(self)
        if self.context_overlap_seconds == 0:
            values.pop("context_overlap_seconds", None)
        return values


class PrefixContextConfig(_InputModel):
    series_compact_max_chars: int = Field(gt=0)
    video_compact_max_chars: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)


class VoiceprintConfig(_InputModel):
    max_tool_calls: int = Field(gt=0)
    execution_timeout_seconds: float = Field(gt=0)


class _ModelPolicy(_InputModel):
    """Optional controls keep historical snapshots readable without rewriting them."""

    stream: bool = Field(default=True, strict=True)
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
        if "stream" not in self.model_fields_set:
            values.pop("stream", None)
        return values


class FallbackModelConfig(_ModelPolicy):
    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: ReasoningEffort | None = None


class GenerativeModelConfig(_ModelPolicy):
    """Credential-free identity and fixed request controls for one model stage."""

    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: ReasoningEffort | None = None
    fallback: FallbackModelConfig | None = None


class ASRModelConfig(GenerativeModelConfig):
    mode: Literal["lite", "none"]
    fps: float = Field(gt=0)
    scope: Literal["chunk", "video"] = "chunk"

    @model_serializer(mode="wrap")
    def serialize_asr(self, handler):
        values = self.serialize_policy(handler)
        if self.scope == "chunk":
            values.pop("scope", None)
        return values


class EmbeddingModelConfig(_InputModel):
    input_mode: Literal["multimodal", "text"] = "multimodal"
    query_instruction: str | None = Field(default=None, min_length=1, pattern=r"\S")
    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    encoder_model: str | None = Field(default=None, min_length=1)
    dimensions: int = Field(gt=0)
    video_fps: float = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)


class SpeakerEmbeddingModelConfig(_InputModel):
    source: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)


class ModelSnapshot(_InputModel):
    """Effective model selection persisted with the task, without credentials."""

    asr: ASRModelConfig
    understanding: GenerativeModelConfig
    refine: GenerativeModelConfig | None = None
    ocr: GenerativeModelConfig | None = None
    prefix_context: GenerativeModelConfig | None = None
    voiceprint: GenerativeModelConfig | None = None
    speaker_embedding: SpeakerEmbeddingModelConfig | None = None
    embedding: EmbeddingModelConfig

    @model_serializer(mode="wrap")
    def serialize_models(self, handler):
        values = handler(self)
        for name in ("voiceprint", "speaker_embedding"):
            if getattr(self, name) is None:
                values.pop(name, None)
        return values


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
    # Requests and historical snapshots may predate this server-owned value.
    # The host binds it before accepting an executable task.
    index_generation: int | None = Field(default=None, gt=0)
    sparse_enabled: bool
    contract_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")

    @model_serializer(mode="wrap")
    def serialize_indexing(self, handler):
        values = handler(self)
        if self.index_generation is None:
            values.pop("index_generation", None)
        return values


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
    voiceprint: VoiceprintConfig | None = None
    voiceprint_mode: Literal["off", "flag", "prefill"] = "off"
    execution: ExecutionConfig
    indexing: IndexingConfig

    @model_serializer(mode="wrap")
    def serialize_config(self, handler):
        values = handler(self)
        if self.voiceprint is None:
            values.pop("voiceprint", None)
        if self.voiceprint_mode == "off":
            values.pop("voiceprint_mode", None)
        return values

    @property
    def voiceprint_enabled(self) -> bool:
        return self.voiceprint is not None or self.voiceprint_mode != "off"

    @model_validator(mode="after")
    def validate_config_limits(self) -> ParseConfig:
        if not self.understanding_enabled and self.models.asr.mode != "none" and self.models.asr.scope == "video":
            raise ValueError("video ASR requires Understanding to interpret boundary-crossing cues")
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
        if self.voiceprint_mode != "off" and self.models.speaker_embedding is None:
            raise ValueError("models.speaker_embedding is required for B0 scoring")
        if self.voiceprint_mode == "off" and self.voiceprint is not None and (
            self.models.voiceprint is None or self.models.speaker_embedding is None
        ):
            raise ValueError(
                "models.voiceprint and models.speaker_embedding are required when voiceprint is enabled"
            )
        return self


class ParseInput(_InputModel):
    """创建解析任务的输入；Video 归属由任务服务校验。"""

    series_id: str = Field(min_length=1)
    video_id: str = Field(min_length=1)
    parse_config: ParseConfig
