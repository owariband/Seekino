from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .video import ID_LENGTH, LONG_TEXT, MYSQL_TABLE_OPTIONS, TimestampedRecord


class UploadSessionStatus(StrEnum):
    UPLOADING = "UPLOADING"
    COMPLETING = "COMPLETING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


class UploadCompletionStage(StrEnum):
    VERIFYING = "VERIFYING"
    ASSEMBLING = "ASSEMBLING"
    PROBING = "PROBING"
    CREATING_JOB = "CREATING_JOB"


class UploadSessionRecord(TimestampedRecord, Base):
    __tablename__ = "upload_session"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_upload_session_workspace_idempotency",
        ),
        UniqueConstraint(
            "active_slot_key",
            name="uq_upload_session_active_slot",
        ),
        CheckConstraint("episode_order > 0", name="ck_upload_session_episode_order"),
        CheckConstraint(
            "upload_sequence > 0",
            name="ck_upload_session_upload_sequence",
        ),
        CheckConstraint(
            "total_size_bytes > 0",
            name="ck_upload_session_total_size",
        ),
        CheckConstraint(
            "preferred_segment_size_bytes > 0",
            name="ck_upload_session_segment_size",
        ),
        CheckConstraint(
            "total_segments > 0",
            name="ck_upload_session_total_segments",
        ),
        CheckConstraint("version > 0", name="ck_upload_session_version"),
        CheckConstraint(
            "completion_stage IS NULL OR completion_stage IN ('VERIFYING', 'ASSEMBLING', 'PROBING', 'CREATING_JOB')",
            name="ck_upload_session_completion_stage",
        ),
        CheckConstraint(
            "status IN ('UPLOADING', 'COMPLETING', 'COMPLETED', 'EXPIRED')",
            name="ck_upload_session_status",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    upload_session_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.series_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    episode_order: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    preferred_segment_size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    total_segments: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    completion_stage: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    active_slot_key: Mapped[str | None] = mapped_column(String(96))
    completion_token: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    completion_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False)
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    video_id: Mapped[str | None] = mapped_column(
        ForeignKey("video.video_id", ondelete="SET NULL"),
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("series_parse_job.job_id", ondelete="SET NULL"), index=True
    )


class UploadSegmentRecord(Base):
    __tablename__ = "upload_segment"
    __table_args__ = (
        CheckConstraint(
            "segment_number >= 0",
            name="ck_upload_segment_number",
        ),
        CheckConstraint(
            "start_byte >= 0 AND end_byte_exclusive > start_byte",
            name="ck_upload_segment_range",
        ),
        CheckConstraint(
            "actual_size_bytes = end_byte_exclusive - start_byte",
            name="ck_upload_segment_size",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    upload_session_id: Mapped[str] = mapped_column(
        ForeignKey("upload_session.upload_session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    segment_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_byte: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_byte_exclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
    )
