from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..utils.time import now_unix_time_ms
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

    video_chunk_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("video_chunk.video_chunk_id", ondelete="CASCADE"),
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
    available_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    synced_at: Mapped[int | None] = mapped_column(BigInteger)
