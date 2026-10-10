from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseModel
from ..constants.database import ID_LENGTH, MYSQL_TABLE_OPTIONS


class Tenant(BaseModel):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("type IN ('local', 'public', 'personal')", name="ck_tenant_type"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_tenant_status"),
        CheckConstraint("type = 'public' OR owner_id IS NOT NULL", name="ck_owned_tenant_owner"),
        CheckConstraint("type <> 'public' OR owner_id IS NULL", name="ck_public_tenant_owner"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), ForeignKey("user.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)
    avatar: Mapped[str | None] = mapped_column(Text)
