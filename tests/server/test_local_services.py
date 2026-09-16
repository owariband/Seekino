import json

import pytest

from scripts import local_services
from scripts.local_services import api_ready, config_digest


def test_config_digest_tracks_model_api_key_changes(monkeypatch) -> None:
    monkeypatch.setenv("ARK_API_KEY", "ark-first")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-first")
    initial = config_digest()

    monkeypatch.setenv("ARK_API_KEY", "ark-second")
    ark_changed = config_digest()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-second")
    both_changed = config_digest()

    assert initial != ark_changed != both_changed
    assert len(initial) == len(ark_changed) == len(both_changed) == 64


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (
            {
                "status": "ok",
                "components": {"parseConsumer": {"status": "running"}},
            },
            True,
        ),
        (
            {
                "status": "degraded",
                "components": {"parseConsumer": {"status": "failed"}},
            },
            False,
        ),
        (
            {
                "status": "ok",
                "components": {"parseConsumer": {"status": "stopped"}},
            },
            False,
        ),
        ({"status": "ok"}, False),
    ],
)
def test_api_ready_requires_running_parse_consumer(monkeypatch, health, expected) -> None:
    requested = []

    def get(url: str) -> bytes:
        requested.append(url)
        return json.dumps(health).encode()

    monkeypatch.setattr(local_services, "get", get)

    assert api_ready("http://127.0.0.1:9621") is expected
    assert requested == ["http://127.0.0.1:9621/healthz"]


@pytest.mark.parametrize("payload", [None, b"not-json", b"[]"])
def test_api_ready_rejects_invalid_health_payload(monkeypatch, payload) -> None:
    monkeypatch.setattr(local_services, "get", lambda _url: payload)

    assert not api_ready("http://127.0.0.1:9621")


def test_start_requires_model_key_before_launching_server(monkeypatch) -> None:
    monkeypatch.setenv("ANIVAG_HTTP_PORT", "9621")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(local_services, "listening", lambda _port: False)
    monkeypatch.setattr(local_services, "owned_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        local_services.start_applications()
