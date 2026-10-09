from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    cast,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.expression import Grouping

from ..database import Base
from ..constants.identity import LOCAL_USER_ID
from ..utils.time import now_unix_time_ms
from .video import ID_LENGTH, LONG_TEXT, MYSQL_TABLE_OPTIONS


class AgentMessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class AgentMessageStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentConversation(Base):
    __tablename__ = "agent_conversation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "idempotency_key",
            name="uq_agent_conversation_tenant_idempotency",
        ),
        CheckConstraint(
            "next_message_sequence >= 0",
            name="ck_agent_conversation_next_message_sequence",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    conversation_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=LOCAL_USER_ID,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    title_is_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    next_message_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_message_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
        onupdate=now_unix_time_ms,
    )


class AgentMessage(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_agent_message_conversation_sequence",
        ),
        CheckConstraint(
            "sequence_no >= 0",
            name="ck_agent_message_sequence",
        ),
        CheckConstraint("version > 0", name="ck_agent_message_version"),
        CheckConstraint(
            "phase IS NULL OR phase IN ('QUEUED', 'RETRIEVING', 'EVALUATING', 'RESPONDING')",
            name="ck_agent_message_phase",
        ),
        CheckConstraint(
            "role IN ('USER', 'ASSISTANT')",
            name="ck_agent_message_role",
        ),
        CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_agent_message_status",
        ),
        CheckConstraint(
            "requested_top_k IS NULL OR requested_top_k BETWEEN 1 AND 100",
            name="ck_agent_message_requested_top_k",
        ),
        Index(
            "ix_agent_message_conversation_status",
            "conversation_id",
            "status",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    message_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reference: Mapped[dict[str, str] | None] = mapped_column(JSON)
    extra_context: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    content: Mapped[str | None] = mapped_column(LONG_TEXT)
    series_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(255))
    requested_top_k: Mapped[int | None] = mapped_column(Integer)
    requested_run_options: Mapped[dict[str, int] | None] = mapped_column(JSON)
    requested_series_ids: Mapped[list[str] | None] = mapped_column(JSON)
    search_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
    )
    updated_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=now_unix_time_ms,
        onupdate=now_unix_time_ms,
    )


AGENT_MESSAGE_IDEMPOTENCY_KEY = cast(
    AgentMessage.detail[("request", "idempotencyKey")].as_string(),
    String(128),
)
Index(
    "uq_agent_message_conversation_idempotency",
    AgentMessage.conversation_id,
    # MySQL requires parentheses around a functional key part.
    Grouping(AGENT_MESSAGE_IDEMPOTENCY_KEY),
    unique=True,
)
