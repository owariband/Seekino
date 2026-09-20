from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal, cast

from engine.parse.runtime import VideoParseRuntimeConfig

from ..database import DatabaseEnvironment
from .agent import AgentServiceConfig
from .index_sync import IndexSyncConfig
from .media import ImageUploadConfig
from .logging import LoggingConfig
from .mq import MQConsumerConfig
from .retrieval import RetrievalConfig
from .sse import SSEConfig
from .upload import UploadServiceConfig


@dataclass(frozen=True, slots=True)
class ServerSettings:
    environment: DatabaseEnvironment
    database_url: str
    artifact_root: Path
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str | None
    text_vector_dimensions: int
    vision_vector_dimensions: int
    upload: UploadServiceConfig
    parse: VideoParseRuntimeConfig
    agent: AgentServiceConfig
    agent_chat_model: str | None
    agent_fallback_model: str | None
    agent_evaluator_model: str | None
    agent_request_timeout_seconds: float
    mq: MQConsumerConfig
    index_sync: IndexSyncConfig
    allowed_origins: tuple[str, ...]
    model_config_path: Path | None
    sse: SSEConfig = field(default_factory=SSEConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    database_pool_recycle_seconds: int = 3600
    video_embedding_model: str | None = None
    video_embedding_fps: float | None = None
    agent_query_model: str | None = None
    model_pricing_path: Path | None = None
    images: ImageUploadConfig = field(default_factory=ImageUploadConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_environment(cls) -> "ServerSettings":
        upload_defaults = UploadServiceConfig()
        parse_defaults = VideoParseRuntimeConfig()
        agent_defaults = AgentServiceConfig()
        sse_defaults = SSEConfig()
        retrieval_defaults = RetrievalConfig()
        index_sync_defaults = IndexSyncConfig()
        image_defaults = ImageUploadConfig()
        logging_defaults = LoggingConfig()
        environment = DatabaseEnvironment(
            os.getenv("ANIVAG_ENVIRONMENT", DatabaseEnvironment.DEVELOPMENT.value)
        )
        database_url = _required_environment("ANIVAG_DATABASE_URL")
        qdrant_url = _required_environment("ANIVAG_QDRANT_URL")
        qdrant_collection = _required_environment("ANIVAG_QDRANT_COLLECTION")
        artifact_root = Path(
            os.getenv("ANIVAG_ARTIFACT_ROOT", "outputs/server/artifacts")
        ).expanduser()
        segment_size = _environment_int(
            "ANIVAG_UPLOAD_SEGMENT_SIZE_BYTES",
            upload_defaults.preferred_segment_size_bytes,
        )
        max_upload_size = _environment_int(
            "ANIVAG_MAX_UPLOAD_SIZE_BYTES",
            upload_defaults.maximum_upload_size_bytes,
        )
        lease_seconds = _environment_int(
            "ANIVAG_MQ_LEASE_SECONDS",
            300,
            legacy_name="ANIVAG_WORKER_LEASE_SECONDS",
        )
        heartbeat_seconds = _environment_int(
            "ANIVAG_MQ_HEARTBEAT_SECONDS",
            60,
            legacy_name="ANIVAG_WORKER_HEARTBEAT_SECONDS",
        )
        retry_base_seconds = _environment_int(
            "ANIVAG_MQ_RETRY_BASE_SECONDS",
            30,
            legacy_name="ANIVAG_WORKER_RETRY_BASE_SECONDS",
        )
        retry_max_seconds = _environment_int(
            "ANIVAG_MQ_RETRY_MAX_SECONDS",
            900,
            legacy_name="ANIVAG_WORKER_RETRY_MAX_SECONDS",
        )
        text_dimensions = _environment_int(
            "ANIVAG_TEXT_VECTOR_DIMENSIONS",
            1024,
        )
        vision_dimensions = _environment_int(
            "ANIVAG_VISION_VECTOR_DIMENSIONS",
            1024,
        )
        if text_dimensions != vision_dimensions:
            raise ValueError(
                "text and vision vector dimensions must match for shared query vectors"
            )
        asr_mode = os.getenv("ANIVAG_ASR_MODE", parse_defaults.asr_mode).strip().casefold()
        if asr_mode not in {"lite", "none"}:
            raise ValueError("ANIVAG_ASR_MODE must be 'lite' or 'none'")
        origins = tuple(
            item.strip()
            for item in os.getenv(
                "ANIVAG_ALLOWED_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
            if item.strip()
        )
        model_config = os.getenv("ANIVAG_MODEL_CONFIG")
        model_pricing_config = _optional_environment("ANIVAG_MODEL_PRICING_CONFIG")
        agent_default_top_k = _environment_int(
            "ANIVAG_AGENT_DEFAULT_TOP_K", agent_defaults.default_top_k
        )
        agent_maximum_top_k = _environment_int(
            "ANIVAG_AGENT_MAX_TOP_K", agent_defaults.maximum_top_k
        )
        if agent_maximum_top_k < agent_default_top_k:
            raise ValueError(
                "ANIVAG_AGENT_MAX_TOP_K must be at least ANIVAG_AGENT_DEFAULT_TOP_K"
            )
        agent_default_model_calls = _environment_int(
            "ANIVAG_AGENT_MAX_MODEL_CALLS_PER_TURN",
            agent_defaults.max_model_calls_per_turn,
        )
        agent_maximum_model_calls = _environment_int(
            "ANIVAG_AGENT_MAXIMUM_MODEL_CALLS_PER_TURN",
            max(
                agent_defaults.maximum_model_calls_per_turn,
                agent_default_model_calls,
            ),
        )
        agent_default_tool_calls = _environment_int(
            "ANIVAG_AGENT_MAX_TOOL_CALLS_PER_TURN",
            agent_defaults.max_tool_calls_per_turn,
        )
        agent_maximum_tool_calls = _environment_int(
            "ANIVAG_AGENT_MAXIMUM_TOOL_CALLS_PER_TURN",
            max(
                agent_defaults.maximum_tool_calls_per_turn,
                agent_default_tool_calls,
            ),
        )
        return cls(
            environment=environment,
            database_url=database_url,
            artifact_root=artifact_root,
            logging=LoggingConfig(
                directory=Path(os.getenv("ANIVAG_LOG_DIRECTORY", str(logging_defaults.directory))).expanduser(),
                level=os.getenv("ANIVAG_LOG_LEVEL", logging_defaults.level).strip().casefold(),
                maximum_file_size_bytes=_environment_int("ANIVAG_LOG_MAX_BYTES", logging_defaults.maximum_file_size_bytes),
                backup_count=_environment_int("ANIVAG_LOG_BACKUP_COUNT", logging_defaults.backup_count),
            ),
            images=ImageUploadConfig(
                maximum_upload_size_bytes=_environment_int(
                    "ANIVAG_MAX_IMAGE_UPLOAD_SIZE_BYTES", image_defaults.maximum_upload_size_bytes,
                ),
                maximum_pixels=_environment_int(
                    "ANIVAG_MAX_IMAGE_PIXELS", image_defaults.maximum_pixels,
                ),
                model_max_edge=_environment_int(
                    "ANIVAG_BACKGROUND_IMAGE_MAX_EDGE", image_defaults.model_max_edge,
                ),
            ),
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_api_key=os.getenv("ANIVAG_QDRANT_API_KEY") or None,
            text_vector_dimensions=text_dimensions,
            vision_vector_dimensions=vision_dimensions,
            upload=UploadServiceConfig(
                preferred_segment_size_bytes=segment_size,
                maximum_upload_size_bytes=max_upload_size,
                session_ttl=timedelta(
                    seconds=_environment_int(
                        "ANIVAG_UPLOAD_SESSION_TTL_SECONDS",
                        int(upload_defaults.session_ttl.total_seconds()),
                    )
                ),
                completion_stale_after=timedelta(
                    seconds=_environment_int(
                        "ANIVAG_UPLOAD_COMPLETION_STALE_SECONDS",
                        int(upload_defaults.completion_stale_after.total_seconds()),
                    )
                ),
                pipeline_version=os.getenv(
                    "ANIVAG_PIPELINE_VERSION",
                    upload_defaults.pipeline_version,
                ),
                max_job_attempts=_environment_int(
                    "ANIVAG_PARSE_JOB_MAX_ATTEMPTS",
                    upload_defaults.max_job_attempts,
                ),
            ),
            parse=VideoParseRuntimeConfig(
                chunk_duration_ms=_environment_int(
                    "ANIVAG_CHUNK_DURATION_MS",
                    parse_defaults.chunk_duration_ms,
                ),
                context_overlap_ms=_environment_nonnegative_int(
                    "ANIVAG_CONTEXT_OVERLAP_MS",
                    parse_defaults.context_overlap_ms,
                ),
                max_height=_environment_int(
                    "ANIVAG_PARSE_MAX_HEIGHT", parse_defaults.max_height
                ),
                asr_concurrency=_environment_int(
                    "ANIVAG_ASR_CONCURRENCY", parse_defaults.asr_concurrency
                ),
                asr_mode=cast(Literal["lite", "none"], asr_mode),
                request_timeout_seconds=_environment_float(
                    "ANIVAG_PARSE_REQUEST_TIMEOUT_SECONDS",
                    parse_defaults.request_timeout_seconds,
                ),
                max_context_events=_environment_int(
                    "ANIVAG_MAX_CONTEXT_EVENTS",
                    parse_defaults.max_context_events,
                ),
                max_context_chars=_environment_int(
                    "ANIVAG_MAX_CONTEXT_CHARS",
                    parse_defaults.max_context_chars,
                ),
            ),
            agent=AgentServiceConfig(
                history_message_limit=_environment_int(
                    "ANIVAG_AGENT_HISTORY_MESSAGE_LIMIT",
                    agent_defaults.history_message_limit,
                ),
                default_top_k=agent_default_top_k,
                maximum_top_k=agent_maximum_top_k,
                max_loop_turns=_environment_int(
                    "ANIVAG_AGENT_MAX_LOOP_TURNS", agent_defaults.max_loop_turns,
                ),
                max_model_calls_per_turn=agent_default_model_calls,
                maximum_model_calls_per_turn=agent_maximum_model_calls,
                max_tool_calls_per_turn=agent_default_tool_calls,
                maximum_tool_calls_per_turn=agent_maximum_tool_calls,
                max_output_tokens=_environment_int(
                    "ANIVAG_AGENT_MAX_OUTPUT_TOKENS", agent_defaults.max_output_tokens,
                ),
                max_prompt_chars=_environment_int(
                    "ANIVAG_AGENT_MAX_PROMPT_CHARS", agent_defaults.max_prompt_chars,
                ),
                context_window_tokens=_environment_int(
                    "ANIVAG_AGENT_CONTEXT_WINDOW_TOKENS",
                    agent_defaults.context_window_tokens,
                ),
                context_safety_margin_tokens=_environment_int(
                    "ANIVAG_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS",
                    agent_defaults.context_safety_margin_tokens,
                ),
                auto_compact_tokens=_environment_int(
                    "ANIVAG_AGENT_AUTO_COMPACT_TOKENS",
                    agent_defaults.auto_compact_tokens,
                ),
                compact_target_tokens=_environment_int(
                    "ANIVAG_AGENT_COMPACT_TARGET_TOKENS",
                    agent_defaults.compact_target_tokens,
                ),
                max_tool_result_bytes=_environment_int(
                    "ANIVAG_AGENT_MAX_TOOL_RESULT_BYTES",
                    agent_defaults.max_tool_result_bytes,
                ),
                max_catalog_items_per_page=_environment_int(
                    "ANIVAG_AGENT_MAX_CATALOG_ITEMS_PER_PAGE",
                    agent_defaults.max_catalog_items_per_page,
                ),
                execution_timeout_seconds=_environment_float(
                    "ANIVAG_AGENT_EXECUTION_TIMEOUT_SECONDS",
                    agent_defaults.execution_timeout_seconds,
                ),
                maximum_concurrent_kicks=_environment_int(
                    "ANIVAG_AGENT_MAX_CONCURRENT_KICKS",
                    agent_defaults.maximum_concurrent_kicks,
                ),
            ),
            agent_query_model=_optional_environment("ANIVAG_AGENT_QUERY_MODEL"),
            agent_chat_model=_optional_environment(
                "ANIVAG_AGENT_CHAT_MODEL",
                legacy_name="ANIVAG_DEEPSEEK_CHAT_MODEL",
            ),
            agent_fallback_model=_optional_environment(
                "ANIVAG_AGENT_FALLBACK_MODEL"
            ),
            agent_evaluator_model=_optional_environment(
                "ANIVAG_AGENT_EVALUATOR_MODEL"
            ),
            agent_request_timeout_seconds=_environment_float(
                "ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS",
                180,
            ),
            mq=MQConsumerConfig(
                consumer_id=(
                    _optional_environment("ANIVAG_MQ_CONSUMER_ID")
                    or _optional_environment("ANIVAG_WORKER_ID")
                    or f"local-consumer-{os.getpid()}"
                ),
                lease_duration=timedelta(seconds=lease_seconds),
                heartbeat_interval=timedelta(seconds=heartbeat_seconds),
                poll_interval_seconds=_environment_float(
                    "ANIVAG_MQ_POLL_SECONDS",
                    2.0,
                    legacy_name="ANIVAG_WORKER_POLL_SECONDS",
                ),
                retry_base=timedelta(seconds=retry_base_seconds),
                retry_max=timedelta(seconds=retry_max_seconds),
            ),
            index_sync=IndexSyncConfig(
                retry_base_seconds=_environment_int(
                    "ANIVAG_INDEX_SYNC_RETRY_BASE_SECONDS",
                    index_sync_defaults.retry_base_seconds,
                    legacy_name="ANIVAG_WORKER_RETRY_BASE_SECONDS",
                ),
                retry_max_seconds=_environment_int(
                    "ANIVAG_INDEX_SYNC_RETRY_MAX_SECONDS",
                    index_sync_defaults.retry_max_seconds,
                    legacy_name="ANIVAG_WORKER_RETRY_MAX_SECONDS",
                ),
            ),
            allowed_origins=origins,
            model_config_path=(
                Path(model_config).expanduser() if model_config else None
            ),
            model_pricing_path=(
                Path(model_pricing_config).expanduser() if model_pricing_config else None
            ),
            sse=SSEConfig(
                poll_interval_seconds=_environment_float(
                    "ANIVAG_SSE_POLL_SECONDS", sse_defaults.poll_interval_seconds
                ),
                keepalive_interval_seconds=_environment_float(
                    "ANIVAG_SSE_KEEPALIVE_SECONDS", sse_defaults.keepalive_interval_seconds
                ),
            ),
            retrieval=RetrievalConfig(
                channel_top_k=_environment_int(
                    "ANIVAG_RETRIEVAL_CHANNEL_TOP_K", retrieval_defaults.channel_top_k
                ),
                rrf_k=_environment_int(
                    "ANIVAG_RETRIEVAL_RRF_K", retrieval_defaults.rrf_k
                ),
                text_weight=float(
                    os.getenv(
                        "ANIVAG_RETRIEVAL_TEXT_WEIGHT", str(retrieval_defaults.text_weight)
                    )
                ),
                vision_weight=float(
                    os.getenv(
                        "ANIVAG_RETRIEVAL_VISION_WEIGHT", str(retrieval_defaults.vision_weight)
                    )
                ),
            ),
            database_pool_recycle_seconds=_environment_int(
                "ANIVAG_DATABASE_POOL_RECYCLE_SECONDS", 3600
            ),
            video_embedding_model=_optional_environment("ANIVAG_VIDEO_EMBEDDING_MODEL"),
            video_embedding_fps=(
                _environment_float("ANIVAG_VIDEO_EMBEDDING_FPS", 1.0)
                if _optional_environment("ANIVAG_VIDEO_EMBEDDING_FPS") is not None
                else None
            ),
        )


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required environment variable {name}")
    return value.strip()


def _optional_environment(
    name: str,
    *,
    legacy_name: str | None = None,
) -> str | None:
    value = os.getenv(name)
    if value is None and legacy_name is not None:
        value = os.getenv(legacy_name)
    return value.strip() if value and value.strip() else None


def _environment_int(
    name: str,
    default: int,
    *,
    legacy_name: str | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None and legacy_name is not None:
        raw = os.getenv(legacy_name)
    value = int(raw if raw is not None else default)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _environment_float(
    name: str,
    default: float,
    *,
    legacy_name: str | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None and legacy_name is not None:
        raw = os.getenv(legacy_name)
    value = float(raw if raw is not None else default)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return value


def _environment_nonnegative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value
