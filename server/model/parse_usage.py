from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .video import ID_LENGTH, MYSQL_TABLE_OPTIONS, TimestampedRecord


class ParseUsageAttemptRecord(Base):
    __tablename__ = "parse_usage_attempt"
    __table_args__ = MYSQL_TABLE_OPTIONS

    job_id: Mapped[str] = mapped_column(ForeignKey("series_parse_job.job_id"), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    recording_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ParseModelCallRecord(TimestampedRecord, Base):
    __tablename__ = "parse_model_call"
    __table_args__ = (
        CheckConstraint("category IN ('parse', 'embedding')", name="ck_parse_model_call_category"),
        CheckConstraint("status IN ('completed', 'failed')", name="ck_parse_model_call_status"),
        Index("ix_parse_model_call_job_started", "job_id", "started_at"),
        MYSQL_TABLE_OPTIONS,
    )

    call_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("series_parse_job.job_id"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    series_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    video_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    sequence_no: Mapped[int | None] = mapped_column(Integer)
    video_chunk_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tokens: Mapped[dict | None] = mapped_column(JSON)
    # A canonical decimal string preserves all digits on MySQL and isolated SQLite alike.
    estimated_cost_cny: Mapped[str | None] = mapped_column(String(128))
    price_snapshot: Mapped[dict | None] = mapped_column(JSON)
