from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr

from gateway.config import (
    ArkModelSourceConfig, CodexModelSourceConfig, GatewayConfig, ModelBinding, OpenAIModelSourceConfig,
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


class YamlChatModels:
    """Project the local Gateway's active bindings. No credentials are serialized or copied to preferences."""

    def __init__(self, config: GatewayConfig, primary: ModelBinding, *, environ: Mapping[str, str] | None = None):
        self._config = config
        self._environ = os.environ if environ is None else environ
        self.connections: dict[str, PersonalModelConnectionView] = {}
        self.models: dict[str, YamlChatBinding] = {}
        self._aliases: dict[str, str] = {}
        self.default_id: str | None = None
        alternatives = config.configured_supervisor_choices(discover=True, environ=self._environ)
        for ref, binding in [("default", primary), *alternatives.items()]:
            source = config.source(binding.source)
            if not isinstance(source, (OpenAIModelSourceConfig, ArkModelSourceConfig, CodexModelSourceConfig)):
                continue
            namespace = uuid5(NAMESPACE_URL, "seekino:yaml:source:" + binding.source)
            connection_id = str(namespace)
            option_id = f"yaml:{uuid5(namespace, binding.model)}"
            existing = self.models.get(option_id)
            if existing and ref in config.supervisor_choices and existing.binding != binding:
                # A deliberately named preset can use different reasoning or fallback settings for the same model.
                option_id = f"yaml:{uuid5(namespace, f'preset:{ref}:{binding.model}')}"
            if connection_id not in self.connections:
                key_name = source_api_key_env_name(source)
                base_url = source.base_url if isinstance(source, OpenAIModelSourceConfig) else (
                    ARK_OPENAI_BASE_URL if isinstance(source, ArkModelSourceConfig) else ""
                )
                self.connections[connection_id] = PersonalModelConnectionView(
                    origin="yaml", connection_id=connection_id, name=binding.source,
                    protocol="codex" if isinstance(source, CodexModelSourceConfig) else "openai_chat",
                    base_url=base_url, models=[], supports_vision=False,
                    has_api_key=bool(key_name and self._environ.get(key_name, "").strip()), revision=0,
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
                self.models[option_id] = YamlChatBinding(ref, binding, connection, option)
                if option.is_yaml_default and option.available:
                    self.default_id = option_id
            # Older chats persisted these unqualified choices. Namespaced personal IDs cannot alias YAML.
            if ref == "default" or (ref in config.supervisor_choices and not ref.startswith(("personal:", "yaml:"))):
                self.models[option_id].option.aliases.append(ref)
                self._aliases[ref] = option_id

    def find(self, model_id: str) -> YamlChatBinding | None:
        model = self.models.get(self._aliases.get(model_id, model_id))
        return model if model and model.option.available else None

    def probe_target(self, model: YamlChatBinding) -> PersonalModelTarget:
        connection = model.connection
        if connection.protocol == "codex":
            raise ValueError("此模型使用本机 Codex 登录")
        source = self._config.source(model.binding.source)
        return PersonalModelTarget(
            connection.protocol, connection.base_url, model.binding.model,
            SecretStr(resolve_source_api_key(source, environ=self._environ) or ""), bool(model.binding.supports_vision_input),
        )
