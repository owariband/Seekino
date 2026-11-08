from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, field_validator

from gateway.personal_endpoint import PersonalProtocol
from ...schema import ContractModel

ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class PersonalModelConnectionSave(ContractModel):
    name: ModelName
    protocol: PersonalProtocol
    base_url: str = Field(min_length=1, max_length=2048)
    model_ids: list[ModelName] = Field(min_length=1, max_length=64)
    supports_vision: bool = False
    revision: int = Field(ge=0)
    api_key: SecretStr | None = Field(default=None, max_length=8192)

    @field_validator("model_ids")
    @classmethod
    def distinct_models(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not mid.isprintable() for mid in value):
            raise ValueError("模型 ID 不能重复或包含控制字符")
        return value


class PersonalModelOption(ContractModel):
    id: str
    model_id: str
    label: str | None = None
    available: bool = True
    aliases: list[str] = Field(default_factory=list)
    is_yaml_default: bool = False


class PersonalModelConnectionView(ContractModel):
    origin: Literal["personal", "yaml"] = "personal"
    connection_id: str
    name: str
    protocol: PersonalProtocol | Literal["codex"]
    base_url: str
    models: list[PersonalModelOption]
    supports_vision: bool
    has_api_key: bool
    revision: int


class PersonalModelSettingsView(ContractModel):
    connections: list[PersonalModelConnectionView]
    default_model_id: str = "default"
    revision: int = 0


class ModelDefaultSave(ContractModel):
    model_id: ModelName
    revision: int | None = Field(default=None, ge=0)
