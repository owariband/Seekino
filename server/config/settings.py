from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal, cast

from engine.common.config import VideoParseRuntimeConfig

from ..database import DatabaseEnvironment
from .agent import AgentServiceConfig
from .auth import AuthConfig
from .elasticsearch import ElasticsearchConfig, resolve_vector_dimensions
from .global_agent_settings import read_global_agent_settings
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
    text_vector_dimensions: int
    vision_vector_dimensions: int
    upload: UploadServiceConfig
    parse: VideoParseRuntimeConfig
    agent: AgentServiceConfig
    agent_chat_model: str | None
    agent_fallback_model: str | None
    agent_evaluator_model: str | None
    agent_request_timeout_seconds: float | None
    mq: MQConsumerConfig
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
    elasticsearch: ElasticsearchConfig | None = None
    parse_execution_mode: Literal["v2"] = "v2"
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def from_environment(cls) -> "ServerSettings":
        upload_defaults = UploadServiceConfig()
        parse_defaults = VideoParseRuntimeConfig()
        sse_defaults = SSEConfig()
        image_defaults = ImageUploadConfig()
        logging_defaults = LoggingConfig()
        environment = resolve_environment()
        database_url = resolve_database_url()
        artifact_root = resolve_artifact_root()
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
        text_dimensions, vision_dimensions = resolve_vector_dimensions(require_matching=True)
        asr_mode = os.getenv("ANIVAG_ASR_MODE", parse_defaults.asr_mode).strip().casefold()
        if asr_mode not in {"lite", "none"}:
            raise ValueError("ANIVAG_ASR_MODE must be 'lite' or 'none'")
        parse_execution_mode = resolve_execution_mode()
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
        agent_config, _ = read_global_agent_settings(model_config)
        return cls(
            auth=AuthConfig.from_environment(),
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
            elasticsearch=ElasticsearchConfig.from_environment(environment),
            parse_execution_mode=parse_execution_mode,
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
                video_concurrency=_environment_int("ANIVAG_VIDEO_CONCURRENCY", parse_defaults.video_concurrency),
                parse_concurrency=_environment_int("ANIVAG_PARSE_CONCURRENCY", parse_defaults.parse_concurrency),
                refine_concurrency=_environment_int("ANIVAG_REFINE_CONCURRENCY", parse_defaults.refine_concurrency),
                index_concurrency=_environment_int("ANIVAG_INDEX_CONCURRENCY", parse_defaults.index_concurrency),
                max_model_calls=_environment_int("ANIVAG_MAX_MODEL_CALLS", parse_defaults.max_model_calls),
                parse_execution_timeout_seconds=_environment_float("ANIVAG_PARSE_EXECUTION_TIMEOUT_SECONDS", parse_defaults.parse_execution_timeout_seconds),
                refine_execution_timeout_seconds=_environment_float("ANIVAG_REFINE_EXECUTION_TIMEOUT_SECONDS", parse_defaults.refine_execution_timeout_seconds),
                index_execution_timeout_seconds=_environment_float("ANIVAG_INDEX_EXECUTION_TIMEOUT_SECONDS", parse_defaults.index_execution_timeout_seconds),
                batch_max_size=_environment_int(
                    "ANIVAG_PARSE_BATCH_MAX_SIZE", parse_defaults.batch_max_size,
                ),
                dispatch_interval_seconds=float(os.getenv(
                    "ANIVAG_PARSE_DISPATCH_INTERVAL_SECONDS",
                    str(parse_defaults.dispatch_interval_seconds),
                )),
                chunk_duration_seconds=_environment_int(
                    "ANIVAG_CHUNK_DURATION_SECONDS",
                    parse_defaults.chunk_duration_seconds,
                ),
                context_overlap_seconds=_environment_nonnegative_int(
                    "ANIVAG_CONTEXT_OVERLAP_SECONDS",
                    parse_defaults.context_overlap_seconds,
                ),
                max_height=resolve_parse_max_height(),
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
            agent=agent_config,
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
            agent_request_timeout_seconds=resolve_agent_request_timeout_seconds(),
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
            retrieval=RetrievalConfig.from_environment(),
            database_pool_recycle_seconds=resolve_database_pool_recycle_seconds(),
            video_embedding_model=_optional_environment("ANIVAG_VIDEO_EMBEDDING_MODEL"),
            video_embedding_fps=(
                _environment_float("ANIVAG_VIDEO_EMBEDDING_FPS", 1.0)
                if _optional_environment("ANIVAG_VIDEO_EMBEDDING_FPS") is not None
                else None
            ),
        )


def resolve_environment(value: DatabaseEnvironment | str | None = None) -> DatabaseEnvironment:
    return DatabaseEnvironment(
        value if value is not None
        else os.getenv("ANIVAG_ENVIRONMENT", DatabaseEnvironment.DEVELOPMENT.value)
    )


def resolve_database_url(value: str | None = None) -> str:
    return _required_environment("ANIVAG_DATABASE_URL", value=value)


def resolve_database_pool_recycle_seconds(value: int | str | None = None) -> int:
    return _environment_int("ANIVAG_DATABASE_POOL_RECYCLE_SECONDS", 3600, value=value)


def resolve_artifact_root(value: Path | str | None = None) -> Path:
    return Path(
        value if value is not None else os.getenv("ANIVAG_ARTIFACT_ROOT", "outputs/server/artifacts")
    ).expanduser()


def resolve_parse_max_height(value: int | str | None = None) -> int:
    return _environment_int("ANIVAG_PARSE_MAX_HEIGHT", VideoParseRuntimeConfig().max_height, value=value)


def resolve_execution_mode(value: str | None = None) -> Literal["v2"]:
    resolved = (value if value is not None else os.getenv("ANIVAG_PARSE_EXECUTION_MODE", "v2"))
    if resolved.strip().casefold() != "v2":
        raise ValueError("ANIVAG_PARSE_EXECUTION_MODE must be 'v2'; V1 execution is retired")
    return "v2"


def resolve_agent_request_timeout_seconds(value: float | str | None = None) -> float | None:
    name = "ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS"
    raw = value if value is not None else _optional_environment(name)
    if raw is None:
        return None
    timeout = float(raw)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return timeout


def _required_environment(name: str, *, value: str | None = None) -> str:
    value = value if value is not None else os.getenv(name)
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
    value: int | str | None = None,
) -> int:
    raw = value if value is not None else os.getenv(name)
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
    value: float | str | None = None,
) -> float:
    raw = value if value is not None else os.getenv(name)
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
