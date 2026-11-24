"""Desktop model discovery reuses the Gateway policy without releasing another destination's key."""
import json

import pytest

from gateway.constants import ARK_OPENAI_BASE_URL
from server.desktop.model_catalogue import discover
from server.desktop.model_settings import ModelProfile
from server.desktop.watch_settings import prepare_watch, save_watch, watch_credentials, watch_view


def form(protocol="openai_chat", **changes):
    return {"name": "Studio", "protocol": protocol,
            "baseUrl": ARK_OPENAI_BASE_URL if protocol == "ark" else "http://localhost:7878/v1",
            "auth": "key", "vision": "test-vision", "transcription": "test-asr", "embedding": "test-embed",
            "dimensions": 64, "rerank": "", **changes}


def managed(tmp_path):
    profile = ModelProfile.read(tmp_path).candidate(form())
    profile.persist()
    return ModelProfile.read(tmp_path)


def test_reuses_a_saved_key_only_for_its_stored_destination(tmp_path, monkeypatch):
    profile = managed(tmp_path)
    slot = profile.slots()[profile.source]
    calls = []

    def list_models(protocol, base_url, api_key, *, allow_local):
        calls.append((protocol, base_url, api_key, allow_local))
        return ["vision", "embedding"]

    monkeypatch.setattr("server.desktop.model_catalogue.discover_models", list_models)
    keys = {slot: "saved-secret"}
    same = discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:7878/v1",
                    api_key="", revision=profile.revision, keys=keys)
    assert same == {"modelIds": ["vision", "embedding"], "listAvailable": True}
    assert calls[-1] == ("openai_chat", "http://localhost:7878/v1", "saved-secret", True)

    changed = discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:9999/v1",
                       api_key="", revision=profile.revision, keys=keys)
    assert changed == {"modelIds": ["vision", "embedding"], "listAvailable": True}
    assert calls[-1][2] is None

    typed = discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:9999/v1",
                     api_key="typed-secret", revision=profile.revision, keys=keys)
    assert typed["modelIds"] == ["vision", "embedding"]
    assert calls[-1][2] == "typed-secret"


def test_watch_discovery_uses_only_the_saved_watch_destination(tmp_path, monkeypatch):
    profile = managed(tmp_path)
    target = {"protocol": "openai_chat", "baseUrl": "https://vision.example/v1", "model": "vision", "auth": "key"}
    revision = watch_view(tmp_path, profile, {})["watchRevision"]
    prepared = prepare_watch(tmp_path, {"revision": revision, "form": target})
    slot = watch_credentials(prepared)["credentialName"]
    save_watch(tmp_path, prepared, {slot: "watch-secret"})
    calls = []

    def list_models(protocol, base_url, api_key, *, allow_local):
        calls.append((protocol, base_url, api_key, allow_local))
        return ["vision-two"]

    monkeypatch.setattr("server.desktop.model_catalogue.discover_models", list_models)
    keys = {slot: "watch-secret"}
    revision = watch_view(tmp_path, profile, {})["watchRevision"]
    assert discover(tmp_path, purpose="watch", protocol="openai_chat", base_url=target["baseUrl"],
                    api_key="", revision=revision, keys=keys)["modelIds"] == ["vision-two"]
    assert calls[-1][2] == "watch-secret"
    assert discover(tmp_path, purpose="watch", protocol="openai_chat", base_url="https://other.example/v1",
                    api_key="", revision=revision, keys=keys)["modelIds"] == ["vision-two"]
    assert calls[-1][2] is None


def test_missing_list_manual_ark_and_stale_revision(tmp_path, monkeypatch):
    profile = managed(tmp_path)
    monkeypatch.setattr("server.desktop.model_catalogue.discover_models", lambda *args, **kwargs: None)
    result = discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:7878/v1",
                      api_key="", revision=profile.revision, keys={})
    assert result == {"modelIds": [], "listAvailable": False}
    with pytest.raises(ValueError, match="方舟"):
        discover(tmp_path, purpose="model", protocol="ark", base_url=ARK_OPENAI_BASE_URL,
                 api_key="", revision=profile.revision, keys={})
    with pytest.raises(ValueError, match="已变更"):
        discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:7878/v1",
                 api_key="", revision="stale", keys={})
    with pytest.raises(ValueError, match="OpenAI 兼容"):
        discover(tmp_path, purpose="model", protocol="other", base_url="http://localhost:7878/v1",
                 api_key="", revision=profile.revision, keys={})


def test_errors_never_repeat_a_typed_or_saved_key(tmp_path, monkeypatch):
    profile = managed(tmp_path)
    slot = profile.slots()[profile.source]

    def reject(protocol, base_url, api_key, *, allow_local):
        raise ValueError(f"provider echoed {api_key}")

    monkeypatch.setattr("server.desktop.model_catalogue.discover_models", reject)
    with pytest.raises(ValueError) as typed:
        discover(tmp_path, purpose="model", protocol="openai_chat", base_url="https://models.example/v1",
                 api_key="typed-secret", revision=profile.revision, keys={})
    assert "typed-secret" not in str(typed.value)
    with pytest.raises(ValueError) as saved:
        discover(tmp_path, purpose="model", protocol="openai_chat", base_url="http://localhost:7878/v1",
                 api_key="", revision=profile.revision, keys={slot: "saved-secret"})
    assert "saved-secret" not in str(saved.value)


def test_dispatch_scrubs_the_discovery_key(tmp_path, monkeypatch):
    from server.desktop import app

    monkeypatch.setattr(app, "_application", None)

    def reject(*args, **kwargs):
        raise ValueError("provider echoed typed-secret")

    monkeypatch.setattr(app, "discover_models", reject)
    response = json.loads(app.dispatch(json.dumps({
        "action": "discoverModels",
        "args": {"root": str(tmp_path), "purpose": "model", "protocol": "openai_chat",
                 "baseUrl": "http://localhost:7878/v1", "apiKey": "typed-secret", "revision": ""},
    })))
    assert "typed-secret" not in response["error"]
