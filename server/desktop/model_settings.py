"""Read, validate and edit a library's Gateway v2 profile without persisting secrets."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
import json
import os
import sqlite3
from pathlib import Path

from server.storage.sqlite_index import frozen_library_predicate
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import yaml

from gateway.config import GatewayConfig, config_from_document, source_api_key_env_name
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.embedding import describe_embedding_model
from gateway.personal_endpoint import normalize_personal_endpoint
from server.config.storage import STORAGE_CONFIG, desktop_storage

EXAMPLE = STORAGE_CONFIG.parents[1] / "deploy/cloud/models.example.yaml"
HEADER = "# Seekino Desktop model service "
LEGACY_KEYS = ("ARK_API_KEY", "DEEPSEEK_API_KEY")
DEFAULT_FORM = dict(name="", protocol="", auth="key", baseUrl="", vision="", transcription="",
                    embedding="", dimensions=1024, rerank="", encoderModel="")


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _identity(name, source):
    adapter = source.adapter
    protocol = getattr(source, "protocol", None) or ("openai_chat" if adapter == "openai" else adapter)
    url = ARK_OPENAI_BASE_URL if adapter == "ark" else getattr(source, "base_url", "").rstrip("/")
    declared = source_api_key_env_name(source)
    if not declared and not getattr(source, "encrypted_api_key", None):
        return protocol, url, ""
    if name == "ark_default" and adapter == "ark" and declared == LEGACY_KEYS[0]:
        return protocol, url, declared
    if (name == "deepseek_api" and adapter == "openai" and getattr(source, "provider", "") == "deepseek"
            and url == "https://api.deepseek.com" and declared == LEGACY_KEYS[1]):
        return protocol, url, declared
    identity = json.dumps([name, adapter, protocol, url, declared], ensure_ascii=False)
    return protocol, url, "SEEKINO_SERVICE_" + _digest(identity.encode())[:32].upper()


def _text(form, key, *, required=True):
    value = form.get(key, "")
    limit = 2048 if key == "baseUrl" else 255
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise ValueError(f"请检查模型设置字段 {key}，最多可填写 {limit} 个字符。")
    return value.strip()


@dataclass
class ModelProfile:
    path: Path
    raw: bytes | None
    config: GatewayConfig
    form: dict | None
    source: str | None

    @classmethod
    def read(cls, root):
        path = Path(root).resolve() / "models.yaml"
        raw = path.read_bytes() if path.is_file() else None
        return cls.parse(path, raw)

    @classmethod
    def parse(cls, path, raw):
        content = EXAMPLE.read_bytes() if raw is None else raw
        header, _, body = content.partition(b"\n")
        form = source = None
        if header.startswith(HEADER.encode()):
            try:
                meta = json.loads(header[len(HEADER.encode()):])
                if meta["digest"] == _digest(body):
                    form, source = meta["form"], meta["source"]
            except (TypeError, ValueError, KeyError):
                pass  # An externally edited profile is always treated as operator-managed.
        try:
            config = config_from_document(yaml.safe_load(content), path if raw is not None else EXAMPLE)
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise ValueError("模型配置文件无效，请检查 models.yaml 的模型、协议和维度。") from exc
        return cls(path, raw, config, form, source)

    @property
    def revision(self):
        return _digest(self.raw or b"")

    def check_revision(self, revision):
        if not isinstance(revision, str) or self.revision != revision:
            raise ValueError("模型设置已变更，请重新打开设置。")

    def candidate(self, form, *, keep_legacy=False):
        if self.raw is not None and self.form is None:
            raise ValueError("当前 models.yaml 由部署管理。可更新其凭据，模型绑定请在原文件中修改。")
        if self.path.is_symlink():
            raise ValueError("请直接编辑符号链接指向的模型配置。")
        protocol = form.get("protocol")
        if protocol not in {"ark", "openai_chat"}:
            raise ValueError("请选择 Ark 或 OpenAI 兼容协议。")
        url = normalize_personal_endpoint(_text(form, "baseUrl"), allow_local=True)
        if protocol == "ark" and url != ARK_OPENAI_BASE_URL:
            raise ValueError("方舟协议使用内置的官方服务地址。")
        host = urlsplit(url).hostname
        name = _text(form, "name", required=False) or ("Ark" if protocol == "ark" else host)
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        auth = form.get("auth", "key")
        if auth not in {"key", "none"} or (auth == "none" and (protocol != "openai_chat" or not loopback)):
            raise ValueError("免密连接仅适用于本机的 OpenAI 兼容服务。")
        vision, asr, embedding = (_text(form, key) for key in ("vision", "transcription", "embedding"))
        dims = form.get("dimensions")
        if type(dims) is not int or not 1 <= dims <= 2048:
            raise ValueError("向量维度须为 1 到 2048 之间的整数。")
        encoder = _text(form, "encoderModel", required=False)
        if protocol == "ark" and embedding.startswith("ep-") and (not encoder or encoder.startswith("ep-")):
            raise ValueError("向量接入点需要其底层模型 ID，才能识别既有索引。")
        if encoder and (protocol != "ark" or not embedding.startswith("ep-")):
            raise ValueError("仅方舟向量接入点可指定底层模型 ID。")
        rerank = _text(form, "rerank", required=False)
        clean = dict(name=name, protocol=protocol, baseUrl=url, vision=vision, transcription=asr, embedding=embedding,
                     dimensions=dims, rerank=rerank, encoderModel=encoder, auth=auth)
        source = "desktop_" + _digest(json.dumps([name, protocol, url, auth], ensure_ascii=False).encode())[:20]
        document = yaml.safe_load(EXAMPLE.read_bytes())
        # Freeze the old source names and destinations. Saved parse jobs refer to these by name.
        if self.raw is not None:
            document["model_sources"] = yaml.safe_load(self.raw)["model_sources"]
        elif not keep_legacy:
            document["model_sources"] = {}
        if protocol == "ark":
            connection = {"adapter": "ark", "api_key_env": "SEEKINO_DESKTOP_MODEL_KEY"}
        else:
            connection = dict(adapter="chat_api", protocol=protocol, base_url=url,
                              api_key_env="SEEKINO_DESKTOP_MODEL_KEY" if auth == "key" else None)
        document["model_sources"][source] = connection

        def gen(model, **options):
            return dict(source=source, model=model, stream=False, request_timeout_seconds=600, **options)

        document["models"] = dict(
            agent=gen(vision), supervisor=gen(vision, supports_vision_input=True),
            summarize=gen(vision, supports_vision_input=True), vision=gen(vision, supports_vision_input=True, fallback=None),
            transcription=gen(asr, kind="transcription"), rerank=gen(rerank or vision, kind="rerank"),
            embedding=dict(source=source, model=embedding, kind="embedding", dimensions=dims, video_fps=1.0,
                           input_mode="multimodal" if protocol == "ark" else "text", **({"encoder_model": encoder} if encoder else {})))
        # The desktop calls parse and Event retrieval directly and has no Supervisor UI.
        if protocol == "openai_chat":
            document.pop("supervisor_harness", None)
            document["models"].pop("supervisor")
        document["video_parser"]["fallback"]["enabled"] = False
        document["query_hook"] = dict(enabled=False, rewriter_enabled=False)
        body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
        meta = json.dumps(dict(source=source, form=clean, digest=_digest(body)), ensure_ascii=False)
        return self.parse(self.path, (HEADER + meta + "\n").encode() + body)

    def prepare(self, args):
        self.check_revision(args.get("revision"))
        if args.get("retainConfig") is True:
            return self
        form = args.get("form")
        if not isinstance(form, dict):
            raise ValueError("请填写模型服务。")
        # Check the same frozen work protected by SQLiteSearchIndex.bind_contract.
        path = self.path.parent / desktop_storage().database.file
        frozen = False
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                tables = (row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'"))
                frozen = bool(db.execute("SELECT " + frozen_library_predicate(tables)).fetchone()[0])
        candidate = self.candidate(form, keep_legacy=frozen)
        if frozen and self.encoder_contract() != candidate.encoder_contract():
            raise ValueError("资料库已有视频、上传任务或索引。向量模型、服务地址、维度或编码方式与原配置不一致。请保留原配置，或为新模型使用新的资料库。")
        return candidate

    def encoder_contract(self):
        binding = self.config.resolve_embedding_binding(environ={})
        info = describe_embedding_model(binding, self.config.source(binding.source))
        return (info.provider_name, info.encoder_model, info.encoder_revision, info.dimensions,
                binding.input_mode, binding.query_instruction)

    def slots(self):
        return {name: _identity(name, src)[2] for name, src in self.config.model_sources.items()}

    def values(self, keys):
        values = {}
        operator_config = self.raw is not None and self.form is None
        for name, slot in self.slots().items():
            if not slot:
                continue
            declared = source_api_key_env_name(self.config.source(name))
            ambient = None
            if slot in LEGACY_KEYS:
                ambient = os.environ.get(slot)
            elif operator_config and declared and declared not in LEGACY_KEYS:
                # The operator explicitly named this variable in their own configuration.
                ambient = os.environ.get(declared)
            values[slot] = (keys.get(slot) or ambient or "").strip()
        return values

    def for_gateway(self, keys):
        # Sources can share an operator's env name but use different endpoints. Bind each in memory.
        config = self.config.model_copy(deep=True)
        slots = self.slots()
        values = self.values(keys)
        for name, src in config.model_sources.items():
            if not slots[name]:
                continue
            # An explicit vault update supersedes a sealed operator key in memory.
            if values[slots[name]] and getattr(src, "encrypted_api_key", None):
                src.encrypted_api_key = None
            src.api_key_env = slots[name]
        return config, values

    def ready_sources(self, keys):
        values = self.values(keys)
        return {name: bool(values.get(slot) or getattr(self.config.model_sources[name], "encrypted_api_key", None) or not slot)
                for name, slot in self.slots().items()}

    def view(self, keys):
        available = self.ready_sources(keys)
        credentials = {slot: available[name] for name, slot in self.slots().items() if slot}
        roles = {}
        for role in ("agent", "supervisor", "vision", "transcription", "ocr", "summarize", "embedding", "rerank",
                     "diarization", "vision_fallback"):
            if role in self.config.normalized_models or role in self.config.model_roles:
                roles[role] = self.config.resolve_model_binding(role, environ={})
        parse_roles = ("vision", "transcription", "ocr", "diarization", "embedding")
        parse_ready = all(available.get(roles[role].source) for role in parse_roles if role in roles)
        index_ready = available[roles["embedding"].source]
        mode = ("managed" if self.form else "custom" if self.raw is not None
                else "legacy" if any(credentials.values()) else "new")
        rows = []
        for name, src in self.config.model_sources.items():
            protocol, base, slot = _identity(name, src)
            bound = [{"role": key, "model": binding.model} for key, binding in roles.items() if binding.source == name]
            if mode != "new" and (mode != "managed" or name == self.source or bound):
                rows.append(dict(name=name, label=self.form["name"] if name == self.source else name,
                                 protocol=protocol, baseUrl=base, credentialName=slot, roles=bound))
        return dict(mode=mode, form=self.form or DEFAULT_FORM.copy(), connections=rows, revision=self.revision,
                    credentialNames=list(credentials), credentials=credentials,
                    parseReady=parse_ready, importReady=parse_ready, indexReady=index_ready, searchReady=index_ready,
                    ready=parse_ready and index_ready)

    def retry_ready(self, task, available):
        snapshot = task.parse_config
        models = snapshot.get("models", {})
        required = ["embedding"]
        if task.status not in {"INDEXING", "INDEXING_FAILED"}:
            if snapshot.get("understanding_enabled", True):
                required.append("understanding")
            if (models.get("asr") or {}).get("mode") != "none":
                required.append("asr")
            if snapshot.get("ocr_enabled"):
                required.append("ocr")
            if models.get("diarization"):
                required.append("diarization")
            for optional in ("refine", "prefix_context"):
                if models.get(optional):
                    required.append(optional)
        for role in required:
            binding = models.get(role)
            if not isinstance(binding, dict):
                return False
            # V2 persists an explicit source with each model, even before model settings existed.
            if not available.get(binding.get("source")):
                return False
        return True

    def persist(self):
        if self.raw is None:
            raise ValueError("没有可保存的模型配置")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.path.parent, prefix=".models-", delete=False) as handle:
            temp = Path(handle.name)
        try:
            with temp.open("wb") as handle:
                handle.write(self.raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
