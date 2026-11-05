"""Persisted Video, Chunk and Interval results, separate from execution state."""
from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseModel
from .types import StringEncodedJSON
from .video import ID_LENGTH, MYSQL_TABLE_OPTIONS


class VideoParseContent(BaseModel):
    __tablename__ = "video_parse_content"
    __table_args__ = (
        UniqueConstraint("video_task_id", name="uq_video_parse_content_task"),
        CheckConstraint("version > 0", name="ck_video_parse_content_version"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    video_task_id: Mapped[str] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="CASCADE"), nullable=False,
    )
    asr: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    diarization: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    display: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outline: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ChunkParseContent(BaseModel):
    __tablename__ = "chunk_parse_content"
    __table_args__ = (
        UniqueConstraint(
            "chunk_task_id",
            name="uq_chunk_parse_content_task",
        ),
        CheckConstraint("version > 0", name="ck_chunk_parse_content_version"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    chunk_task_id: Mapped[str] = mapped_column(
        ForeignKey("chunk_parse_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[Any | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detail: Mapped[dict[str, Any] | None] = mapped_column(
        StringEncodedJSON(string_path=("_index_embeddings_v2", "representations")),
    )


class IntervalParseContent(BaseModel):
    __tablename__ = "interval_parse_content"
    __table_args__ = MYSQL_TABLE_OPTIONS

    interval_task_id: Mapped[str] = mapped_column(
        ForeignKey("interval_parse_task.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
