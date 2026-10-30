from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from engine.common.config import VideoParseRuntimeConfig
from agent.video_parse_v2.input import (
    ASRModelConfig as VideoParseV2ASRModelConfig,
    EmbeddingModelConfig as VideoParseV2EmbeddingModelConfig,
    ExecutionConfig as VideoParseV2ExecutionConfig,
    IndexingConfig as VideoParseV2IndexingConfig,
    FallbackModelConfig as VideoParseV2FallbackModelConfig,
    GenerativeModelConfig as VideoParseV2GenerativeModelConfig,
    ModelSnapshot as VideoParseV2ModelSnapshot,
    ParseConfig as VideoParseV2Config,
    VisualInputConfig as VideoParseV2VisualInputConfig,
)
from gateway.config import GatewayConfig
from gateway.embedding import describe_embedding_model
from engine.rag import VIDEO_CLIP_PREPROCESS_REVISION
from engine.rag.encoding import DEFAULT_ENCODER_OUTPUT_CONTRACT, EncoderContract
from engine.rag.indexing.events import EventIndexingContract
from engine.rag.vision.event_frames import EventFrameContract
from engine.rag.indexing.pipeline import INDEX_SOURCE_VERSION
from engine.rag.indexing.projection import INDEX_PROJECTION_VERSION
from ..constants.parse import MAX_INDEX_GENERATION, PARSE_RETRIEVAL_CONTRACT
from gateway.multimodal import adapter_capabilities

from ..schema import ContractModel
from .media import ImageUploadConfig
from .elasticsearch import DEFAULT_INDEX_GENERATION
from .mq import MQConsumerConfig
from .upload import UploadServiceConfig


_MODEL_POLICY_FIELDS = (
    "context_window_tokens", "context_safety_margin_tokens", "auto_compact_tokens",
    "compact_target_tokens", "max_input_chars",
)
_EXECUTION_POLICY_FIELDS = (
    "batch_max_size", "dispatch_interval_seconds",
    "retry_initial_delay_seconds", "retry_max_delay_seconds",
    "parse_concurrency", "refine_concurrency", "index_concurrency",
    "parse_execution_timeout_seconds", "refine_execution_timeout_seconds",
    "index_execution_timeout_seconds", "max_model_calls",
)


def _validate_schedule_delay(seconds: float | None) -> None:
    if seconds is None:
        return
    try:
        datetime.now(UTC) + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("scheduling delay exceeds the supported datetime range") from exc


def validate_video_parse_schedule(config: VideoParseV2Config) -> VideoParseV2Config:
    """Reject non-executable delays at the Server request boundary."""
    generation = config.indexing.index_generation
    if generation is not None and not 1 <= generation <= MAX_INDEX_GENERATION:
        raise ValueError("index_generation is outside the Elasticsearch version contract")
    for seconds in (
        config.dispatch_interval_seconds,
        config.execution.retry_initial_delay_seconds,
        config.execution.retry_max_delay_seconds,
        config.execution.parse_execution_timeout_seconds,
        config.execution.refine_execution_timeout_seconds,
        config.execution.index_execution_timeout_seconds,
    ):
        _validate_schedule_delay(seconds)
    if config.voiceprint is not None:
        _validate_schedule_delay(config.voiceprint.execution_timeout_seconds)
    return config


def index_contract_fingerprint(embedding, *, provider: str, encoder_revision: str, max_height: int) -> str:
    """Freeze encoding rules without credentials or request-only timeouts."""
    identity = {
        "retrieval": PARSE_RETRIEVAL_CONTRACT,
        "source": INDEX_SOURCE_VERSION,
        "projection": INDEX_PROJECTION_VERSION,
        "output": DEFAULT_ENCODER_OUTPUT_CONTRACT,
        "preprocess": VIDEO_CLIP_PREPROCESS_REVISION,
        "provider": provider,
        "source_name": embedding.source,
        "encoder_model": embedding.encoder_model or embedding.model,
        "encoder_revision": encoder_revision,
        "dimensions": embedding.dimensions,
        "fps": float(embedding.video_fps),
        "max_height": max_height,
    }
    if embedding.input_mode != "multimodal":
        identity["input_mode"] = embedding.input_mode
    if embedding.query_instruction is not None:
        identity["query_instruction"] = embedding.query_instruction
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_event_indexing_contract(
    encoder: EncoderContract, *, max_height: int, vision_enabled: bool = True,
    environ: Mapping[str, str] | None = None,
) -> EventIndexingContract:
    """Resolve Event sampling once; tasks freeze the returned effective contract."""
    if not vision_enabled:
        return EventIndexingContract(semantic_encoder=encoder, vision_encoder=None, capture=None)
    environment = os.environ if environ is None else environ
    options: dict[str, Any] = {"max_height": max_height}
    for key, name, convert in (
        ("SEEKINO_EVENT_FRAME_FPS", "fps", float),
        ("SEEKINO_EVENT_MAX_FRAMES", "max_frames", int),
    ):
        value = environment.get(key)
        if value is not None:
            options[name] = convert(value)
    return EventIndexingContract(
        semantic_encoder=encoder, vision_encoder=encoder,
        capture=EventFrameContract(**options),
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
    scope: Literal["chunk", "video"] = "chunk"
    model: str = Field(min_length=1)
    fps: float = Field(ge=0.2, le=5.0)
    max_output_tokens: int | None = Field(default=None, ge=256, le=262_144)
    concurrency: int = Field(ge=1)
    request_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_serializer(mode="wrap")
    def serialize_asr(self, handler):
        values = handler(self)
        if self.scope == "chunk":
            values.pop("scope", None)
        return values


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
    input_mode: Literal["multimodal", "text"] = "multimodal"
    query_instruction: str | None = Field(default=None, min_length=1, pattern=r"\S")
    source: str = Field(default="ark_default", min_length=1)
    model: str = Field(min_length=1)
    encoder_model: str | None = Field(default=None, min_length=1)
    dimensions: int = Field(ge=1, le=2048)
    video_fps: float = Field(ge=0.2, le=5.0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)


class ParseExecutionConfig(ContractModel):
    request_timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1, strict=True)
    parse_concurrency: int | None = Field(default=None, gt=0, strict=True)
    refine_concurrency: int | None = Field(default=None, gt=0, strict=True)
    index_concurrency: int | None = Field(default=None, gt=0, strict=True)
    max_model_calls: int | None = Field(default=None, gt=0, strict=True)
    parse_execution_timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    refine_execution_timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    index_execution_timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    batch_max_size: int | None = Field(default=None, gt=0, strict=True)
    dispatch_interval_seconds: float | None = Field(
        default=None, ge=0, strict=True, allow_inf_nan=False,
    )
    retry_initial_delay_seconds: float | None = Field(
        default=None, gt=0, strict=True, allow_inf_nan=False,
    )
    retry_max_delay_seconds: float | None = Field(
        default=None, gt=0, strict=True, allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_retry_delays(self) -> "ParseExecutionConfig":
        for name in _EXECUTION_POLICY_FIELDS[1:]:
            _validate_schedule_delay(getattr(self, name))
        if (
            self.retry_initial_delay_seconds is not None
            and self.retry_max_delay_seconds is not None
            and self.retry_max_delay_seconds < self.retry_initial_delay_seconds
        ):
            raise ValueError("retry_max_delay_seconds must be at least retry_initial_delay_seconds")
        return self

    @model_serializer(mode="wrap")
    def serialize_execution(self, handler):
        values = handler(self)
        # Older upload requests must keep their original idempotency fingerprint.
        for name in _EXECUTION_POLICY_FIELDS:
            if getattr(self, name) is None:
                values.pop(name, None)
                alias = self.__class__.model_fields[name].alias
                if alias is not None:
                    values.pop(alias, None)
        return values


class ParseConfig(ContractModel):
    version: Literal[2] = 2
    window: ParseWindowConfig
    asr: ParseASRConfig
    vision: ParseVisionConfig
    ocr: VideoParseV2GenerativeModelConfig | None = None
    context: ParseContextConfig
    fallback: ParseFallbackConfig
    embedding: ParseEmbeddingConfig
    execution: ParseExecutionConfig
    indexing: VideoParseV2IndexingConfig | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_ocr(self, handler):
        values = handler(self)
        if self.ocr is None:
            values.pop("ocr", None)
        return values

    def resolve_execution(self, defaults: "ParseConfig | None") -> "ParseConfig":
        """Fill only missing legacy request fields before freezing the task."""
        missing = {
            name for name in _EXECUTION_POLICY_FIELDS
            if getattr(self.execution, name) is None
        }
        if not missing and self.indexing is not None:
            return self
        if defaults is None:
            raise ValueError("incomplete parse configuration requires explicitly supplied defaults")
        upstream = defaults.to_video_parse_v2()
        values = {
            "batch_max_size": upstream.batch_max_size,
            "dispatch_interval_seconds": upstream.dispatch_interval_seconds,
            "retry_initial_delay_seconds": upstream.execution.retry_initial_delay_seconds,
            "retry_max_delay_seconds": upstream.execution.retry_max_delay_seconds,
        }
        values.update({name: getattr(upstream.execution, name) for name in _EXECUTION_POLICY_FIELDS[4:]})
        execution = ParseExecutionConfig.model_validate({
            **self.execution.model_dump(),
            **{name: values[name] for name in missing},
        })
        return self.model_copy(update={"execution": execution, "indexing": self.indexing or upstream.indexing})

    def to_video_parse_v2(self) -> VideoParseV2Config:
        """Freeze this effective server profile into the V2 task contract.

        The server profile accepts upload configuration; the harness snapshot
        persists the resolved values that affect its execution plan.
        """

        if self.indexing is None or any(getattr(self.execution, name) is None for name in _EXECUTION_POLICY_FIELDS):
            raise ValueError("parse execution configuration must be resolved before creating a V2 task")
        return VideoParseV2Config(
            chunk_duration=self.window.chunk_duration_seconds,
            interval_duration=self.window.chunk_duration_seconds * 3,
            batch_max_size=self.execution.batch_max_size,
            dispatch_interval_seconds=self.execution.dispatch_interval_seconds,
            models=VideoParseV2ModelSnapshot(
                asr=VideoParseV2ASRModelConfig(
                    source=self.asr.source,
                    model=self.asr.model,
                    request_timeout_seconds=self.asr.request_timeout_seconds or self.execution.request_timeout_seconds,
                    max_output_tokens=self.asr.max_output_tokens,
                    mode=self.asr.mode,
                    scope=self.asr.scope,
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
                ocr=self.ocr,
                prefix_context=None,
                embedding=VideoParseV2EmbeddingModelConfig(
                    input_mode=self.embedding.input_mode,
                    query_instruction=self.embedding.query_instruction,
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
                max_height=self.window.analysis_max_height,
                context_overlap_seconds=self.window.context_overlap_seconds,
            ),
            ocr_enabled=self.ocr is not None,
            understanding_enabled=True,
            compact_enabled=False,
            prefix_context=None,
            execution=VideoParseV2ExecutionConfig(
                parse_concurrency=self.execution.parse_concurrency,
                refine_concurrency=self.execution.refine_concurrency,
                index_concurrency=self.execution.index_concurrency,
                parse_execution_timeout_seconds=self.execution.parse_execution_timeout_seconds,
                refine_execution_timeout_seconds=self.execution.refine_execution_timeout_seconds,
                index_execution_timeout_seconds=self.execution.index_execution_timeout_seconds,
                max_retry_count=self.execution.max_attempts - 1,
                retry_initial_delay_seconds=self.execution.retry_initial_delay_seconds,
                retry_max_delay_seconds=self.execution.retry_max_delay_seconds,
                max_model_calls=self.execution.max_model_calls,
            ),
            indexing=self.indexing,
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
        mq: MQConsumerConfig | None = None,
        sparse_enabled: bool = False,
        index_generation: int = DEFAULT_INDEX_GENERATION,
    ) -> "ParseConfig":
        mq = mq if mq is not None else MQConsumerConfig(consumer_id="parse-config-defaults")
        parser = gateway.video_parser
        fallback = parser.fallback
        asr_binding = gateway.resolve_model_binding("transcription")
        vision_binding = gateway.resolve_model_binding("vision")
        if not vision_binding.supports_vision_input:
            raise ValueError("configured vision model must declare image input support")
        ocr = None
        if "ocr" in gateway.normalized_models:
            ocr_binding = gateway.resolve_model_binding("ocr")
            if not ocr_binding.supports_vision_input:
                raise ValueError("configured OCR model must declare image input support")
            if ocr_binding.fallback is not None and ocr_binding.fallback.fallback is not None:
                raise ValueError("OCR supports one configured fallback")
            ocr = VideoParseV2GenerativeModelConfig.model_validate({
                **ocr_binding.model_dump(
                    mode="json", exclude_none=True,
                    include={
                        **dict.fromkeys(VideoParseV2GenerativeModelConfig.model_fields, True),
                        "fallback": set(VideoParseV2FallbackModelConfig.model_fields),
                    },
                ),
                "request_timeout_seconds": ocr_binding.request_timeout_seconds or runtime.request_timeout_seconds,
            })
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
        effective_embedding = resolve_embedding_config(
            gateway, model=embedding_model if embedding_model is not None else embedding_binding.model,
            dimensions=embedding_dimensions,
            video_fps=embedding_fps if embedding_fps is not None else embedding_binding.video_fps,
        )
        validate_embedding_sparse_support(gateway, effective_embedding, sparse_enabled=sparse_enabled)
        embedding_info = describe_embedding_model(embedding_binding, gateway.source(embedding_binding.source))
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
                scope="video" if gateway.source(asr_binding.source).adapter == "whisperx" else "chunk",
                model=runtime.transcript_model or asr_binding.model,
                fps=runtime.transcript_fps or float(os.getenv("SEEKINO_TRANSCRIPT_FPS") or parser.transcript_fps),
                concurrency=runtime.asr_concurrency,
                max_output_tokens=runtime.transcript_max_output_tokens or asr_binding.max_output_tokens,
                request_timeout_seconds=asr_binding.request_timeout_seconds,
            ),
            vision=ParseVisionConfig(
                source=vision_binding.source,
                model=runtime.video_model or vision_binding.model,
                fps=runtime.video_fps or float(os.getenv("SEEKINO_VIDEO_FPS") or parser.video_fps),
                input_mode=vision_input_mode,
                max_frames_per_window=vision_max_frames,
                max_output_tokens=runtime.video_max_output_tokens or vision_binding.max_output_tokens,
                reasoning_effort=vision_binding.reasoning_effort,
                request_timeout_seconds=vision_binding.request_timeout_seconds,
                **_model_policy(vision_binding),
            ),
            ocr=ocr,
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
            embedding=effective_embedding,
            indexing=VideoParseV2IndexingConfig(
                index_generation=index_generation,
                sparse_enabled=sparse_enabled,
                contract_fingerprint=index_contract_fingerprint(
                    effective_embedding, provider=embedding_info.provider_name,
                    encoder_revision=embedding_info.encoder_revision, max_height=runtime.max_height,
                ),
            ),
            execution=ParseExecutionConfig(
                request_timeout_seconds=runtime.request_timeout_seconds,
                max_attempts=upload.max_job_attempts,
                parse_concurrency=runtime.parse_concurrency,
                refine_concurrency=runtime.refine_concurrency,
                index_concurrency=runtime.index_concurrency,
                parse_execution_timeout_seconds=runtime.parse_execution_timeout_seconds,
                refine_execution_timeout_seconds=runtime.refine_execution_timeout_seconds,
                index_execution_timeout_seconds=runtime.index_execution_timeout_seconds,
                max_model_calls=runtime.max_model_calls,
                batch_max_size=runtime.batch_max_size,
                dispatch_interval_seconds=runtime.dispatch_interval_seconds,
                retry_initial_delay_seconds=mq.retry_base.total_seconds(),
                retry_max_delay_seconds=mq.retry_max.total_seconds(),
            ),
        )


def validate_embedding_sparse_support(
    gateway: GatewayConfig, embedding: ParseEmbeddingConfig, *, sparse_enabled: bool,
) -> None:
    if not sparse_enabled:
        return
    source = gateway.source(embedding.source)
    if not adapter_capabilities(source.adapter).sparse_embedding:
        raise ValueError(
            f"embedding source {embedding.source!r} ({source.adapter}) does not support sparse embeddings; "
            "SEEKINO_RETRIEVAL_SPARSE_WEIGHT must be 0 for this source"
        )


def resolve_embedding_config(
    gateway: GatewayConfig, *, dimensions: int,
    model: str | None = None, video_fps: float | None = None,
) -> ParseEmbeddingConfig:
    environment = dict(os.environ)
    if model is not None:
        environment.pop("SEEKINO_VIDEO_EMBEDDING_MODEL", None)
    if video_fps is not None:
        environment.pop("SEEKINO_VIDEO_EMBEDDING_FPS", None)
    definition = gateway.resolve_embedding_binding(environ=environment)
    updates = {"dimensions": dimensions}
    if model is not None:
        updates["model"] = model
    if video_fps is not None:
        updates["video_fps"] = video_fps
    definition = gateway.resolve_embedding_binding(definition.model_copy(update=updates))
    return ParseEmbeddingConfig(
        input_mode=definition.input_mode, query_instruction=definition.query_instruction,
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
        runtime=VideoParseRuntimeConfig(),
        gateway=gateway,
        upload=UploadServiceConfig(),
        images=ImageUploadConfig(),
        embedding_model=None,
        embedding_dimensions=embedding.dimensions,
        embedding_fps=None,
    ).to_video_parse_v2()
