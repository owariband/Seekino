"""Storage is selected by the shipping entry point, before services are constructed."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


STORAGE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "storage.yaml"


class _StorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerDatabase(_StorageModel):
    type: Literal["mysql"]
    url_env: str = Field(pattern=r"^[A-Z_][A-Z_0-9]*$")


class ServerSearch(_StorageModel):
    type: Literal["elasticsearch"]


class ServerStorage(_StorageModel):
    database: ServerDatabase
    search: ServerSearch
    media: Literal["ffmpeg_cli"]


class DesktopDatabase(_StorageModel):
    type: Literal["sqlite"]
    file: str

    @field_validator("file")
    @classmethod
    def local_filename(cls, value: str) -> str:
        if not value.strip() or Path(value).name != value or value in {".", "..", ":memory:"}:
            raise ValueError("desktop.database.file must be a filename within the library")
        return value


class DesktopSearch(_StorageModel):
    type: Literal["sqlite"]
    text: Literal["fts5"]
    vector: Literal["sqlite-vec"]


class DesktopStorage(_StorageModel):
    database: DesktopDatabase
    search: DesktopSearch
    media: Literal["pyav"]


def _section(path: Path, name: str) -> dict:
    with path.open(encoding="utf-8") as source:
        document = yaml.safe_load(source)
    if not isinstance(document, dict) or name not in document:
        raise ValueError(f"missing {name} storage configuration in {path}")
    return document[name]


def server_storage(path: Path = STORAGE_CONFIG) -> ServerStorage:
    return ServerStorage.model_validate(_section(path, "server"))


def desktop_storage(path: Path = STORAGE_CONFIG) -> DesktopStorage:
    return DesktopStorage.model_validate(_section(path, "desktop"))
