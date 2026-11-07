from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from gateway.config import (
    ArkModelSourceConfig, ChatAPISourceConfig, CodexModelSourceConfig, GatewayConfig, ModelBinding, OpenAIModelSourceConfig,
    resolve_source_api_key, source_api_key_env_name,
)
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.personal_endpoint import PersonalModelTarget
from ..model.dto.personal_model import PersonalModelConnectionView, PersonalModelOption


@dataclass(frozen=True, slots=True)
class YamlChatBinding:
    runtime_ref: str
    binding: ModelBinding
    connection: PersonalModelConnectionView
    option: PersonalModelOption
    config: GatewayConfig


    def target(self, *, environ: Mapping[str, str] | None = None) -> PersonalModelTarget:
        connection = self.connection
        if connection.protocol == "codex":
            raise ValueError("此模型使用本机 Codex 登录")
        source = self.config.source(self.binding.source)
        return PersonalModelTarget(
            connection.protocol, connection.base_url, self.binding.model,
            SecretStr(resolve_source_api_key(source, environ=environ) or ""), bool(self.binding.supports_vision_input),
        )


class YamlChatModels:
    """Project the local Gateway's active bindings. No credentials are serialized or copied to preferences."""

    def __init__(self, config: GatewayConfig, primary: ModelBinding, *, environ: Mapping[str, str] | None = None):
        self.revision = 0
        self.config = config
        self._environ = os.environ if environ is None else environ
        self.connections: dict[str, PersonalModelConnectionView] = {}
        self.models: dict[str, YamlChatBinding] = {}
        self.source_names: dict[str, str] = {}
        self._refs: dict[str, str] = {}
        self.default_id: str | None = None
        alternatives = config.configured_supervisor_choices(discover=True, environ=self._environ)
        for ref, binding in [("default", primary), *alternatives.items()]:
            source = config.source(binding.source)
            if not isinstance(source, (OpenAIModelSourceConfig, ArkModelSourceConfig, CodexModelSourceConfig, ChatAPISourceConfig)):
                continue
            namespace = uuid5(NAMESPACE_URL, "seekino:yaml:source:" + binding.source)
            connection_id = str(namespace)
            if isinstance(source, ChatAPISourceConfig) and binding.source.startswith("chat_"):
                try:
                    connection_id = str(UUID(binding.source[5:]))
                except ValueError:
                    pass
            self.source_names[connection_id] = binding.source
            option_id = f"yaml:{uuid5(namespace, binding.model)}"
            existing = self.models.get(option_id)
            if existing and ref in config.supervisor_choices and existing.binding != binding:
                # A deliberately named preset can use different reasoning or fallback settings for the same model.
                option_id = f"yaml:{uuid5(namespace, f'preset:{ref}:{binding.model}')}"
            if connection_id not in self.connections:
                key_name = source_api_key_env_name(source)
                base_url = source.base_url if isinstance(source, (OpenAIModelSourceConfig, ChatAPISourceConfig)) else (
                    ARK_OPENAI_BASE_URL if isinstance(source, ArkModelSourceConfig) else ""
                )
                self.connections[connection_id] = PersonalModelConnectionView(
                    origin="yaml", connection_id=connection_id, name=getattr(source, "display_name", None) or binding.source,
                    protocol="codex" if isinstance(source, CodexModelSourceConfig) else getattr(source, "protocol", "openai_chat"),
                    base_url=base_url, models=[], supports_vision=False,
                    has_api_key=bool(getattr(source, "encrypted_api_key", None) or (key_name and self._environ.get(key_name, "").strip())), revision=0,
                )
            connection = self.connections[connection_id]
            if option_id not in self.models:
                option = PersonalModelOption(
                    id=option_id, model_id=binding.model, is_yaml_default=ref == "default",
                    available=config.chat_credentials_ready(binding, environ=self._environ),
                    label=config.supervisor_choices.get(ref),
                )
                connection.models.append(option)
                connection.supports_vision |= bool(binding.supports_vision_input)
                self.models[option_id] = YamlChatBinding(ref, binding, connection, option, config)
                if option.is_yaml_default and option.available:
                    self.default_id = option_id
            self._refs[ref] = option_id
            # Older chats persisted these unqualified choices. Namespaced personal IDs cannot alias YAML.
            if ref == "default" or (ref in config.supervisor_choices and not ref.startswith(("personal:", "yaml:"))):
                self.models[option_id].option.aliases.append(ref)

    def settings(self):
        from ..model.dto.personal_model import PersonalModelSettingsView
        selected = self.find(self.config.chat_default_model) if self.config.chat_default_model else None
        return PersonalModelSettingsView(connections=list(self.connections.values()),
                                         default_model_id=selected.option.id if selected else "default", revision=self.revision)

    def new_chat_id(self) -> str:
        selected = self.find(self.config.chat_default_model) if self.config.chat_default_model else None
        return selected.option.id if selected else self.default_id or "default"

    def find(self, model_id: str) -> YamlChatBinding | None:
        model = self.models.get(self._refs.get(model_id, model_id))
        return model if model and model.option.available else None
