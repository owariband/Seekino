from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants.database import MYSQL_TABLE_OPTIONS
from .base import BaseModel


class PersonalModelPreferences(BaseModel):
    __tablename__ = "personal_model_preferences"
    __table_args__ = MYSQL_TABLE_OPTIONS

    owner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    connections: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    default_model: Mapped[str] = mapped_column(String(128), nullable=False)
