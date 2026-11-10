"""Fetch model IDs on demand from the configured connection, using the same outbound policy as chat."""
import json

import httpx

from .personal_endpoint import endpoint_http_client


# Status codes meaning the server has no model-list endpoint at all (e.g. whisper.cpp), not a failed request.
LIST_NOT_PROVIDED = {404, 405, 501}


def discover_models(protocol: str, base_url: str, api_key: str | None, *, allow_local: bool) -> list[str] | None:
    """Return the model IDs the service lists, or None when the service has no model-list endpoint."""
    headers = {}
    if protocol == "ark":
        raise ValueError("请使用方舟推理接入点的模型 ID")
    if protocol == "ollama_embedding":
        path, items, field = "/api/tags", "models", "name"
    elif protocol == "llama_cpp_embedding":
        path, items, field = ("/models" if base_url.endswith("/v1") else "/v1/models"), "data", "id"
    elif protocol == "gemini":
        path, items, field = "/models", "models", "name"
        if api_key:
            headers["x-goog-api-key"] = api_key
    elif protocol == "anthropic_messages":
        path = "/models" if base_url.endswith("/v1") else "/v1/models"
        items, field = "data", "id"
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    else:
        path, items, field = "/models", "data", "id"
    if api_key and protocol not in {"gemini", "anthropic_messages"}:
        headers["Authorization"] = "Bearer " + api_key
    found: dict[str, None] = {}
    params = {}
    try:
        with endpoint_http_client(base_url, allow_local=allow_local, timeout=8) as client:
            for _ in range(5):
                with client.stream("GET", base_url + path, headers=headers, params=params) as response:
                    if response.status_code in LIST_NOT_PROVIDED and not found and not params:
                        return None
                    if not response.is_success:
                        raise ValueError(f"模型列表接口返回 HTTP {response.status_code}，可手动填写模型 ID")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            raise ValueError("模型列表响应过大")
                data = json.loads(content)
                if not isinstance(data, dict) or not isinstance(data.get(items), list):
                    raise ValueError("服务未返回模型列表，可手动填写模型 ID")
                for entry in data[items]:
                    name = entry.get(field) if isinstance(entry, dict) else None
                    if isinstance(name, str) and 0 < len(name) <= 255 and name.isprintable():
                        found[name.removeprefix("models/") if protocol == "gemini" else name] = None
                if protocol == "gemini" and data.get("nextPageToken"):
                    params = {"pageToken": data["nextPageToken"]}
                elif protocol == "anthropic_messages" and data.get("has_more") and data.get("last_id"):
                    params = {"after_id": data["last_id"]}
                else:
                    break
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("无法获取模型列表，请检查连接或手动填写模型 ID") from None
    return list(found)
