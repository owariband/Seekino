"""Named desktop connections. Effective playback and parse settings are published separately."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from server.desktop.model_settings import DEFAULT_FORM, _text
from server.desktop.watch_settings import validate_destination

ROLE_FIELDS = ("vision", "transcription", "embedding", "dimensions", "encoderModel", "rerank", "watchModel")


def _hash(parts):
    return sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def clean(form):
    protocol, base, auth = validate_destination(form)
    fields = {key: _text(form, key, required=False) for key in ROLE_FIELDS if key != "dimensions"}
    dims = form.get("dimensions", DEFAULT_FORM["dimensions"])
    if type(dims) not in (int, float) or not 1 <= dims <= 2048 or int(dims) != dims:
        raise ValueError("向量维度须为 1 到 2048 之间的整数。")
    return dict(protocol=protocol, baseUrl=base, auth=auth, dimensions=int(dims),
                name=_text(form, "name", required=False) or urlsplit(base).hostname, **fields)


def slot(row):
    if row["auth"] == "none":
        return ""
    # Editing either the protocol or destination creates a different native vault item.
    return "SEEKINO_PROVIDER_" + _hash([row["id"], row["protocol"], row["baseUrl"]])[:32].upper()


class ServiceProfiles:
    def __init__(self, root, settings):
        self.path = Path(root) / "model-services.json"
        self.raw = self.path.read_bytes() if self.path.is_file() else None
        self.revision = _hash([(self.raw or b"").hex(), settings.get("revision"), settings.get("watchRevision")])
        self.rows = {}
        self.legacy_slots = {}
        self.active = {}
        self.effective = {}
        self.effective_slots = {}
        if self.raw is not None:
            doc = json.loads(self.raw)
            if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("services"), list):
                raise ValueError("服务连接文件无法读取。")
            for form in doc["services"]:
                key = form["id"]
                if not re.fullmatch("[a-f0-9]{32}", key) or key in self.rows:
                    raise ValueError("服务连接标识无效。")
                self.rows[key] = {"id": key, **clean(form)}
        if settings.get("mode") == "managed":
            form = settings["form"]
            for role in ("vision", "transcription", "embedding", "rerank"):
                if role == "embedding" and form.get("embeddingMode") == "local":
                    continue
                connection = form.get("roleConnections", {}).get(role, form)
                record = next((row for row in settings["connections"] if
                               any(b["role"] == role for b in row["roles"])), {})
                selected = {**form, **connection}
                if form.get("embeddingMode") == "local":
                    selected.update(embedding="", dimensions=DEFAULT_FORM["dimensions"], encoderModel="")
                self._project(role, selected, record.get("credentialName", ""))
        watch = settings.get("watchSettings")
        if watch:
            self._project("watch", {**watch, "watchModel": watch["model"]}, settings.get("watchCredentialName", ""))

    def _project(self, purpose, form, vault_name):
        key = form.get("serviceID") or _hash([str(self.path.parent.resolve()), "watch" if purpose == "watch" else "model", vault_name,
                                             form["protocol"], form["baseUrl"]])[:32]
        derived = {"id": key, **clean(form)}
        self.rows.setdefault(key, derived)
        self.active[purpose] = key
        self.effective_slots[purpose] = vault_name
        self.effective[purpose] = {k: derived[k] for k in ("id", "protocol", "baseUrl", "auth", "name")}
        if vault_name and slot(self.rows[key]) == slot(derived):
            self.legacy_slots[key] = vault_name

    def require(self, key):
        if key not in self.rows:
            raise ValueError("服务连接已变更，请重新打开设置。")
        return self.rows[key]

    def check(self, revision):
        if self.revision != revision:
            raise ValueError("服务连接已变更，请重新打开设置。")

    def credential_names(self, *selected):
        rows = selected or tuple(self.rows.values())
        return {name for row in rows for name in (slot(row), self.legacy_slots.get(row["id"])) if name}

    def get_key(self, row, keys):
        key = slot(row)
        if not key:
            return ""
        legacy = self.legacy_slots.get(row["id"])
        if legacy and key == slot(self.rows[row["id"]]):
            return keys.get(key) or keys.get(legacy) or ""
        return keys.get(key) or ""

    def at_destination(self, row, role):
        current = self.effective[role]
        return all(row[field] == current[field] for field in ("id", "protocol", "baseUrl", "auth"))

    def view(self, keys):
        profiles = []
        for row in self.rows.values():
            assigned = [role for role, key in self.active.items() if key == row["id"]]
            active = [role for role in assigned if self.at_destination(row, role)]
            profiles.append({**row, "hasKey": bool(self.get_key(row, keys)), "activeRoles": active,
                             "pendingRoles": [role for role in assigned if role not in active],
                             "parseActive": any(role != "watch" for role in active), "watchActive": "watch" in active})
        return {"servicesRevision": self.revision, "serviceProfiles": profiles, "effectiveRoleServices": self.effective,
                "parseRoleServices": {role: key for role, key in self.active.items() if role != "watch"}}

    def edit(self, form):
        key = form.get("id") or uuid4().hex
        prev = self.require(key) if form.get("id") else {}
        return {"id": key, **clean({**prev, **form})}

    def persist(self, rows):
        data = json.dumps({"version": 1, "services": list(rows.values())}, ensure_ascii=False, indent=2).encode() + b"\n"
        self._replace(data)

    def _replace(self, data):
        if self.path.is_symlink():
            raise ValueError("服务连接文件是符号链接，请检查资料库设置。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.path.parent, prefix=".model-services-", delete=False) as handle:
            temp = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def restore(self):
        if self.raw is None:
            self.path.unlink(missing_ok=True)
        else:
            self._replace(self.raw)

    def apply_form(self, key, purpose, roles):
        row = self.require(key)
        rows = self.rows.copy()
        if purpose == "watch":
            model = _text(roles, "watchModel")
            rows[key] = {**row, "watchModel": model}
            return rows, {"serviceID": key, "protocol": row["protocol"], "baseUrl": row["baseUrl"],
                          "auth": row["auth"], "model": model}, {"connection": row}

        if purpose != "model":
            raise ValueError("未知的模型设置目标。")
        selected = roles.get("roleServices") or {role: key for role in ("vision", "transcription", "embedding", "rerank")}
        if not isinstance(selected, dict) or selected.get("vision") != key:
            raise ValueError("请检查模型用途选择。")
        mode = roles.get("embeddingMode", "api")
        required_roles = ("vision", "transcription") if mode == "local" else ("vision", "transcription", "embedding")
        assigned = {role: self.require(selected.get(role)) for role in required_roles}
        assigned["rerank"] = self.require(selected.get("rerank") or key)
        form = {}
        for role, target in assigned.items():
            model = _text(roles, role, required=role != "rerank" or bool(selected.get("rerank") and selected["rerank"] != key))
            form[role] = model
            rows[target["id"]] = {**rows[target["id"]], role: model}
        if mode != "local":
            embedding = assigned["embedding"]
            dims = roles.get("dimensions", embedding["dimensions"])
            encoder = roles.get("encoderModel", "")
            rows[embedding["id"]] = {**rows[embedding["id"]], "dimensions": dims, "encoderModel": encoder}
            form.update(dimensions=int(dims), encoderModel=encoder)
        for target in assigned.values():
            rows[target["id"]] = {"id": target["id"], **clean(rows[target["id"]])}
        connections = {role: {"serviceID": target["id"], **{k: target[k] for k in
                       ("name", "protocol", "baseUrl", "auth")}} for role, target in assigned.items()}
        return rows, {**connections["vision"], **form, "embeddingMode": mode,
                      "roleConnections": connections}, assigned
