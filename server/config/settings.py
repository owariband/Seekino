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
    agent_evaluator_model: str | None
    agent_request_timeout_seconds: float
    worker_id: str
    worker_lease_duration: timedelta
    worker_heartbeat_interval: timedelta
    worker_poll_interval_seconds: float
    worker_retry_base: timedelta
    worker_retry_max: timedelta
    allowed_origins: tuple[str, ...]
    model_config_path: Path | None
    sse: SSEConfig = field(default_factory=SSEConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    database_pool_recycle_seconds: int = 3600
    video_embedding_model: str | None = None
    video_embedding_fps: float | None = None

    @classmethod
    def from_environment(cls) -> "ServerSettings":
        upload_defaults = UploadServiceConfig()
        parse_defaults = VideoParseRuntimeConfig()
        agent_defaults = AgentServiceConfig()
        sse_defaults = SSEConfig()
        retrieval_defaults = RetrievalConfig()
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
        lease_seconds = _environment_int("ANIVAG_WORKER_LEASE_SECONDS", 300)
        heartbeat_seconds = _environment_int(
            "ANIVAG_WORKER_HEARTBEAT_SECONDS",
            60,
        )
        if heartbeat_seconds >= lease_seconds:
            raise ValueError(
                "ANIVAG_WORKER_HEARTBEAT_SECONDS must be smaller than the lease"
            )
        retry_base_seconds = _environment_int(
            "ANIVAG_WORKER_RETRY_BASE_SECONDS",
            30,
        )
        retry_max_seconds = _environment_int(
            "ANIVAG_WORKER_RETRY_MAX_SECONDS",
            900,
        )
        if retry_max_seconds < retry_base_seconds:
            raise ValueError(
                "ANIVAG_WORKER_RETRY_MAX_SECONDS must be at least the retry base"
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
        return cls(
            environment=environment,
            database_url=database_url,
            artifact_root=artifact_root,
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
            ),
            agent_chat_model=_optional_environment("ANIVAG_AGENT_CHAT_MODEL"),
            agent_evaluator_model=_optional_environment(
                "ANIVAG_AGENT_EVALUATOR_MODEL"
            ),
            agent_request_timeout_seconds=_environment_float(
                "ANIVAG_AGENT_REQUEST_TIMEOUT_SECONDS",
                180,
            ),
            worker_id=os.getenv("ANIVAG_WORKER_ID", f"local-worker-{os.getpid()}"),
            worker_lease_duration=timedelta(seconds=lease_seconds),
            worker_heartbeat_interval=timedelta(seconds=heartbeat_seconds),
            worker_poll_interval_seconds=_environment_float(
                "ANIVAG_WORKER_POLL_SECONDS",
                2.0,
            ),
            worker_retry_base=timedelta(seconds=retry_base_seconds),
            worker_retry_max=timedelta(seconds=retry_max_seconds),
            allowed_origins=origins,
            model_config_path=(
                Path(model_config).expanduser() if model_config else None
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


def _optional_environment(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _environment_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _environment_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return value


def _environment_nonnegative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value
