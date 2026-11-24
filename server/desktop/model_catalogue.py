"""List model IDs from a desktop model service without adding an HTTP endpoint."""
from __future__ import annotations

from collections.abc import Mapping

from gateway.model_discovery import discover_models
from gateway.personal_endpoint import normalize_personal_endpoint

from .model_settings import ModelProfile
from .watch_settings import watch_credentials, watch_view


def _saved(keys: Mapping | None, slot: str) -> str:
    value = keys.get(slot, "") if slot and isinstance(keys, Mapping) else ""
    return value.strip() if isinstance(value, str) else ""


def _message(error: Exception, secrets: tuple[str, ...]) -> str:
    message = str(error) or "无法获取模型列表，请检查服务地址后手动填写模型 ID"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "••••")
    return message[:512]


def discover(root, *, purpose, protocol, base_url, api_key, revision, keys):
    """Return the service's model IDs, or a no-list result when its endpoint cannot list them."""
    if protocol == "ark":
        raise ValueError("方舟使用推理接入点 ID，请手动填写模型 ID。")
    if protocol != "openai_chat":
        raise ValueError("请选择 Ark 或 OpenAI 兼容协议。")
    url = normalize_personal_endpoint(str(base_url or ""), allow_local=True)
    profile = ModelProfile.read(root)
    if purpose == "model":
        if revision != profile.revision:
            raise ValueError("模型设置已变更，请重新打开设置。")
        form = profile.form
        same = bool(form) and form.get("protocol") == protocol and form.get("baseUrl") == url
        slot = profile.slots().get(profile.source or "", "") if same else ""
    elif purpose == "watch":
        view = watch_view(root, profile, keys or {})
        if revision != view["watchRevision"]:
            raise ValueError("观看问答设置已变更，请重新打开。")
        form = view["watchSettings"]
        same = bool(form) and form.get("protocol") == protocol and form.get("baseUrl") == url
        slot = watch_credentials(form)["credentialName"] if same else ""
    else:
        raise ValueError("未知的模型设置目标。")
    typed = api_key.strip() if isinstance(api_key, str) else ""
    # A saved key belongs to its stored destination. Only the served profile can release it.
    saved = _saved(keys, slot) if same else ""
    key = typed or saved
    try:
        found = discover_models(protocol, url, key or None, allow_local=True)
    except Exception as error:
        raise ValueError(_message(error, (typed, saved))) from None
    return {"modelIds": found or [], "listAvailable": found is not None}
