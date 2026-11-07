from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid5

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from gateway.personal_endpoint import PersonalModelTarget, normalize_personal_endpoint
from ..model.dto.personal_model import (
    PersonalModelConnectionSave, PersonalModelConnectionView, PersonalModelOption, PersonalModelSettingsView,
)
from ..repository.personal_models import PersonalModelConflictError, PersonalModelRepository, Preferences


class PersonalModelNotFoundError(Exception):
    pass


def _options(record: dict) -> list[PersonalModelOption]:
    namespace = UUID(record["id"])
    return [PersonalModelOption(id=f"personal:{uuid5(namespace, model)}", model_id=model) for model in record["models"]]


def _view(records: list[dict], default: str) -> PersonalModelSettingsView:
    return PersonalModelSettingsView(connections=[PersonalModelConnectionView(
        connection_id=r["id"], name=r["name"], protocol=r["protocol"], base_url=r["url"],
        models=_options(r), has_api_key=bool(r["key"]), supports_vision=r["vision"], revision=r["revision"],
    ) for r in records], default_model_id=default)


def _retained_default(records: list[dict], current: str) -> str:
    if current != "default" and not any(opt.id == current for r in records for opt in _options(r)):
        return "default"
    return current


class PersonalModelConnections:
    def __init__(self, session_factory: sessionmaker[Session], *, key_file: Path, signing_secret: str, allow_local: bool):
        self._repo = PersonalModelRepository(session_factory)
        self._key_file = key_file
        self._master = hmac.digest(signing_secret.encode(), b"seekino/personal-model-connections/v1", hashlib.sha256) if signing_secret else None
        self.allow_local = allow_local

    def _cipher(self) -> AESGCM:
        if self._master:
            return AESGCM(self._master)
        path = self._key_file
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent) as tmp:
                tmp.write(os.urandom(32))
                tmp.flush()
                os.fsync(tmp.fileno())
                try:
                    os.link(tmp.name, path)
                except FileExistsError:
                    pass
            key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("模型凭证保护密钥不可用")
        return AESGCM(key)

    def list(self, owner: str) -> PersonalModelSettingsView:
        return _view(*self._repo.read(owner))

    def save(self, owner: str, connection_id: str, body: PersonalModelConnectionSave) -> PersonalModelSettingsView:
        cid = str(UUID(connection_id))
        url = normalize_personal_endpoint(body.base_url, allow_local=self.allow_local)

        def change(records: list[dict], default: str) -> Preferences:
            previous = next((r for r in records if r["id"] == cid), None)
            if not previous and body.revision:
                raise PersonalModelNotFoundError()
            if previous and previous["revision"] != body.revision:
                raise PersonalModelConflictError()
            if not previous and len(records) >= 64:
                raise ValueError("已达到个人模型服务数量上限")
            key = previous["key"] if previous else ""
            if "api_key" in body.model_fields_set:
                raw = body.api_key.get_secret_value() if body.api_key else ""
                if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
                    raise ValueError("API Key 不能包含控制字符")
                aad = json.dumps([owner, cid, body.protocol, url]).encode()
                nonce = os.urandom(12)
                key = base64.b64encode(nonce + self._cipher().encrypt(nonce, raw.encode(), aad)).decode() if raw else ""
            elif key and (url != previous["url"] or body.protocol != previous["protocol"]):
                raise ValueError("修改接口地址或协议后，请重新输入或清除 API Key")
            record = dict(id=cid, name=body.name, protocol=body.protocol, url=url, models=body.model_ids,
                          vision=body.supports_vision, key=key, revision=body.revision + 1)
            updated = [record if r["id"] == cid else r for r in records] if previous else [*records, record]
            return updated, _retained_default(updated, default)
        return _view(*self._repo.update(owner, change))

    def delete(self, owner: str, connection_id: str, revision: int) -> PersonalModelSettingsView:
        def change(records: list[dict], default: str) -> Preferences:
            record = next((r for r in records if r["id"] == connection_id), None)
            if not record:
                raise PersonalModelNotFoundError()
            if revision != record["revision"]:
                raise PersonalModelConflictError()
            updated = [r for r in records if r["id"] != connection_id]
            return updated, _retained_default(updated, default)
        return _view(*self._repo.update(owner, change))

    def set_default(self, owner: str, model_id: str) -> PersonalModelSettingsView:
        def change(records: list[dict], _: str) -> Preferences:
            if _retained_default(records, model_id) != model_id:
                raise PersonalModelNotFoundError()
            return records, model_id
        return _view(*self._repo.update(owner, change))

    def resolve(self, owner: str, model_id: str, *, connection_id: str | None = None) -> PersonalModelTarget:
        for r in self._repo.read(owner)[0]:
            if connection_id and r["id"] != connection_id:
                continue
            match = next((m for m in _options(r) if m.id == model_id), None)
            if match:
                secret = ""
                if r["key"]:
                    blob = base64.b64decode(r["key"])
                    aad = json.dumps([owner, r["id"], r["protocol"], r["url"]]).encode()
                    secret = self._cipher().decrypt(blob[:12], blob[12:], aad).decode()
                return PersonalModelTarget(r["protocol"], r["url"], match.model_id, SecretStr(secret), r["vision"])
        raise PersonalModelNotFoundError()
