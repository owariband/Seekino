from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator

from gateway.config import ModelRole
from gateway.personal_endpoint import PersonalProtocol
from ...schema import ContractModel

ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
RegistryProtocol = PersonalProtocol | Literal["ark", "openai_audio", "ollama_embedding"]
ViewProtocol = RegistryProtocol | Literal["codex", "openai_vision", "llama_cpp_embedding", "whisperx", "pyannote", "campplus", "claude_sdk"]
RegistryKind = Literal["generative", "embedding", "transcription", "rerank"]


class RegisteredModelInput(ContractModel):
    model_id: ModelName
    kind: RegistryKind = "generative"
    supports_vision: bool = False
    dimensions: int | None = Field(default=None, ge=1, le=2048)

    @model_validator(mode="after")
    def validate_capabilities(self):
        if (self.kind == "embedding") != (self.dimensions is not None):
            raise ValueError("只有向量模型需要填写维度")
        if self.supports_vision and self.kind != "generative":
            raise ValueError("仅生成模型可设置图像输入")
        return self


class ModelConnectionEndpoint(ContractModel):
    protocol: RegistryProtocol
    base_url: str = Field(min_length=1, max_length=2048)
    revision: int = Field(ge=0)
    api_key: SecretStr | None = Field(default=None, max_length=8192)


class PersonalModelConnectionSave(ModelConnectionEndpoint):
    protocol: ViewProtocol
    base_url: str = Field(max_length=2048)
    name: ModelName
    model_ids: list[ModelName] = Field(min_length=1, max_length=64)
    supports_vision: bool = False
    models: list[RegisteredModelInput] | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("model_ids")
    @classmethod
    def distinct_models(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not mid.isprintable() for mid in value):
            raise ValueError("模型 ID 不能重复或包含控制字符")
        return value

    @model_validator(mode="after")
    def check_model_inputs(self):
        if self.models is not None:
            keys = {(m.kind, m.model_id) for m in self.models}
            if len(keys) != len(self.models) or list(dict.fromkeys(m.model_id for m in self.models)) != self.model_ids:
                raise ValueError("各类型的模型 ID 必须唯一并与模型列表一致")
        return self


class PersonalModelOption(ContractModel):
    id: str
    model_id: str
    label: str | None = None
    available: bool = True
    aliases: list[str] = Field(default_factory=list)
    is_yaml_default: bool = False
    testable: bool = False
    roles: list[ModelRole] = Field(default_factory=list)
    kind: str = "generative"
    supports_vision: bool = False
    dimensions: int | None = None


class PersonalModelConnectionView(ContractModel):
    origin: Literal["personal", "yaml"] = "yaml"
    connection_id: str
    name: str
    protocol: ViewProtocol
    base_url: str
    models: list[PersonalModelOption]
    supports_vision: bool
    has_api_key: bool
    revision: int
    editable: bool = True
    discoverable: bool = False
    model_kinds: list[RegistryKind] = Field(default_factory=list)


class ModelRoleView(ContractModel):
    role: ModelRole
    model_id: str | None
    overridden: bool
    activation: Literal["immediate", "restart", "reindex"]


class PersonalModelSettingsView(ContractModel):
    connections: list[PersonalModelConnectionView]
    default_model_id: str = "default"
    revision: int = 0
    roles: list[ModelRoleView] = Field(default_factory=list)


class ModelDefaultSave(ContractModel):
    model_id: ModelName
    revision: int | None = Field(default=None, ge=0)


class ModelRoleSave(ContractModel):
    role: ModelRole
    model_id: ModelName | None
    revision: int = Field(ge=0)


class DiscoveredModels(ContractModel):
    model_ids: list[str]
