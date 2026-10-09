from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..constants.database import ID_LENGTH, MYSQL_TABLE_OPTIONS
from ..utils.time import now_unix_time_ms


class Tenant(Base):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("type IN ('local', 'public', 'personal')", name="ck_tenant_type"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_tenant_status"),
        CheckConstraint("type = 'public' OR owner_user_id IS NOT NULL", name="ck_owned_tenant_owner"),
        CheckConstraint("type <> 'public' OR owner_user_id IS NULL", name="ck_public_tenant_owner"),
        MYSQL_TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), ForeignKey("user.user_id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_unix_time_ms)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_unix_time_ms, onupdate=now_unix_time_ms)
