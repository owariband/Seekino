from unittest.mock import Mock

import httpcore
import httpx
import pytest
from pydantic import SecretStr

from gateway.personal_endpoint import PersonalModelTarget, _EndpointNetwork, normalize_personal_endpoint, personal_http_client


@pytest.mark.parametrize("url", ["file:///etc", "https://u:p@upstream.com", "https://host/path?key=x",
    "https://host/#", "https://host\\@example.com", "https://169.254.169.254", "https://[::ffff:169.254.169.254]", "https://0.0.0.0"])
def test_forbidden_in_all_modes(url):
    with pytest.raises(ValueError):
        normalize_personal_endpoint(url, allow_local=True)


def test_cloud_policy_and_exact_origin(monkeypatch):
    for value in ("http://localhost", "https://10.0.0.1", "https://[::1]", "http://example.org"):
        with pytest.raises(ValueError):
            normalize_personal_endpoint(value, allow_local=False)
    assert normalize_personal_endpoint("http://[::1]:3000/v1/", allow_local=True) == "http://[::1]:3000/v1"
    target = PersonalModelTarget("openai_chat", "https://custom.example/v1", "any", SecretStr(""), False)
    with personal_http_client(target, allow_local=False, timeout=1) as client:
        with pytest.raises(httpx.RequestError, match="已配置接口"):
            client.get("https://other.example")
    dial = Mock()
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", dial)
    responses = [("93.184.216.34", 443)]
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", item) for item in responses])
    transport = _EndpointNetwork(False, True)
    transport.connect_tcp("custom.example", 443)
    assert dial.call_args.args[0] == "93.184.216.34"
    responses.append(("127.0.0.1", 443))
    with pytest.raises(httpcore.ConnectError):
        transport.connect_tcp("custom.example", 443)
    assert dial.call_count == 1
