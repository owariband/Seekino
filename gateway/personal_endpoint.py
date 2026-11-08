"""Transport for a user-supplied model API. Preserve TLS SNI while pinning the dial to a checked IP."""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpcore
import httpx
from pydantic import SecretStr

PersonalProtocol = Literal["openai_chat", "openai_responses", "anthropic_messages", "gemini"]


@dataclass(frozen=True, slots=True)
class PersonalModelTarget:
    protocol: PersonalProtocol
    base_url: str
    model_id: str
    api_key: SecretStr
    supports_vision: bool


def _allowed(address: str, allow_local: bool, https: bool) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return allow_local
    if isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96")):
        return False
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip.is_global:
        return https
    return allow_local and (ip.is_loopback or any(ip in net for net in _LOCAL_NETWORKS if net.version == ip.version))


_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def normalize_personal_endpoint(value: str, *, allow_local: bool) -> str:
    value = value.strip()
    invalid = "请输入有效的 API Base URL"
    if not value or len(value) > 2048 or any(ord(c) < 33 or c in "\\?#" for c in value):
        raise ValueError(invalid)
    try:
        parts = urlsplit(value)
        port = parts.port
        url = httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ValueError(invalid) from None
    if parts.username is not None or not parts.hostname or port == 0 or url.scheme not in {"http", "https"}:
        raise ValueError(invalid)
    if "%" in parts.hostname or (not allow_local and url.scheme != "https"):
        raise ValueError("该部署要求使用公网 HTTPS 接口")
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        if not allow_local and parts.hostname.rstrip(".").lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("该部署要求使用公网 HTTPS 接口") from None
    else:
        if not _allowed(str(ip), allow_local, url.scheme == "https"):
            raise ValueError("不支持此接口地址")
    return str(url).rstrip("/")


class _EndpointNetwork(httpcore.SyncBackend):
    def __init__(self, allow_local: bool, https: bool):
        self.allow_local = allow_local
        self.https = https

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except OSError:
            raise httpcore.ConnectError("无法解析接口地址") from None
        if not addresses or not all(_allowed(ip, self.allow_local, self.https) for ip in addresses):
            raise httpcore.ConnectError("不支持此接口地址")
        for ip in addresses:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            try:
                return super().connect_tcp(ip, port, remaining, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if ip == addresses[-1]:
                    raise
        raise httpcore.ConnectError("无法连接接口")


class _EndpointTransport(httpx.HTTPTransport):
    def __init__(self, url: httpx.URL, allow_local: bool):
        super().__init__(trust_env=False, retries=0)
        # httpx's sync transport owns this pool and its response/exception adapters.
        self._pool._network_backend = _EndpointNetwork(allow_local, url.scheme == "https")
        self.origin = (url.scheme, url.host, url.port)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.host, request.url.port) != self.origin:
            raise httpx.RequestError("模型请求超出已配置接口", request=request)
        return super().handle_request(request)


def personal_http_client(target: PersonalModelTarget, *, allow_local: bool, timeout: float) -> httpx.Client:
    url = httpx.URL(normalize_personal_endpoint(target.base_url, allow_local=allow_local))
    return httpx.Client(transport=_EndpointTransport(url, allow_local), timeout=timeout, trust_env=False, follow_redirects=False)
