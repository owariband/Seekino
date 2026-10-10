from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..constants.database import ID_LENGTH, MYSQL_TABLE_OPTIONS
from .base import BaseModel, UtcDateTime


class User(BaseModel):
    __tablename__ = "user"
    __table_args__ = (
        CheckConstraint("auth_version >= 1", name="ck_user_auth_version"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_user_status"),
        CheckConstraint("is_superuser IN (0, 1)", name="ck_user_superuser"),
        CheckConstraint("is_anonymous = 0 OR email IS NULL", name="ck_guest_email"),
        CheckConstraint("is_anonymous = 0 OR is_superuser = 0", name="ck_guest_role"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    nickname: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    avatar: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(32), default="Chinese")
    color_schema: Mapped[str | None] = mapped_column(String(32), default="Bright")
    timezone: Mapped[str | None] = mapped_column(String(64), default="Asia/Shanghai")
    last_login_time: Mapped[datetime | None] = mapped_column(UtcDateTime(), index=True)
    login_channel: Mapped[str | None] = mapped_column(String(32))
