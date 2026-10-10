from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseModel
from .video import (
    ID_LENGTH,
    MYSQL_TABLE_OPTIONS,
    UTC_TIMESTAMP,
)


class ModelUsage(BaseModel):
    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('video_parse', 'chat_retrieval')",
            name="ck_model_usage_task_type",
        ),
        Index("ix_model_usage_tenant_started", "tenant_id", "start_time", "id"),
        Index("ix_model_usage_tenant_run_started", "tenant_id", "run_id", "start_time"),
        Index("ix_model_usage_tenant_actor_started", "tenant_id", "user_id", "start_time"),
        {**MYSQL_TABLE_OPTIONS, "mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[str] = mapped_column(String(256), nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, nullable=False)
    usage: Mapped[dict] = mapped_column(JSON, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)


class ParseUsageAttempt(BaseModel):
    __tablename__ = "parse_usage_attempt"
    __table_args__ = MYSQL_TABLE_OPTIONS

    job_id: Mapped[str] = mapped_column(
        ForeignKey("series_parse_job.id"),
        primary_key=True,
    )
    video_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="SET NULL"),
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, nullable=False)
    recording_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class VideoParseUsageAttempt(BaseModel):
    __tablename__ = "video_parse_usage_attempt"
    __table_args__ = (
        UniqueConstraint(
            "video_task_id",
            "execution_kind",
            "execution_task_id",
            "attempt_count",
            "lease_version",
            name="uq_video_parse_usage_attempt_lease",
        ),
        CheckConstraint(
            "execution_kind IN ('video', 'chunk', 'interval', 'prefix')",
            name="ck_video_parse_usage_attempt_execution_kind",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    video_task_id: Mapped[str] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_task_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, nullable=False)
    recording_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class ParseModelCall(BaseModel):
    __tablename__ = "parse_model_call"
    __table_args__ = (
        CheckConstraint(
            "category IN ('parse', 'embedding')",
            name="ck_parse_model_call_category",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_parse_model_call_status",
        ),
        Index("ix_parse_model_call_job_started", "job_id", "start_time"),
        Index(
            "ix_parse_model_call_video_parse_task_started",
            "video_task_id",
            "start_time",
        ),
        Index(
            "ix_parse_model_call_v2_execution",
            "video_task_id",
            "execution_kind",
            "execution_task_id",
            "start_time",
        ),
        CheckConstraint(
            "(job_id IS NOT NULL) <> (video_task_id IS NOT NULL)",
            name="ck_parse_model_call_exactly_one_task",
        ),
        CheckConstraint(
            "(video_task_id IS NULL "
            "AND execution_kind IS NULL AND execution_task_id IS NULL) OR "
            "(video_task_id IS NOT NULL "
            "AND execution_kind IS NOT NULL AND execution_task_id IS NOT NULL)",
            name="ck_parse_model_call_execution_identity",
        ),
        CheckConstraint(
            "execution_kind IS NULL OR "
            "execution_kind IN ('video', 'chunk', 'interval', 'prefix')",
            name="ck_parse_model_call_execution_kind",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("series_parse_job.id"),
    )
    video_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="CASCADE")
    )
    execution_kind: Mapped[str | None] = mapped_column(String(16))
    execution_task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    series_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    video_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    sequence_no: Mapped[int | None] = mapped_column(Integer)
    chunk_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tokens: Mapped[dict | None] = mapped_column(JSON)
    # A canonical decimal string preserves all digits on MySQL and isolated SQLite alike.
    estimated_cost_cny: Mapped[str | None] = mapped_column(String(128))
    price_snapshot: Mapped[dict | None] = mapped_column(JSON)
