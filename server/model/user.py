from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..constants.database import ID_LENGTH, MYSQL_TABLE_OPTIONS
from ..utils.time import now_unix_time_ms


class User(Base):
    __tablename__ = "user"
    __table_args__ = (
        CheckConstraint("auth_version >= 1", name="ck_user_auth_version"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_user_status"),
        CheckConstraint("role IN ('user', 'admin')", name="ck_user_role"),
        CheckConstraint("is_guest = 0 OR email IS NULL", name="ck_guest_email"),
        CheckConstraint("is_guest = 0 OR role = 'user'", name="ck_guest_role"),
        MYSQL_TABLE_OPTIONS,
    )

    user_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_unix_time_ms)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_unix_time_ms, onupdate=now_unix_time_ms)
