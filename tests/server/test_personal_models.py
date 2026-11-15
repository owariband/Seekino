import json
from uuid import uuid4

import httpx
import pytest
import yaml
from langchain_core.tools import tool
from pydantic import BaseModel, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gateway.agent_model.context import ContextWindowPolicy
from gateway.config import load_config, resolve_source_api_key
from gateway.personal_endpoint import PersonalModelTarget
from gateway import personal_models

from server.config.yaml_document import GlobalAgentSettingsConflictError
from server.model.dto.personal_model import PersonalModelConnectionSave
from server.model.personal_models import PersonalModelPreferences
from server.service.model_settings import ModelSettings, PersonalModelNotFoundError


def _input(connection, **updates):
    fields = dict(name=connection.name, protocol=connection.protocol, base_url=connection.base_url,
                  model_ids=list(dict.fromkeys(m.model_id for m in connection.models)),
                  supports_vision=connection.supports_vision, revision=connection.revision)
    return PersonalModelConnectionSave(**(fields | updates))


def test_native_settings_edit_active_yaml_and_preserve_env_secret(yaml_model_settings):
    store = yaml_model_settings
    before = yaml.safe_load(store.path.read_bytes())
    store.path.write_text("# keep operator note\n" + store.path.read_text())
    row = next(c for c in store.catalog().connections.values() if c.name == "my-endpoint")
    assert row.origin == "yaml" and row.has_api_key and row.models[0].is_yaml_default
    assert [m.model_id for m in row.models if "chat" in m.roles] == ["team/home", "team/alternative", "team/discovered"]
    assert [m.model_id for m in row.models if m.kind == "rerank"] == ["team/ranking-only"]
    assert not any(x in store.catalog().settings().model_dump_json() for x in ("synthetic-private-key", "TEST_LOCAL_PROVIDER_KEY"))
    submitted = _input(row, name="团队接口", model_ids=[m.model_id for m in row.models] + ["team/new"])
    store.save(row.connection_id, submitted)
    assert store.path.read_text().startswith("# keep operator note")
    current = yaml.safe_load(store.path.read_bytes())
    assert current["model_sources"]["my-endpoint"] == before["model_sources"]["my-endpoint"] | {"display_name": "团队接口"}
    assert all(current["models"][k] == v for k, v in before["models"].items())
    assert all(current[k] == v for k, v in before.items() if k not in {"models", "model_sources"})
    with pytest.raises(GlobalAgentSettingsConflictError):
        store.save(row.connection_id, submitted)
    reloaded = ModelSettings(store.path, allow_local=True, environ=store.environ)
    row = reloaded.catalog().connections[row.connection_id]
    assert row.name == "团队接口" and "team/new" in {m.model_id for m in row.models}
    assert reloaded.catalog().find("my-alternative").target(environ=store.environ).api_key.get_secret_value() == "synthetic-private-key"
    with pytest.raises(ValueError, match="API Key"):
        store.save(row.connection_id, _input(row, base_url="https://different.example"))
    assert not (store.path.parent / ".seekino-secrets").exists()
    with pytest.raises(ValueError, match="引用"):
        store.delete(row.connection_id, row.revision)
    store.save(row.connection_id, _input(row, api_key="rotated-synthetic-key"))
    config = load_config(store.path)
    assert resolve_source_api_key(config.source("my-endpoint"), environ={}) == "rotated-synthetic-key"
    assert all(v not in store.path.read_text() for v in ("synthetic-private-key", "rotated-synthetic-key"))
    assert (store.path.parent / ".seekino-secrets/model-config.key").stat().st_mode & 0o777 == 0o600
    assert store.catalog().find("my-alternative").target(environ={}).api_key.get_secret_value() == "rotated-synthetic-key"


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages", "gemini"])
def test_yaml_managed_protocol_default_conflict_and_removal(yaml_model_settings, protocol):
    store = yaml_model_settings
    cid = str(uuid4())
    body = PersonalModelConnectionSave(name="Team", protocol=protocol, base_url="https://api.example.test/v1",
                                       api_key="example-write-only", model_ids=["team/any"], revision=0)
    added = store.save(cid, body)
    row = next(c for c in added.connections if c.connection_id == cid)
    model_id = row.models[0].id
    assert "example-write-only" not in added.model_dump_json() + store.path.read_text()
    assert store.catalog().find(model_id).target().protocol == protocol
    assert store.set_default(model_id, added.revision).default_model_id == model_id
    with pytest.raises(GlobalAgentSettingsConflictError):
        store.set_default("default", added.revision)
    data = yaml.safe_load(store.path.read_bytes())
    assert data["models"][data["chat_default_model"]]["model"] == "team/any"
    reload = ModelSettings(store.path, allow_local=False, environ={})
    assert reload.catalog().new_chat_id() == model_id
    row = reload.catalog().connections[cid]
    changed = reload.save(cid, _input(row, model_ids=["renamed"], api_key=""))
    assert changed.default_model_id != model_id and reload.catalog().find(changed.default_model_id).target().api_key.get_secret_value() == ""
    assert reload.delete(cid, changed.revision).default_model_id == "default"
    assert not any(c.connection_id == cid for c in store.catalog().settings().connections)
    with pytest.raises(PersonalModelNotFoundError):
        store.set_default(model_id)


def test_old_personal_models_remain_owner_only_for_existing_conversations(tmp_path):
    import base64
    from uuid import UUID, uuid5
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from server.service.personal_models import LegacyPersonalModels
    db = create_engine("sqlite://")
    PersonalModelPreferences.__table__.create(db)
    sessions = sessionmaker(db)
    cid = str(uuid4())
    path = tmp_path / "old.key"
    path.write_bytes(AESGCM.generate_key(bit_length=256))
    nonce = b"test-nonce12"
    aad = json.dumps(["alice", cid, "gemini", "https://team.example"]).encode()
    sealed = base64.b64encode(nonce + AESGCM(path.read_bytes()).encrypt(nonce, b"old-synthetic", aad)).decode()
    with sessions.begin() as session:
        session.add(PersonalModelPreferences(owner_id="alice", default_model="default", revision=1, connections=[
            dict(id=cid, protocol="gemini", url="https://team.example", models=["old"], key=sealed, vision=False)]))
    legacy = LegacyPersonalModels(sessions, key_file=path, signing_secret="")
    ref = f"personal:{uuid5(UUID(cid), 'old')}"
    assert legacy.resolve("alice", ref).api_key.get_secret_value() == "old-synthetic"
    with pytest.raises(PersonalModelNotFoundError):
        legacy.resolve("bob", ref)
    db.dispose()


def test_legacy_yaml_honors_chat_override_before_and_after_an_edit(tmp_path):
    from pathlib import Path
    path = tmp_path / "v1.yaml"
    path.write_bytes((Path(__file__).parents[1] / "gateway/fixtures/legacy-config.yaml").read_bytes())
    env = {"SEEKINO_AGENT_CHAT_MODEL": "deployment/override", "DEEPSEEK_API_KEY": "fake", "ARK_API_KEY": "fake"}
    store = ModelSettings(path, allow_local=True, environ=env)
    assert store.catalog().find("default").binding.model == "deployment/override"
    store.save(str(uuid4()), PersonalModelConnectionSave(name="added", protocol="gemini",
               base_url="https://api.example.test", model_ids=["another/model"], revision=0))
    assert load_config(path).config_version == 2
    assert ModelSettings(path, allow_local=True, environ=env).catalog().find("default").binding.model == "deployment/override"


class PersonalStructuredResult(BaseModel):
    answer: str


@tool
def lookup_video(query: str) -> str:
    """Find a video by text."""
    return "fixture result"


@pytest.mark.parametrize(("protocol", "suffix", "auth"), [
    ("openai_chat", "/chat/completions", "authorization"),
    ("openai_responses", "/responses", "authorization"),
    ("anthropic_messages", "/messages", "x-api-key"),
    ("gemini", ":generateContent", "x-goog-api-key"),
])
def test_personal_structured_executes_selected_target_tools_without_global_model(
    monkeypatch, protocol, suffix, auth,
):
    requests = []
    model_id = "team/selected-chat" if protocol != "gemini" else "selected-gemini"
    tool_result_markers = {
        "openai_chat": '"tool_call_id"',
        "openai_responses": '"function_call_output"',
        "anthropic_messages": '"tool_result"',
        "gemini": '"functionResponse"',
    }

    def reply(*, name, arguments, call_id):
        if protocol == "openai_chat":
            call = {"id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}
            return {"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [call],
            }}]}
        if protocol == "openai_responses":
            item = {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id,
                    "name": name, "arguments": json.dumps(arguments)}
            return {"id": f"resp_{call_id}", "object": "response", "output": [item],
                    "status": "completed", "model": model_id}
        if protocol == "anthropic_messages":
            block = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
            return {"content": [block]}
        return {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": name, "args": arguments}},
        ]}, "finishReason": "STOP"}]}

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path.endswith(suffix)
        assert request.url.host == "selected.example"
        assert request.headers[auth].removeprefix("Bearer ") == "personal-key"
        if protocol == "gemini":
            assert model_id in str(request.url)
        else:
            assert payload["model"] == model_id
        assert "lookup_video" in json.dumps(payload["tools"])
        if len(requests) == 1:
            return httpx.Response(200, json=reply(
                name="lookup_video", arguments={"query": "q"}, call_id="call_1",
            ))
        assert "fixture result" in json.dumps(payload)
        assert tool_result_markers[protocol] in json.dumps(payload)
        return httpx.Response(200, json=reply(
            name="PersonalStructuredResult", arguments={"answer": "done"}, call_id="call_2",
        ))

    client = httpx.Client(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(personal_models, "personal_http_client", lambda *_args, **_kwargs: client)
    target = PersonalModelTarget(protocol, "https://selected.example/v1", model_id,
                                 SecretStr("personal-key"), True)
    policy = ContextWindowPolicy(32_000, 2_048, 2_048, 22_000, 12_000)
    model = personal_models.build_personal_structured(
        target, request_timeout_seconds=30, max_output_tokens=2_048,
        max_input_chars=80_000, context_window_policy=policy, allow_local=True,
    )
    assert model.model_name == model_id
    assert model.provider_name == protocol
    assert model.supports_vision_input is True
    assert model.max_output_tokens == 2_048
    assert model.max_input_chars == 80_000
    assert model.context_window_policy == policy
    result = model.run_structured(
        "Find the video.", tools=[lookup_video], result_type=PersonalStructuredResult,
        max_model_calls=3,
    )
    assert result == PersonalStructuredResult(answer="done")
    assert len(requests) == 2
    model.close()
    assert client.is_closed


def test_personal_structured_closes_private_client_when_construction_fails(monkeypatch):
    closed = []

    class _Client:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(personal_models, "personal_http_client", lambda *_args, **_kwargs: _Client())

    def fail(*_args, **_kwargs):
        raise RuntimeError("structured setup failed")

    monkeypatch.setattr(personal_models, "_PersonalStructured", fail)
    target = PersonalModelTarget("openai_chat", "https://selected.example/v1", "team/selected",
                                 SecretStr("personal-key"), False)
    with pytest.raises(RuntimeError, match="structured setup failed"):
        personal_models.build_personal_structured(
            target, request_timeout_seconds=30, max_output_tokens=2_048,
            max_input_chars=80_000,
            context_window_policy=ContextWindowPolicy(32_000, 2_048, 2_048, 22_000, 12_000),
            allow_local=True,
        )
    assert closed == [True]
