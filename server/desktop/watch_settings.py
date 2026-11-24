"""An optional vision connection for the watch player, independent of a library's index."""
from hashlib import sha256
from ipaddress import ip_address
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from gateway.config import ArkModelSourceConfig, ChatAPISourceConfig, ModelBinding
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.personal_endpoint import normalize_personal_endpoint


def _read(root):
    path = Path(root) / "watch-model.json"
    raw = path.read_bytes() if path.is_file() else b""
    revision = sha256(raw).hexdigest()
    try:
        form = json.loads(raw) if raw else {}
        return (_parse(form) if form != {} else {}), revision, ""
    except (TypeError, ValueError):
        return {}, revision, "观看问答设置文件无法读取，请重新保存连接。"


def _slot(form):
    if not form or form["auth"] == "none":
        return ""
    destination = json.dumps([form["protocol"], form["baseUrl"]]).encode()
    return "SEEKINO_WATCH_" + sha256(destination).hexdigest()[:32].upper()


def prepare_watch(root, args):
    _, revision, _ = _read(root)
    if args.get("revision") != revision:
        raise ValueError("观看问答设置已变更，请重新打开。")
    return _parse(args.get("form"))


def _parse(form):
    if not isinstance(form, dict):
        raise ValueError("请填写观看问答连接。")
    if form.get("inherit") is True:
        return {}
    protocol = form.get("protocol")
    if protocol not in {"ark", "openai_chat"}:
        raise ValueError("请选择 Ark 或 OpenAI 兼容协议。")
    base = normalize_personal_endpoint(str(form.get("baseUrl") or ""), allow_local=True)
    model = str(form.get("model") or "").strip()
    if not model or len(model) > 255:
        raise ValueError("请填写支持图片输入的模型 ID。")
    if protocol == "ark" and base != ARK_OPENAI_BASE_URL:
        raise ValueError("Ark 协议使用内置的服务地址。")
    host = urlsplit(base).hostname
    try:
        local = ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    auth = form.get("auth", "key")
    if auth not in {"none", "key"} or (auth == "none" and (protocol != "openai_chat" or not local)):
        raise ValueError("免密连接仅适用于本机 OpenAI 兼容服务。")
    return {"protocol": protocol, "baseUrl": base, "model": model, "auth": auth}


def watch_credentials(form):
    slot = _slot(form)
    return {"credentialName": slot, "credentialNames": [slot] if slot else []}


def watch_view(root, profile, keys):
    form, revision, issue = _read(root)
    config = profile.config
    binding = (config.resolve_model_binding("vision", environ={})
               if "vision" in config.normalized_models or "vision" in config.model_roles else None)
    inherited = bool(binding and binding.supports_vision_input
                     and config.source(binding.source).adapter in {"ark", "chat_api", "openai", "openai_vision"}
                     and profile.ready_sources(keys).get(binding.source))
    slot = _slot(form)
    ready = (not slot or bool(keys.get(slot))) if form else inherited
    return {"watchReady": ready and not issue, "watchSettings": form, "watchRevision": revision,
            "watchCredentialName": slot, "watchHasKey": bool(slot and keys.get(slot)),
            "watchSettingsIssue": issue, "watchInheritedModel": binding.model if inherited else ""}


def save_watch(root, form, keys):
    slot = _slot(form)
    if slot and not keys.get(slot):
        raise ValueError("请填写这个服务地址的 API Key。")
    path = Path(root) / "watch-model.json"
    with NamedTemporaryFile(dir=root, prefix=".watch-model-", delete=False) as handle:
        temp = Path(handle.name)
    try:
        temp.write_text(json.dumps(form, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def watch_connection(root, profile, keys):
    view = watch_view(root, profile, keys)
    if not view["watchReady"]:
        return None
    config, environ = profile.for_gateway(keys)
    form = view["watchSettings"]
    if form:
        slot = view["watchCredentialName"]
        source = ArkModelSourceConfig(api_key_env=slot) if form["protocol"] == "ark" else ChatAPISourceConfig(
            protocol="openai_chat", base_url=form["baseUrl"], api_key_env=slot or None)
        source_name = "desktop_watch"
        config.model_sources[source_name] = source
        config.normalized_models["vision"] = ModelBinding(source=source_name, model=form["model"],
            supports_vision_input=True, stream=False, request_timeout_seconds=60, max_output_tokens=2048)
        if slot:
            environ[slot] = keys[slot]
    return config, environ
