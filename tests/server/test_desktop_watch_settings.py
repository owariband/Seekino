"""Watch connections can change independently of a populated desktop library."""
import json

import pytest

from gateway.constants import ARK_OPENAI_BASE_URL
from server.desktop.model_settings import ModelProfile
from server.desktop.watch_settings import (
    prepare_watch, save_watch, watch_connection, watch_credentials, watch_view,
)


def prepare(root, profile, **changes):
    form = {"protocol": "openai_chat", "model": "vision", "auth": "key", "baseUrl": "https://vision.example/v1"}
    return prepare_watch(root, {"revision": watch_view(root, profile, {})["watchRevision"], "form": form | changes})


@pytest.mark.parametrize("protocol,base", [("openai_chat", "https://vision.example/v1"), ("ark", ARK_OPENAI_BASE_URL)])
def test_watch_only_connection(tmp_path, protocol, base):
    profile = ModelProfile.read(tmp_path)
    original = profile.config.model_dump()
    form = prepare(tmp_path, profile, protocol=protocol, baseUrl=base)
    slot = watch_credentials(form)["credentialName"]
    with pytest.raises(ValueError, match="API Key"):
        save_watch(tmp_path, form, {})
    save_watch(tmp_path, form, {slot: "private-test-key"})
    assert "private-test-key" not in (tmp_path / "watch-model.json").read_text()
    assert watch_connection(tmp_path, profile, {}) is None
    cfg, env = watch_connection(tmp_path, profile, {slot: "private-test-key"})
    binding = cfg.resolve_model_binding("vision", environ=env)
    assert binding.model == "vision" and binding.supports_vision_input
    assert env[cfg.source(binding.source).api_key_env] == "private-test-key"
    assert cfg.resolve_model_binding("embedding", environ=env) == profile.config.resolve_model_binding("embedding", environ={})
    assert profile.config.model_dump() == original and not profile.path.exists()
    changed = prepare(tmp_path, profile, baseUrl="https://another.example/v1")
    assert slot != watch_credentials(changed)["credentialName"]


def test_local_no_key_and_malformed_config_recovery(tmp_path):
    profile = ModelProfile.read(tmp_path)
    with pytest.raises(ValueError, match="免密"):
        prepare(tmp_path, profile, auth="none")
    path = tmp_path / "watch-model.json"
    path.write_bytes(b"broken")
    initial = watch_view(tmp_path, profile, {})
    assert not initial["watchReady"] and initial["watchSettingsIssue"]
    form = prepare(tmp_path, profile, baseUrl="http://localhost:7878/v1/", auth="none")
    save_watch(tmp_path, form, {})
    assert json.loads(path.read_text()) == form
    assert watch_connection(tmp_path, profile, {}) is not None
    with pytest.raises(ValueError, match="已变更"):
        prepare_watch(tmp_path, {"revision": initial["watchRevision"], "form": {"inherit": True}})
