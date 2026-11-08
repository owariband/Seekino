"""The model settings editor changes the same document that the Gateway loads."""
from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid5

import yaml

from gateway.config import GatewayConfig, ModelRole, config_from_document, resolve_config_path, resolve_source_api_key
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.model_credentials import seal
from gateway.personal_endpoint import normalize_personal_endpoint
from ..config.yaml_document import document_lock, edit_mapping, replace_document, revision
from ..model.dto.personal_model import ModelConnectionEndpoint, PersonalModelConnectionSave, PersonalModelSettingsView, RegisteredModelInput
from .yaml_chat_models import YamlChatModels


class PersonalModelNotFoundError(LookupError):
    pass


# These roles have callers outside the model picker. Deleting their source would
# silently break e.g. parse or embeddings even when the chat catalog looks valid.
_SERVICE_ROLES = frozenset({
    "agent", "supervisor", "embedding", "transcription", "vision", "vision_fallback", "summarize",
    "ocr", "rerank", "query_hook", "query_rewriter", "diarization", "speaker", "speaker_embedding",
})


class ModelSettings:
    def __init__(self, path: str | Path | None, *, allow_local: bool, environ: Mapping[str, str] | None = None,
                 validate_deployment: Callable[[GatewayConfig], None] | None = None):
        self.path = resolve_config_path(path)
        self.allow_local = allow_local
        self._validate_deployment = validate_deployment
        self.environ = os.environ if environ is None else environ
        self._cached: tuple[bytes, YamlChatModels] | None = None

    @staticmethod
    def _document(raw: bytes) -> dict:
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise ValueError("模型配置文件不是有效的 YAML") from None
        if not isinstance(data, dict):
            raise ValueError("模型配置文件必须是 YAML 映射")
        return data

    def _catalog(self, data: dict, raw: bytes) -> YamlChatModels:
        try:
            config = config_from_document(data, self.path)
            independent = "supervisor" in config.normalized_models
            primary = config.resolve_model_binding("supervisor" if independent else "agent", environ=self.environ)
            if not independent:
                primary = primary.model_copy(update={"reasoning_effort": None})
            # The underlying model declarations remain selectable when a role is reassigned.
            declared = config_from_document({**data, "model_roles": {}}, self.path) if config.model_roles else config
            catalog = YamlChatModels(config, primary, environ=self.environ, declared=declared)
        except (ValueError, KeyError, TypeError):
            # Pydantic diagnostics include raw inputs, sometimes an entire model source.
            raise ValueError("模型配置与所选协议或现有用途不兼容，请检查配置") from None
        version = int(revision(raw)[:12], 16) or 1  # exact in a JS Number
        catalog.revision = version
        for connection in catalog.connections.values():
            connection.revision = version
        return catalog

    def catalog(self) -> YamlChatModels:
        raw = self.path.read_bytes()
        cached = self._cached
        if cached is not None and cached[0] == raw:
            return cached[1]
        result = self._catalog(self._document(raw), raw)
        self._cached = (raw, result)
        return result

    def _change(self, edit: Callable[[dict, YamlChatModels], None]) -> PersonalModelSettingsView:
        with document_lock(self.path):
            raw = self.path.read_bytes()
            original = self._document(raw)
            catalog = self._catalog(original, raw)
            proposed = copy.deepcopy(original)
            edit(proposed, catalog)
            if proposed == original:
                return catalog.settings()
            content = edit_mapping(raw, original, proposed)
            updated = self._catalog(proposed, content)
            if self._validate_deployment and updated.config.model_roles:
                self._validate_deployment(updated.config)
            replace_document(self.path, content, revision(raw))
            self._cached = (content, updated)
            return updated.settings()

    def set_default(self, model_id: str, expected: int | None = None) -> PersonalModelSettingsView:
        def edit(data, catalog):
            if expected is not None and expected != catalog.revision:
                raise self._conflict()
            if model_id == "default":
                data.pop("chat_default_model", None)
                return
            selected = catalog.find(model_id)
            if selected is None:
                raise PersonalModelNotFoundError()
            data["chat_default_model"] = selected.runtime_ref
        return self._change(edit)

    def assign(self, role: ModelRole, model_id: str | None, revision: int) -> PersonalModelSettingsView:
        if role == "chat":
            return self.set_default(model_id or "default", revision)
        def edit(data, catalog):
            if revision != catalog.revision:
                raise self._conflict()
            if model_id is None:
                assignments = data.get("model_roles", {})
                assignments.pop(role, None)
                if not assignments:
                    data.pop("model_roles", None)
                return
            selection = catalog.models.get(model_id)
            if not selection:
                raise PersonalModelNotFoundError()
            self._sections(data, catalog)
            ref = catalog.role_ref(selection, role)
            if ref not in data["models"]:
                # V1 files can include a role synthesized by Gateway defaults.
                data["models"][ref] = catalog.definitions.normalized_models[ref].model_dump(exclude_none=True)
            declaration = data["models"][ref]
            if declaration["model"] != selection.option.model_id:
                # An environment override on the original role is not applied to another role.
                # Register the displayed effective model so the selection and the request agree.
                ref = "model_" + uuid5(UUID(selection.connection.connection_id), f"effective:{ref}:{selection.option.model_id}").hex
                desired = {**declaration, "model": selection.option.model_id}
                if ref in data["models"] and data["models"][ref] != desired:
                    raise ValueError("这个模型引用已由其他配置使用")
                data["models"][ref] = desired
            data.setdefault("model_roles", {})[role] = ref
        return self._change(edit)

    def discover(self, cid: str, body: ModelConnectionEndpoint) -> list[str]:
        from gateway.model_discovery import discover_models
        catalog = self.catalog()
        current = catalog.connections.get(cid)
        if body.revision != (current.revision if current else 0):
            raise self._conflict()
        url = normalize_personal_endpoint(body.base_url, allow_local=self.allow_local)
        if body.protocol in {"openai_vision", "llama_cpp_embedding"} and (
                not current or current.protocol != body.protocol or current.base_url != url):
            raise ValueError("请选择已配置的原生模型服务")
        if "api_key" in body.model_fields_set:
            key = body.api_key.get_secret_value() if body.api_key else ""
        elif current and current.has_api_key:
            if url != current.base_url or body.protocol != current.protocol:
                raise ValueError("修改接口地址或协议后，请重新输入或清除 API Key")
            key = resolve_source_api_key(catalog.config.source(catalog.source_names[cid]), environ=self.environ)
        else:
            key = None
        return discover_models(body.protocol, url, key, allow_local=self.allow_local)

    @staticmethod
    def _conflict():
        from ..config.yaml_document import GlobalAgentSettingsConflictError
        return GlobalAgentSettingsConflictError("配置已更改，请刷新后再保存")

    def save(self, connection_id: str, body: PersonalModelConnectionSave) -> PersonalModelSettingsView:
        cid = str(UUID(connection_id))

        def edit(data, catalog):
            previous = catalog.connections.get(cid)
            if body.revision != (previous.revision if previous else 0):
                raise self._conflict()
            if previous is None and len(catalog.connections) >= 64:
                raise ValueError("已达到模型服务数量上限")
            name = catalog.source_names.get(cid, "service_" + UUID(cid).hex)
            sources, models = self._sections(data, catalog)
            if previous and not previous.editable:
                if (body.protocol != previous.protocol or body.base_url != previous.base_url or previous.name != body.name
                        or "api_key" in body.model_fields_set or previous.protocol in {"campplus", "pyannote"}):
                    raise ValueError("此原生连接的运行参数由部署配置管理")
                self._update_models(models, data, catalog, cid, name, body)
                return
            if body.protocol not in {"openai_chat", "openai_responses", "anthropic_messages", "gemini", "openai_audio", "ollama_embedding", "ark"}:
                raise ValueError("请先在部署中配置原生模型服务")
            url = normalize_personal_endpoint(body.base_url, allow_local=self.allow_local)
            if previous is None and name in sources:
                raise self._conflict()
            old_source = sources.get(name, {})
            protocol = body.protocol
            if previous and previous.protocol == "ark" and protocol == "openai_chat" and url == ARK_OPENAI_BASE_URL:
                protocol = "ark"  # Older clients presented Ark as Chat Completions.
            changed_target = previous and (url != previous.base_url or protocol != previous.protocol)
            if changed_target and (old_source.get("api_key_env") or previous.has_api_key) and "api_key" not in body.model_fields_set:
                raise ValueError("修改接口地址或协议后，请重新输入或清除 API Key")
            same_protocol = previous and protocol == previous.protocol
            if same_protocol and old_source.get("adapter") != "chat_api":
                source = old_source.copy()
                if protocol != "ark":
                    source["base_url"] = url
            else:
                if previous and not same_protocol and self._shared_source(data, name):
                    raise ValueError("此服务仍供其他任务使用。请添加新协议的服务并选择其模型。")
                if protocol == "ark":
                    source = {"adapter": "ark", "api_key_env": None}
                elif protocol in {"openai_audio", "ollama_embedding"}:
                    source = {"adapter": protocol, "base_url": url}
                    if protocol == "openai_audio":
                        source.update(provider="openai_audio", api_key_env=None)
                else:
                    source = dict(adapter="chat_api", protocol=protocol, base_url=url)
                if same_protocol:
                    for field in ("api_key_env", "encrypted_api_key"):
                        if field in old_source:
                            source[field] = old_source[field]
            if protocol == "ark" and url != ARK_OPENAI_BASE_URL:
                raise ValueError("Ark 接口使用配置的火山方舟地址")
            if body.name != name:
                source["display_name"] = body.name
            else:
                source.pop("display_name", None)
            if "api_key" in body.model_fields_set:
                secret = body.api_key.get_secret_value() if body.api_key else ""
                if secret and (not secret.isprintable() or secret == "••••••••"):
                    raise ValueError("请重新输入有效的 API Key")
                source.pop("encrypted_api_key", None)
                source["api_key_env"] = None
                if secret:
                    destination = (name, source["adapter"], source.get("protocol", ""), source.get("base_url", ""))
                    source["encrypted_api_key"] = seal(self.path, destination, secret)
            sources[name] = source
            self._update_models(models, data, catalog, cid, name, body)
        return self._change(edit)

    @staticmethod
    def _sections(data, catalog):
        if "models" in data:
            return data["model_sources"], data["models"]
        # Materialize V1's existing canonical projection when its owner first edits model settings.
        # Remove only the old fields represented by the V2 bindings.
        config = catalog.config
        data["config_version"] = 2
        data.pop("agent_model", None)
        data.pop("agent_chat", None)
        data.pop("normalized_models", None)
        data.pop("multimodal_embedding", None)
        data["model_sources"] = {k: v.model_dump(exclude_none=True) for k, v in config.model_sources.items()}
        for name, source in config.model_sources.items():
            for field in ("display_name", "encrypted_api_key"):
                value = getattr(source, field, None)
                if value:
                    data["model_sources"][name][field] = value
        data["models"] = {k: v.model_dump(exclude_none=True) for k, v in config.normalized_models.items()}
        parser = data.get("video_parser", {})
        for field in ("transcript_model", "transcription_source", "transcription_timeout_seconds", "transcription_max_output_tokens",
                      "video_model", "vision_source", "vision_timeout_seconds", "vision_max_output_tokens", "vision_reasoning_effort"):
            parser.pop(field, None)
        fallback = parser.get("fallback", {})
        fallback.pop("deepseek", None)
        return data["model_sources"], data["models"]

    @staticmethod
    def _shared_source(data, name):
        def uses(binding):
            return isinstance(binding, dict) and (binding.get("source") == name or uses(binding.get("fallback")))
        active = _SERVICE_ROLES.union(data.get("model_roles", {}).values())
        return any(uses(binding) and (ref in active or binding.get("source") != name)
                   for ref, binding in data["models"].items())

    @staticmethod
    def _update_models(models, data, catalog, cid, name, body):
        previous = catalog.connections.get(cid)
        old = {}
        visual = set()
        for option in previous.models if previous else ():
            key = (option.model_id, option.kind)
            old.setdefault(key, option)
            if option.supports_vision:
                visual.add(key)
        requested = {(m.model_id, m.kind): m for m in body.models or ()}
        if body.models is None:
            # Older clients do not send capabilities. Keep existing per-model metadata.
            for mid in body.model_ids:
                existing = [m for (model, _), m in old.items() if model == mid]
                for m in existing:
                    requested[mid, m.kind] = RegisteredModelInput(model_id=mid, kind=m.kind,
                        supports_vision=(body.supports_vision if previous and body.supports_vision != previous.supports_vision
                                         and m.kind == "generative" else (mid, m.kind) in visual), dimensions=m.dimensions)
                if not existing:
                    requested[mid, "generative"] = RegisteredModelInput(model_id=mid, supports_vision=body.supports_vision)
            for key, m in old.items():
                if m.kind != "generative" and key not in requested:
                    requested[key] = RegisteredModelInput(model_id=m.model_id, kind=m.kind, dimensions=m.dimensions)
        removed = old.keys() - requested.keys()
        added = requested.keys() - old.keys()
        rename = {}
        if len(removed) == len(added) == 1:
            before, after = next(iter(removed)), next(iter(added))
            if before[1] == after[1]:
                rename[before] = after
        choices = data.get("supervisor_choices", {})
        installed = set()
        for ref, binding in list(models.items()):
            if binding["source"] != name:
                continue
            implicit = ref if ref in {"transcription", "embedding", "rerank"} else "generative"
            key = (binding["model"], binding.get("kind") or implicit)
            if key not in old:
                continue
            protected = ref in _SERVICE_ROLES or ref in data.get("model_roles", {}).values()
            if key in removed:
                if protected and not (ref == "supervisor" and key in rename):
                    raise ValueError("此模型仍被其他任务引用。请先修改对应的模型用途。")
                if key not in rename:
                    del models[ref]
                    choices.pop(ref, None)
                    if data.get("chat_default_model") == ref:
                        data.pop("chat_default_model")
                    continue
                binding["model"] = rename[key][0]
            new_key = rename.get(key, key)
            model = requested[new_key]
            if model.kind == "generative" and (key in visual) != model.supports_vision:
                binding["supports_vision_input"] = model.supports_vision
            if model.dimensions is not None and old[key].dimensions != model.dimensions:
                binding["dimensions"] = model.dimensions
            installed.add(new_key)
        for key, model in requested.items():
            if key in old or key in installed:
                continue
            token = model.model_id if model.kind == "generative" else f"{model.kind}:{model.model_id}"
            ref = "model_" + uuid5(UUID(cid), token).hex
            entry = {"source": name, "model": model.model_id, "kind": model.kind}
            if model.kind == "generative":
                entry["supports_vision_input"] = model.supports_vision
            if model.kind == "embedding":
                entry.update(dimensions=model.dimensions, video_fps=1,
                             input_mode="multimodal" if data["model_sources"][name]["adapter"] == "ark" else "text")
            models[ref] = entry

    def delete(self, connection_id: str, expected: int) -> PersonalModelSettingsView:
        def edit(data, catalog):
            previous = catalog.connections.get(connection_id)
            if previous is None:
                raise PersonalModelNotFoundError()
            if previous.revision != expected:
                raise self._conflict()
            name = catalog.source_names[connection_id]
            sources, models = self._sections(data, catalog)
            if not previous.editable or self._shared_source(data, name):
                raise ValueError("此服务仍被其他任务引用，无法删除")
            for ref in [ref for ref, model in models.items() if model["source"] == name]:
                del models[ref]
                data.get("supervisor_choices", {}).pop(ref, None)
                if data.get("chat_default_model") == ref:
                    data.pop("chat_default_model")
            del sources[name]
        return self._change(edit)
