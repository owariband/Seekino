"""The model settings editor changes the same document that the Gateway loads."""
from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid5

import yaml

from gateway.config import config_from_document, resolve_config_path
from gateway.model_credentials import seal
from gateway.personal_endpoint import normalize_personal_endpoint
from ..config.yaml_document import document_lock, edit_mapping, replace_document, revision
from ..model.dto.personal_model import PersonalModelConnectionSave, PersonalModelSettingsView
from .yaml_chat_models import YamlChatModels


class PersonalModelNotFoundError(LookupError):
    pass


# These roles have callers outside the model picker. Deleting their source would
# silently break e.g. parse or embeddings even when the chat catalog looks valid.
_SERVICE_ROLES = frozenset({
    "agent", "supervisor", "embedding", "transcription", "vision", "vision_fallback", "summarize",
    "ocr", "rerank", "query_hook", "query_rewriter", "diarization", "speaker_embedding",
})


class ModelSettings:
    def __init__(self, path: str | Path | None, *, allow_local: bool, environ: Mapping[str, str] | None = None):
        self.path = resolve_config_path(path)
        self.allow_local = allow_local
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
            catalog = YamlChatModels(config, primary, environ=self.environ)
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

    @staticmethod
    def _conflict():
        from ..config.yaml_document import GlobalAgentSettingsConflictError
        return GlobalAgentSettingsConflictError("配置已更改，请刷新后再保存")

    def save(self, connection_id: str, body: PersonalModelConnectionSave) -> PersonalModelSettingsView:
        cid = str(UUID(connection_id))
        url = normalize_personal_endpoint(body.base_url, allow_local=self.allow_local)

        def edit(data, catalog):
            previous = catalog.connections.get(cid)
            if body.revision != (previous.revision if previous else 0):
                raise self._conflict()
            if previous is None and len(catalog.connections) >= 64:
                raise ValueError("已达到模型服务数量上限")
            if previous and previous.protocol == "codex":
                raise ValueError("本机 Codex 登录由 CLI 管理")
            name = catalog.source_names.get(cid, "chat_" + UUID(cid).hex)
            sources, models = self._sections(data, catalog)
            if previous is None and name in sources:
                raise self._conflict()
            old_source = sources.get(name, {})
            changed_target = previous and (url != previous.base_url or body.protocol != previous.protocol)
            if changed_target and (old_source.get("api_key_env") or previous.has_api_key) and "api_key" not in body.model_fields_set:
                raise ValueError("修改接口地址或协议后，请重新输入或清除 API Key")
            adapter = old_source.get("adapter")
            if adapter in {"openai", "ark"} and body.protocol == "openai_chat" and (
                adapter == "openai" and url.startswith("https://") or previous and url == previous.base_url
            ):
                source = old_source.copy()
                if adapter == "openai":
                    source["base_url"] = url
            else:
                if adapter in {"openai", "ark"} and self._shared_source(data, name):
                    raise ValueError("此服务仍供其他任务使用。请添加新协议的服务并选择其模型。")
                source = dict(adapter="chat_api", protocol=body.protocol, base_url=url)
                for field in ("api_key_env", "encrypted_api_key"):
                    if field in old_source:
                        source[field] = old_source[field]
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
        return any(uses(binding) and (ref in _SERVICE_ROLES or binding.get("source") != name
                                      or binding.get("kind") not in {None, "generative"})
                   for ref, binding in data["models"].items())

    @staticmethod
    def _update_models(models, data, catalog, cid, name, body):
        previous = catalog.connections.get(cid)
        old_ids = {option.model_id for option in previous.models} if previous else set()
        removed = old_ids - set(body.model_ids)
        added = [mid for mid in body.model_ids if mid not in old_ids]
        choices = data.get("supervisor_choices", {})
        # A one-for-one edit retains the named binding and its tuned profile/label.
        rename = {next(iter(removed)): added[0]} if len(removed) == len(added) == 1 else {}
        renamed = set()
        for ref, binding in list(models.items()):
            if binding["source"] != name or binding.get("kind") not in {None, "generative"}:
                continue
            mid = binding["model"]
            if mid in removed:
                if ref in _SERVICE_ROLES and not (ref == "supervisor" and mid in rename):
                    raise ValueError("此模型仍被其他任务引用。请保留该模型并添加新的模型 ID。")
                if mid in rename:
                    binding["model"] = rename[mid]
                    renamed.add(rename[mid])
                else:
                    del models[ref]
                    choices.pop(ref, None)
                    if data.get("chat_default_model") == ref:
                        data.pop("chat_default_model")
                    continue
            if previous and body.supports_vision != previous.supports_vision and mid in old_ids:
                binding["supports_vision_input"] = body.supports_vision
        for mid in added:
            if mid in renamed:
                continue
            ref = "chat_" + uuid5(UUID(cid), mid).hex
            models[ref] = {"source": name, "model": mid, "supports_vision_input": body.supports_vision}

    def delete(self, connection_id: str, expected: int) -> PersonalModelSettingsView:
        def edit(data, catalog):
            previous = catalog.connections.get(connection_id)
            if previous is None:
                raise PersonalModelNotFoundError()
            if previous.revision != expected:
                raise self._conflict()
            name = catalog.source_names[connection_id]
            sources, models = self._sections(data, catalog)
            if previous.protocol == "codex" or self._shared_source(data, name):
                raise ValueError("此服务仍被其他任务引用，无法删除")
            for ref in [ref for ref, model in models.items() if model["source"] == name]:
                del models[ref]
                data.get("supervisor_choices", {}).pop(ref, None)
                if data.get("chat_default_model") == ref:
                    data.pop("chat_default_model")
            del sources[name]
        return self._change(edit)
