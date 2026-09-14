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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .video import ID_LENGTH, LONG_TEXT, MYSQL_TABLE_OPTIONS, TimestampedRecord


class IndexSyncOperation(StrEnum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"


class IndexSyncStatus(StrEnum):
    PENDING = "PENDING"
    RETRY_WAIT = "RETRY_WAIT"
    SYNCED = "SYNCED"


class IndexSyncIntentRecord(TimestampedRecord, Base):
    __tablename__ = "index_sync_intent"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('UPSERT', 'DELETE')",
            name="ck_index_sync_intent_operation",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RETRY_WAIT', 'SYNCED')",
            name="ck_index_sync_intent_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_index_sync_intent_attempt_count",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_index_sync_intent_generation",
        ),
        Index("ix_index_sync_intent_due", "status", "available_at"),
        MYSQL_TABLE_OPTIONS,
    )

    video_segment_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("video_segment.video_segment_id", ondelete="CASCADE"),
        primary_key=True,
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
