from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from gateway.config import (
    ArkModelSourceConfig, ChatAPISourceConfig, CodexModelSourceConfig, GatewayConfig, ModelBinding,
    ModelRole, OllamaEmbeddingSourceConfig, OpenAIAudioSourceConfig, OpenAIModelSourceConfig,
    resolve_source_api_key, source_api_key_env_name,
)
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.multimodal import adapter_capabilities
from gateway.personal_endpoint import PersonalModelTarget
from ..model.dto.personal_model import (
    ModelRoleView, PersonalModelConnectionView, PersonalModelOption, PersonalModelSettingsView,
)


# Protocols whose servers list their models. openai_audio is excluded: whisper.cpp and WhisperX-style
# transcription servers have no /v1/models, so the button could only ever fail there.
DISCOVERABLE_PROTOCOLS = frozenset({"openai_chat", "openai_responses", "anthropic_messages", "gemini",
                                    "ollama_embedding", "openai_vision", "llama_cpp_embedding"})

MODEL_ROLES: tuple[ModelRole, ...] = ("chat", "vision", "transcription", "ocr", "embedding", "rerank")


@dataclass(frozen=True, slots=True)
class YamlChatBinding:
    runtime_ref: str
    binding: ModelBinding
    connection: PersonalModelConnectionView
    option: PersonalModelOption
    config: GatewayConfig

    def target(self, *, environ: Mapping[str, str] | None = None) -> PersonalModelTarget:
        protocol = self.connection.protocol
        if protocol in {"ark", "openai_vision"}:
            protocol = "openai_chat"
        if protocol not in {"openai_chat", "openai_responses", "anthropic_messages", "gemini"}:
            raise ValueError("此模型不使用聊天接口")
        source = self.config.source(self.binding.source)
        return PersonalModelTarget(protocol, self.connection.base_url, self.binding.model,
                                  SecretStr(resolve_source_api_key(source, environ=environ) or ""),
                                  self.binding.supports_vision_input)


class YamlChatModels:
    """A redacted registry of declared bindings and their compatible deployment roles."""

    def __init__(self, config: GatewayConfig, primary: ModelBinding, *, environ: Mapping[str, str] | None = None,
                 declared: GatewayConfig | None = None):
        self.revision = 0
        self.config = config
        self._environ = os.environ if environ is None else environ
        self.connections: dict[str, PersonalModelConnectionView] = {}
        self.models: dict[str, YamlChatBinding] = {}
        self.source_names: dict[str, str] = {}
        self._refs: dict[str, str] = {}
        self.default_id: str | None = None
        definitions = declared or config
        self.definitions = definitions
        self._primary_ref = "supervisor" if "supervisor" in definitions.normalized_models else "agent"
        self._register("default", primary, chat=True)
        for ref, binding in definitions.configured_supervisor_choices(discover=True, environ=self._environ).items():
            self._register(ref, binding, chat=True)
        self._refs.setdefault(self._primary_ref, self._refs.get("default", ""))
        for ref in definitions.normalized_models:
            if ref not in self._refs:
                self._register(ref, definitions.resolve_model_binding(ref, environ=self._environ), chat=False)

        for name in definitions.model_sources:
            self._connection(name)

    def _connection(self, name: str) -> PersonalModelConnectionView:
        source = self.config.source(name)
        namespace = uuid5(NAMESPACE_URL, "seekino:yaml:source:" + name)
        cid = str(namespace)
        if name.startswith(("chat_", "service_")):
            try:
                cid = str(UUID(name.split("_", 1)[1]))
            except ValueError:
                pass
        if cid not in self.connections:
            if isinstance(source, ChatAPISourceConfig):
                protocol = source.protocol
            elif isinstance(source, OpenAIModelSourceConfig):
                protocol = "openai_chat"
            elif isinstance(source, CodexModelSourceConfig):
                protocol = "codex"
            else:
                protocol = source.adapter
            key_name = source_api_key_env_name(source)
            capabilities = adapter_capabilities(source.adapter)
            model_kinds = [kind for kind in ("generative", "transcription", "embedding", "rerank")
                           if getattr(capabilities, kind) and (kind in {"generative", "rerank"} or
                               not isinstance(source, ChatAPISourceConfig) or protocol == "openai_chat")]
            self.source_names[cid] = name
            self.connections[cid] = PersonalModelConnectionView(
                connection_id=cid, name=getattr(source, "display_name", None) or name,
                protocol=protocol, base_url=getattr(source, "base_url", ARK_OPENAI_BASE_URL if protocol == "ark" else ""),
                editable=isinstance(source, (ChatAPISourceConfig, OpenAIModelSourceConfig, ArkModelSourceConfig,
                                             OpenAIAudioSourceConfig, OllamaEmbeddingSourceConfig)),
                discoverable=protocol in DISCOVERABLE_PROTOCOLS,
                has_api_key=bool(getattr(source, "encrypted_api_key", None) or (key_name and self._environ.get(key_name, "").strip())),
                model_kinds=model_kinds, models=[], supports_vision=False, revision=0,
            )
        return self.connections[cid]

    def _register(self, ref: str, binding: ModelBinding, *, chat: bool):
        source = self.config.source(binding.source)
        connection = self._connection(binding.source)
        namespace = uuid5(NAMESPACE_URL, "seekino:yaml:source:" + binding.source)
        kind = binding.kind or "generative"
        token = binding.model if kind == "generative" else f"{kind}:{binding.model}"
        option_id = f"yaml:{uuid5(namespace, token)}"
        existing = self.models.get(option_id)
        if existing and ref in self.config.supervisor_choices and existing.binding != binding:
            option_id = f"yaml:{uuid5(namespace, f'preset:{ref}:{binding.model}')}"
        chat = chat and isinstance(source, (ChatAPISourceConfig, OpenAIModelSourceConfig, ArkModelSourceConfig, CodexModelSourceConfig))
        roles: list[ModelRole] = ["chat"] if chat else []
        capabilities = adapter_capabilities(source.adapter)
        if kind == "generative" and binding.supports_vision_input and capabilities.visual_input_mode:
            roles.extend(("vision", "ocr"))
        if kind in {"generative", "rerank"} and capabilities.rerank:
            roles.append("rerank")
        if kind in {"embedding", "transcription"}:
            roles.append(kind)
        if option_id not in self.models:
            option = PersonalModelOption(id=option_id, model_id=binding.model, is_yaml_default=chat and ref == "default",
                available=self.config.chat_credentials_ready(binding, environ=self._environ),
                label=self.config.supervisor_choices.get(ref), roles=roles, kind=kind,
                supports_vision=kind == "generative" and binding.supports_vision_input, dimensions=getattr(binding, "dimensions", None))
            connection.models.append(option)
            self.models[option_id] = YamlChatBinding(ref, binding, connection, option, self.config)
        option = self.models[option_id].option
        for role in roles:
            if role not in option.roles:
                option.roles.append(role)
        option.supports_vision |= kind == "generative" and binding.supports_vision_input
        http_protocol = connection.protocol not in {"codex", "claude_sdk", "whisperx", "pyannote", "campplus"}
        option.testable = http_protocol and bool(option.roles) and not (kind == "transcription" and connection.protocol == "ark")
        connection.supports_vision |= option.supports_vision
        self._refs[ref] = option_id
        if chat and ref == "default" and option.available:
            self.default_id = option.id
        if chat and (ref == "default" or (ref in self.config.supervisor_choices and not ref.startswith(("personal:", "yaml:")))):
            option.aliases.append(ref)

    def role_ref(self, selection: YamlChatBinding, role: ModelRole) -> str:
        if role not in selection.option.roles or not selection.option.available:
            raise ValueError("此模型不能用于所选用途")
        # One provider model can have a text preset and a separate visual preset.
        refs = [ref for ref, option_id in self._refs.items() if option_id == selection.option.id and ref != "default"]
        if role in {"vision", "ocr"}:
            for ref in refs:
                original = self.definitions.normalized_models.get(ref)
                if original and original.supports_vision_input:
                    return ref
        return self._primary_ref if selection.runtime_ref == "default" else selection.runtime_ref

    def settings(self) -> PersonalModelSettingsView:
        selected = self.find(self.config.chat_default_model) if self.config.chat_default_model else None
        role_settings = [ModelRoleView(role="chat", model_id=selected.option.id if selected else self.default_id,
                                       overridden=selected is not None, activation="immediate")]
        for role in MODEL_ROLES[1:]:
            ref = self.config.model_roles.get(role, role)
            role_settings.append(ModelRoleView(role=role, model_id=self._refs.get(ref),
                overridden=role in self.config.model_roles, activation="reindex" if role == "embedding" else "restart"))
        return PersonalModelSettingsView(connections=list(self.connections.values()), roles=role_settings,
                                        default_model_id=selected.option.id if selected else "default", revision=self.revision)

    def new_chat_id(self) -> str:
        selected = self.find(self.config.chat_default_model) if self.config.chat_default_model else None
        return selected.option.id if selected else self.default_id or "default"

    def find(self, model_id: str) -> YamlChatBinding | None:
        model = self.models.get(self._refs.get(model_id, model_id))
        return model if model and model.option.available and "chat" in model.option.roles else None
