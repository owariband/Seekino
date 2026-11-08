"""Best-effort visible text snapshots for one Parse model call."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseModel
from .video import ID_LENGTH, LONG_TEXT, MYSQL_TABLE_OPTIONS, UTC_TIMESTAMP


class ParseModelOutputDraft(BaseModel):
    __tablename__ = "parse_model_output_draft"
    __table_args__ = (
        CheckConstraint(
            "status IN ('streaming', 'completed', 'failed', 'cancelled')",
            name="ck_parse_model_output_draft_status",
        ),
        {**MYSQL_TABLE_OPTIONS, "mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    video_task_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
