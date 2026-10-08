from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT, TIMESTAMP
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ..database import Base
from ..utils.time import (
    normalize_database_utc_datetime,
    now_unix_time_ms,
    now_utc,
    require_utc_datetime,
)

ID_LENGTH = 36
LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql")
MYSQL_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
}


class UtcTimestamp(TypeDecorator[datetime]):
    """TIMESTAMP(3) on MySQL with aware UTC values at the ORM boundary."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "mysql":
            return dialect.type_descriptor(TIMESTAMP(fsp=3))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        # MySQL TIMESTAMP uses the connection time zone. The engine pins that
        # zone to UTC, so bind a UTC-naive wall time for DBAPI compatibility.
        instant = require_utc_datetime(value)
        return instant.replace(
            microsecond=(instant.microsecond // 1_000) * 1_000,
            tzinfo=None,
        )

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        return normalize_database_utc_datetime(value)


UTC_TIMESTAMP = UtcTimestamp()


class VideoIndexStatus(StrEnum):
    NOT_READY = "NOT_READY"
    SYNCING = "SYNCING"
    READY = "READY"
    FAILED = "FAILED"


class TimestampedRecord:
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
        onupdate=now_unix_time_ms,
    )


class SeriesRecord(TimestampedRecord, Base):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_series_workspace_name"),
        MYSQL_TABLE_OPTIONS,
    )

    series_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(LONG_TEXT)
    transcript_language_hint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    important_extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    character_bible: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )


class VideoRecord(TimestampedRecord, Base):
    __tablename__ = "video"
    __table_args__ = (
        UniqueConstraint("series_id", "episode_order", name="uq_video_series_order"),
        UniqueConstraint("series_id", "content_hash", name="uq_video_series_hash"),
        CheckConstraint("episode_order > 0", name="ck_video_episode_order"),
        CheckConstraint("duration_seconds > 0", name="ck_video_duration"),
        CheckConstraint("total_bytes >= 0", name="ck_video_total_bytes"),
        CheckConstraint(
            "upload_progress BETWEEN 0 AND 100",
            name="ck_video_upload_progress",
        ),
        CheckConstraint(
            "parse_progress BETWEEN 0 AND 100",
            name="ck_video_parse_progress",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    video_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.series_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    episode_order: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_source_uri: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    inference_proxy_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Media durations and coordinates are integer milliseconds; legacy names remain.
    duration_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="media_time_milliseconds",
    )
    upload_status: Mapped[str] = mapped_column(String(32), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    upload_progress: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_progress: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
        onupdate=now_utc,
    )


class VideoChunkRecord(TimestampedRecord, Base):
    __tablename__ = "video_chunk"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "sequence_no",
            name="uq_video_chunk_video_sequence",
        ),
        CheckConstraint("sequence_no >= 0", name="ck_video_chunk_sequence"),
        CheckConstraint(
            "start_seconds >= 0 AND end_seconds > start_seconds",
            name="ck_video_chunk_range",
        ),
        CheckConstraint(
            "analysis_window_start_seconds <= start_seconds "
            "AND analysis_window_end_seconds >= end_seconds",
            name="ck_video_chunk_analysis_range",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    video_chunk_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
    )
    video_id: Mapped[str] = mapped_column(
        ForeignKey("video.video_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_window_start_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_window_end_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_media_ref: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    analysis_window_media_ref: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    baseline_visual_caption: Mapped[str | None] = mapped_column(LONG_TEXT)
    # The legacy column name is retained for schema compatibility. New values
    # are {"uri": str, "at_seconds": int} objects; historical URI strings remain
    # readable through VideoChunkKeyframeReference.
    keyframe_uris: Mapped[list[str | dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
        onupdate=now_utc,
    )


class VideoChunkInfoRecord(Base):
    __tablename__ = "video_chunk_info"
    __table_args__ = MYSQL_TABLE_OPTIONS

    video_chunk_id: Mapped[str] = mapped_column(
        ForeignKey("video_chunk.video_chunk_id", ondelete="CASCADE"),
        primary_key=True,
    )
    all_time_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    all_time_event_text: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    video_compact: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    context_before_compact: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    context_after_compact: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    window_summary: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(255))
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parse_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parsed_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )


class ContextSnapshotRecord(Base):
    __tablename__ = "context_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "after_chunk_sequence_no",
            name="uq_context_snapshot_video_sequence",
        ),
        CheckConstraint(
            "after_chunk_sequence_no >= 0",
            name="ck_context_snapshot_sequence",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    snapshot_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    engine_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.series_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    video_id: Mapped[str] = mapped_column(
        ForeignKey("video.video_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    after_chunk_sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    compact_text: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    structured_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_from_video_chunk_id: Mapped[str] = mapped_column(
        ForeignKey("video_chunk.video_chunk_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )


class VideoChunkEmbeddingRecord(TimestampedRecord, Base):
    __tablename__ = "video_chunk_embedding"
    __table_args__ = MYSQL_TABLE_OPTIONS

    video_chunk_id: Mapped[str] = mapped_column(
        ForeignKey("video_chunk.video_chunk_id", ondelete="CASCADE"),
        primary_key=True,
    )
    text_input: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    text_vector: Mapped[list[float] | None] = mapped_column(JSON)
    vision_vector: Mapped[list[float] | None] = mapped_column(JSON)
    text_embedding_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    vision_embedding_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
