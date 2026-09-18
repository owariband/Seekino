from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .video import ID_LENGTH, MYSQL_TABLE_OPTIONS


class ParseJobAttemptOutcome(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY = "RETRY"
    FAILED = "FAILED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


class VideoChunkTimingStage(StrEnum):
    SPLIT = "SPLIT"
    ASR = "ASR"
    PARSE = "PARSE"
    TEXT_EMBEDDING = "TEXT_EMBEDDING"
    VISION_EMBEDDING = "VISION_EMBEDDING"
    INDEX = "INDEX"


class VideoChunkTimingStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


class ParseJobAttemptRecord(Base):
    """One durable worker execution attempt for a parse job.

    Wall-clock values are only for observability.  ``duration_ms`` is written
    from the worker's monotonic clock, so clock changes never affect totals.
    """

    __tablename__ = "parse_job_attempt_timing"
    __table_args__ = (
        CheckConstraint(
            "attempt_count > 0",
            name="ck_parse_job_attempt_timing_attempt_count",
        ),
        CheckConstraint(
            "lease_version > 0",
            name="ck_parse_job_attempt_timing_lease_version",
        ),
        CheckConstraint(
            "started_at >= 0 AND (completed_at IS NULL OR completed_at >= 0)",
            name="ck_parse_job_attempt_timing_timestamps",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_parse_job_attempt_timing_duration",
        ),
        CheckConstraint(
            "outcome IN ('RUNNING', 'SUCCEEDED', 'RETRY', 'FAILED', "
            "'LEASE_EXPIRED')",
            name="ck_parse_job_attempt_timing_outcome",
        ),
        CheckConstraint(
            "(outcome = 'RUNNING' AND completed_at IS NULL "
            "AND duration_ms IS NULL AND timing_complete = 0) OR "
            "(outcome IN ('SUCCEEDED', 'RETRY', 'FAILED') "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND timing_complete = 1) OR "
            "(outcome = 'LEASE_EXPIRED' AND completed_at IS NOT NULL "
            "AND duration_ms IS NULL AND timing_complete = 0)",
            name="ck_parse_job_attempt_timing_terminal_state",
        ),
        Index(
            "ix_parse_job_attempt_timing_job_started",
            "job_id",
            "started_at",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    job_id: Mapped[str] = mapped_column(
        ForeignKey("series_parse_job.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    timing_complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class VideoChunkStageTimingRecord(Base):
    __tablename__ = "video_chunk_stage_timing"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "attempt_count", "lease_version"],
            [
                "parse_job_attempt_timing.job_id",
                "parse_job_attempt_timing.attempt_count",
                "parse_job_attempt_timing.lease_version",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "attempt_count > 0",
            name="ck_video_chunk_stage_timing_attempt_count",
        ),
        CheckConstraint(
            "lease_version > 0",
            name="ck_video_chunk_stage_timing_lease_version",
        ),
        CheckConstraint(
            "sequence_no >= 0",
            name="ck_video_chunk_stage_timing_sequence",
        ),
        CheckConstraint(
            "stage IN ('SPLIT', 'ASR', 'PARSE', 'TEXT_EMBEDDING', "
            "'VISION_EMBEDDING', 'INDEX')",
            name="ck_video_chunk_stage_timing_stage",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'LEASE_EXPIRED')",
            name="ck_video_chunk_stage_timing_status",
        ),
        CheckConstraint(
            "started_at >= 0 AND (completed_at IS NULL OR completed_at >= 0)",
            name="ck_video_chunk_stage_timing_timestamps",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_video_chunk_stage_timing_duration",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL "
            "AND duration_ms IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL) OR "
            "(status = 'LEASE_EXPIRED' AND completed_at IS NOT NULL "
            "AND duration_ms IS NULL)",
            name="ck_video_chunk_stage_timing_terminal_state",
        ),
        Index(
            "ix_video_chunk_stage_timing_job_sequence",
            "job_id",
            "sequence_no",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    job_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    video_chunk_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
