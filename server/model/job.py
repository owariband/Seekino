from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Sequence

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseModel
from ..utils.time import now_unix_time_ms, now_utc
from .video import (
    ID_LENGTH,
    LONG_TEXT,
    MYSQL_TABLE_OPTIONS,
    UTC_TIMESTAMP,
)


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


class VideoParseTaskStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    COMPLETE = "COMPLETE"
    PARSING_FAILED = "PARSING_FAILED"
    INDEXING_FAILED = "INDEXING_FAILED"


class ChunkParseTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    UNDERSTANDING = "UNDERSTANDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class IntervalParseTaskStatus(StrEnum):
    WAITING = "WAITING"
    REFINING = "REFINING"
    INDEXING = "INDEXING"
    COMPLETE = "COMPLETE"
    PARSING_FAILED = "PARSING_FAILED"
    INDEXING_FAILED = "INDEXING_FAILED"


class SeriesParseJob(BaseModel):
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
        CheckConstraint(
            "processing_duration_ms >= 0",
            name="ck_series_parse_job_processing_duration",
        ),
        CheckConstraint("version >= 1", name="ck_series_parse_job_version"),
        Index("ix_series_parse_job_due", "status", "next_execute_time"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    creator_id: Mapped[str | None] = mapped_column(String(64))
    series_id: Mapped[str] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    video_id: Mapped[str] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    parse_config: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_execute_time: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expire_time: Mapped[int | None] = mapped_column(BigInteger)
    heartbeat_time: Mapped[int | None] = mapped_column(BigInteger)
    start_time: Mapped[int | None] = mapped_column(BigInteger)
    finish_time: Mapped[int | None] = mapped_column(BigInteger)
    processing_duration_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    processing_timing_complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)


class VideoChunkCheckpoint(BaseModel):
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
        CheckConstraint(
            "(start_ms IS NULL AND end_ms IS NULL) OR "
            "(start_ms IS NOT NULL AND end_ms IS NOT NULL AND "
            "start_ms >= 0 AND end_ms > start_ms)",
            name="ck_video_chunk_checkpoint_range",
        ),
        Index("ix_video_chunk_checkpoint_job_stage", "job_id", "stage"),
        MYSQL_TABLE_OPTIONS,
    )

    job_id: Mapped[str] = mapped_column(
        ForeignKey("series_parse_job.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    chunk_media_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    analysis_window_media_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    parse_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    text_embedding_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    vision_embedding_artifact_uri: Mapped[str | None] = mapped_column(LONG_TEXT)
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)


class VideoParseTask(BaseModel):
    __tablename__ = "video_parse_task"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "version",
            name="uq_video_parse_task_video_version",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PREPARING', 'PARSING', 'INDEXING', "
            "'COMPLETE', 'PARSING_FAILED', 'INDEXING_FAILED')",
            name="ck_video_parse_task_status",
        ),
        CheckConstraint("chunk_count > 0", name="ck_video_parse_task_chunk_count"),
        CheckConstraint("version > 0", name="ck_video_parse_task_version"),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_video_parse_task_attempt_count",
        ),
        CheckConstraint(
            "max_attempts > 0",
            name="ck_video_parse_task_max_attempts",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_video_parse_task_lease_version",
        ),
        CheckConstraint(
            "processing_duration_ms >= 0",
            name="ck_video_parse_task_processing_duration",
        ),
        CheckConstraint(
            "(status IN ('PREPARING', 'PARSING', 'INDEXING') "
            "AND lease_owner IS NOT NULL "
            "AND lease_expire_time IS NOT NULL "
            "AND heartbeat_time IS NOT NULL) OR "
            "(lease_owner IS NULL "
            "AND lease_expire_time IS NULL "
            "AND heartbeat_time IS NULL)",
            name="ck_video_parse_task_lease_state",
        ),
        Index(
            "ix_video_parse_task_due",
            "status",
            "next_execute_time",
        ),
        Index(
            "ix_video_parse_task_lease",
            "status",
            "lease_expire_time",
        ),
        Index(
            "ix_video_parse_task_video_status",
            "video_id",
            "status",
            "version",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    creator_id: Mapped[str | None] = mapped_column(String(64))
    video_id: Mapped[str] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    parse_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    next_execute_time: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expire_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    heartbeat_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    start_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    finish_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    processing_duration_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    processing_timing_complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ChunkParseTask(BaseModel):
    __tablename__ = "chunk_parse_task"
    __table_args__ = (
        UniqueConstraint(
            "video_task_id",
            "chunk_id",
            name="uq_chunk_parse_task_parent_chunk",
        ),
        # MySQL requires a matching referenced key for Interval's composite FK.
        UniqueConstraint(
            "id",
            "video_task_id",
            name="uq_chunk_parse_task_id_parent",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'UNDERSTANDING', 'COMPLETE', 'FAILED')",
            name="ck_chunk_parse_task_status",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_chunk_parse_task_retry_count",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_chunk_parse_task_lease_version",
        ),
        CheckConstraint(
            "(status = 'UNDERSTANDING' "
            "AND lease_owner IS NOT NULL "
            "AND lease_expire_time IS NOT NULL) OR "
            "(status <> 'UNDERSTANDING' "
            "AND lease_owner IS NULL "
            "AND lease_expire_time IS NULL)",
            name="ck_chunk_parse_task_lease_state",
        ),
        Index(
            "ix_chunk_parse_task_due",
            "status",
            "next_execute_time",
        ),
        Index(
            "ix_chunk_parse_task_lease",
            "status",
            "lease_expire_time",
        ),
        Index(
            "ix_chunk_parse_task_parent_status",
            "video_task_id",
            "status",
            "chunk_id",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    video_task_id: Mapped[str] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("video_chunk.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    next_execute_time: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expire_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    start_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    finish_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class IntervalParseTask(BaseModel):
    __tablename__ = "interval_parse_task"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_task_id", "video_task_id"],
            ["chunk_parse_task.id", "chunk_parse_task.video_task_id"],
            name="fk_interval_parse_task_anchor_parent",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "video_task_id",
            "start_sequence_no",
            name="uq_interval_parse_task_parent_start",
        ),
        CheckConstraint(
            "start_sequence_no >= 0",
            name="ck_interval_parse_task_start_sequence",
        ),
        CheckConstraint(
            "chunk_count BETWEEN 1 AND 63",
            name="ck_interval_parse_task_chunk_count",
        ),
        CheckConstraint(
            "ready_mask >= 0 AND (ready_mask >> chunk_count) = 0",
            name="ck_interval_parse_task_ready_mask",
        ),
        CheckConstraint(
            "status IN ('WAITING', 'REFINING', 'INDEXING', 'COMPLETE', "
            "'PARSING_FAILED', 'INDEXING_FAILED')",
            name="ck_interval_parse_task_status",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_interval_parse_task_retry_count",
        ),
        CheckConstraint(
            "lease_version >= 0",
            name="ck_interval_parse_task_lease_version",
        ),
        CheckConstraint(
            "(status IN ('REFINING', 'INDEXING') "
            "AND lease_owner IS NOT NULL "
            "AND lease_expire_time IS NOT NULL) OR "
            "(status IN ('WAITING', 'INDEXING', 'COMPLETE', "
            "'PARSING_FAILED', 'INDEXING_FAILED') "
            "AND lease_owner IS NULL "
            "AND lease_expire_time IS NULL)",
            name="ck_interval_parse_task_lease_state",
        ),
        Index(
            "ix_interval_parse_task_due",
            "status",
            "next_execute_time",
        ),
        Index(
            "ix_interval_parse_task_lease",
            "status",
            "lease_expire_time",
        ),
        Index(
            "ix_interval_parse_task_parent_status",
            "video_task_id",
            "status",
            "start_sequence_no",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    chunk_task_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        nullable=False,
    )
    video_task_id: Mapped[str] = mapped_column(
        ForeignKey("video_parse_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_task_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    start_sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ready_mask: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    next_execute_time: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP,
        nullable=False,
        default=now_utc,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expire_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    start_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    finish_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class IntervalParseContent(BaseModel):
    __tablename__ = "interval_parse_content"
    __table_args__ = MYSQL_TABLE_OPTIONS

    interval_task_id: Mapped[str] = mapped_column(
        ForeignKey("interval_parse_task.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


def interval_sequence_range(start_sequence_no: int, chunk_count: int) -> range:
    if (
        isinstance(start_sequence_no, bool)
        or not isinstance(start_sequence_no, int)
        or start_sequence_no < 0
    ):
        raise ValueError("start_sequence_no must be a nonnegative integer")
    if (
        isinstance(chunk_count, bool)
        or not isinstance(chunk_count, int)
        or not 1 <= chunk_count <= 63
    ):
        raise ValueError("chunk_count must be an integer between 1 and 63")
    return range(start_sequence_no, start_sequence_no + chunk_count)


def interval_ready_mask_bit(member_index: int, chunk_count: int) -> int:
    interval_sequence_range(0, chunk_count)
    if (
        isinstance(member_index, bool)
        or not isinstance(member_index, int)
        or not 0 <= member_index < chunk_count
    ):
        raise ValueError("member_index must identify an Interval member")
    return 1 << member_index


def interval_full_ready_mask(chunk_count: int) -> int:
    interval_sequence_range(0, chunk_count)
    return (1 << chunk_count) - 1


def validate_interval_parse_task_members(
    *,
    chunk_parse_task_id: str,
    video_parse_task_id: str,
    start_sequence_no: int,
    chunk_count: int,
    chunk_parse_task_ids: Sequence[str],
    members: Sequence[tuple[ChunkParseTask, int]],
) -> None:
    """Validate the immutable ordered Interval membership before insertion."""

    expected_sequences = interval_sequence_range(start_sequence_no, chunk_count)
    member_ids = tuple(chunk_parse_task_ids)
    if len(member_ids) != chunk_count:
        raise ValueError("chunk_parse_task_ids length must equal chunk_count")
    if len(set(member_ids)) != chunk_count:
        raise ValueError("chunk_parse_task_ids must be unique")
    if member_ids[0] != chunk_parse_task_id:
        raise ValueError("the first member must equal chunk_parse_task_id")
    if len(members) != chunk_count:
        raise ValueError("members length must equal chunk_count")

    for index, ((member, sequence_no), expected_sequence_no) in enumerate(
        zip(members, expected_sequences, strict=True)
    ):
        if member.id != member_ids[index]:
            raise ValueError("members must follow chunk_parse_task_ids order")
        if member.video_task_id != video_parse_task_id:
            raise ValueError("every member must belong to video_parse_task_id")
        if sequence_no != expected_sequence_no:
            raise ValueError(
                "member sequence numbers must be contiguous from start_sequence_no"
            )
