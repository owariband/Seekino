"""Owner-scoped templates and their independently reproducible revisions."""

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..constants.database import MYSQL_TABLE_OPTIONS
from .base import BaseModel


class EditTemplate(BaseModel):
    __tablename__ = "edit_template"
    __table_args__ = (Index("ix_edit_template_owner", "tenant_id", "creator_id"), MYSQL_TABLE_OPTIONS)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    creator_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    default_version_id: Mapped[str | None] = mapped_column(String(36))
    default_requests: Mapped[dict] = mapped_column(JSON, default=dict)


class EditTemplateVersion(BaseModel):
    __tablename__ = "edit_template_version"
    __table_args__ = (UniqueConstraint("template_id", "version_no"), MYSQL_TABLE_OPTIONS)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("edit_template.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(String(36))
    author: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    evaluation: Mapped[list] = mapped_column(JSON)
    checks: Mapped[list] = mapped_column(JSON)
