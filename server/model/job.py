from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .video import ID_LENGTH, LONG_TEXT, MYSQL_TABLE_OPTIONS, TimestampedRecord


class ParseJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ParseJobStage(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    SPLIT_READY = "SPLIT_READY"
    PARSING = "PARSING"
    TEXT_EMBEDDING = "TEXT_EMBEDDING"
    VISION_EMBEDDING = "VISION_EMBEDDING"
    PERSISTING = "PERSISTING"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"


class VideoChunkCheckpointStage(StrEnum):
    PREPARED = "PREPARED"
    SPLIT_READY = "SPLIT_READY"
    PARSED = "PARSED"
    TEXT_EMBEDDED = "TEXT_EMBEDDED"
    VISION_EMBEDDED = "VISION_EMBEDDED"
    INDEXED = "INDEXED"


class SeriesParseJobRecord(TimestampedRecord, Base):
    __tablename__ = "series_parse_job"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "pipeline_version",
            name="uq_series_parse_job_video_pipeline",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT', "
            "'SUCCEEDED', 'FAILED')",
            name="ck_series_parse_job_status",
        ),
        CheckConstraint(
            "stage IN ('QUEUED', 'PREPARING', 'SPLIT_READY', 'PARSING', "
            "'TEXT_EMBEDDING', 'VISION_EMBEDDING', 'PERSISTING', "
            "'INDEXING', 'COMPLETED')",
            name="ck_series_parse_job_stage",
        ),
        CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="ck_series_parse_job_progress",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_series_parse_job_attempt_count",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_series_parse_job_max_attempts",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_series_parse_job_lease_version",
        ),
        Index("ix_series_parse_job_due", "status", "available_at"),
        MYSQL_TABLE_OPTIONS,
    )

    job_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
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
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False)
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)


class VideoChunkCheckpointRecord(Base):
    __tablename__ = "video_chunk_checkpoint"
    __table_args__ = (
        CheckConstraint(
            "sequence_no >= 0",
            name="ck_video_chunk_checkpoint_sequence",
        ),
        CheckConstraint(
            "stage IN ('PREPARED', 'SPLIT_READY', 'PARSED', "
            "'TEXT_EMBEDDED', 'VISION_EMBEDDED', 'INDEXED')",
            name="ck_video_chunk_checkpoint_stage",
        ),
        Index("ix_video_chunk_checkpoint_job_stage", "job_id", "stage"),
        MYSQL_TABLE_OPTIONS,
    )

    job_id: Mapped[str] = mapped_column(
        ForeignKey("series_parse_job.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_chunk_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    chunk_media_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    analysis_window_media_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    parse_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    text_embedding_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    vision_embedding_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
