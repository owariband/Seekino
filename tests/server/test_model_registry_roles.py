import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from pydantic import BaseModel

from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.personal_models import probe_personal_model
from server.config.parse_profile import default_video_parse_v2_config
from server.config.yaml_document import GlobalAgentSettingsConflictError
from server.model.dto.personal_model import ModelConnectionEndpoint, PersonalModelConnectionSave
from server.service.model_settings import ModelSettings


class Reading(BaseModel):
    text: str


def register(store, name, key, models, protocol="openai_chat"):
    cid = str(uuid4())
    view = store.save(cid, PersonalModelConnectionSave(name=name, base_url="https://fixture.example/v1",
        protocol=protocol, model_ids=[m["model_id"] for m in models], models=models, revision=0, api_key=key))
    return next(c for c in view.connections if c.connection_id == cid)


def test_registry_resolves_separate_keys_and_reaches_the_selected_transports(yaml_model_settings, monkeypatch, tmp_path):
    store = yaml_model_settings
    requests = []
    def respond(req):
        path = req.url.path
        requests.append((path, req.headers.get("authorization"), req.content))
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "work/vision"}]})
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={"model": "work/vector", "data": [{"index": 0, "embedding": [0.6, 0.8]}]})
        if path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "sample speech", "segments": []})
        assert path.endswith("/chat/completions")
        payload = json.loads(req.content)
        if payload["model"] == "work/rank":
            text = '{"scores":[{"index":0,"score":0.8}]}'
        else:
            text = '{"text":"sample frame"}'
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens":7, "completion_tokens":1, "total_tokens":8}})
    monkeypatch.setattr("gateway.personal_endpoint._EndpointTransport", lambda *_: httpx.MockTransport(respond))
    gen = {"model_id": "work/vision", "kind": "generative", "supports_vision": True}
    a = register(store, "Live", "secret-live", [gen])
    b = register(store, "Batch", "secret-batch", [gen, {"model_id": "work/audio", "kind": "transcription"},
                 {"model_id": "work/vector", "kind": "embedding", "dimensions": 2}, {"model_id": "work/rank", "kind": "rerank"}])
    assert a.models[0].id != b.models[0].id
    assert b.models[2].roles == ["embedding"]
    assert store.catalog().find(b.models[2].id) is None
    for role, model in [("chat", a.models[0]), ("vision", b.models[0]), ("ocr", b.models[0]), ("transcription", b.models[1]),
                        ("embedding", b.models[2]), ("rerank", b.models[3])]:
        view = store.assign(role, model.id, store.catalog().revision)
        assert next(r for r in view.roles if r.role == role).model_id == model.id
    raw = yaml.safe_load(store.path.read_bytes())
    assert all(isinstance(ref, str) and ref in raw["models"] for ref in raw["model_roles"].values())
    assert not any(secret in store.path.read_text() + view.model_dump_json() for secret in ("secret-live", "secret-batch"))
    effective = load_config(store.path)
    snapshot = default_video_parse_v2_config(effective).models
    assert (snapshot.asr.model, snapshot.understanding.model, snapshot.ocr.model) == ("work/audio", "work/vision", "work/vision")
    probe_personal_model(store.catalog().find(a.models[0].id).target(), allow_local=True)
    assert requests[-1][:2] == ("/v1/chat/completions", "Bearer secret-live")
    image = tmp_path / "frame.png"
    from PIL import Image
    Image.new("RGB", (2, 2), "red").save(image)
    client = GatewayClient(effective, allow_local_endpoints=True)
    try:
        for role in ("vision", "ocr"):
            model = client.create_generative_model(role)
            from gateway.usage import capture_model_usage
            calls = []
            with capture_model_usage(calls.append):
                assert model.generate([ImageInput(image)], schema_name="read", result_type=Reading).text == "sample frame"
            assert calls[0].usage.total_tokens == 8
            sent = json.loads(requests[-1][2])
            assert sent["model"] == "work/vision" and "data:image/png;base64," in json.dumps(sent)
            model.close()
        audio = tmp_path / "speech.wav"
        audio.write_bytes(b"RIFFfixture")
        model = client.create_transcription_model()
        assert model.transcribe(audio)["text"] == "sample speech" and b"work/audio" in requests[-1][2]
        model.close()
        model = client.create_embedding_model()
        assert model.embed_query("sample").vector == [0.6, 0.8]
        assert json.loads(requests[-1][2])["dimensions"] == 2
        model.close()
        model = client.create_rerank_model()
        assert model.rerank("q", ["d"])[0].score == 0.8
        model.close()
    finally:
        client.close()
    assert all(req[1] == "Bearer secret-batch" for req in requests[1:])
    opts = ModelConnectionEndpoint(protocol="openai_chat", base_url=b.base_url, revision=view.revision)
    assert store.discover(b.connection_id, opts) == ["work/vision"]
    with pytest.raises(ValueError, match="API Key"):
        store.discover(b.connection_id, opts.model_copy(update={"base_url":"https://different.example"}))
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="用途"):
        store.assign("vision", b.models[2].id, view.revision)
    with pytest.raises(GlobalAgentSettingsConflictError):
        store.assign("vision", None, view.revision - 1)
    with pytest.raises(ValueError, match="引用"):
        store.delete(b.connection_id, view.revision)
    assert store.path.read_bytes() == original
    store.assign("vision", None, view.revision)
    assert load_config(store.path).resolve_model_binding("vision").model != "work/vision"
    assert snapshot.understanding.model == "work/vision"


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic_messages", "gemini"])
def test_registered_visual_model_uses_its_wire_protocol(yaml_model_settings, monkeypatch, protocol):
    store = yaml_model_settings
    c = register(store, protocol, "demo", [{"model_id":"any-id", "kind":"generative", "supports_vision":True}], protocol)
    store.assign("ocr", c.models[0].id, store.catalog().revision)
    text = '{"text":"yes"}'
    def respond(req):
        payload = json.loads(req.content)
        if protocol == "anthropic_messages":
            assert req.url.path.endswith("/messages") and req.headers["x-api-key"] == "demo" and payload["model"] == "any-id"
            data = {"content":[{"type":"text","text":text}], "stop_reason":"end_turn"}
        elif protocol == "gemini":
            assert req.url.path.endswith("/models/any-id:generateContent") and req.headers["x-goog-api-key"] == "demo"
            data = {"candidates":[{"content":{"role":"model","parts":[{"text":text}]}, "finishReason":"STOP"}]}
        else:
            assert req.url.path.endswith("/responses") and req.headers["authorization"] == "Bearer demo"
            data = {"id":"resp_demo", "object":"response", "status":"completed", "model":"any-id", "output":[{
                "id":"msg_demo","type":"message","role":"assistant","status":"completed",
                "content":[{"type":"output_text","text":text,"annotations":[]}]}]}
        return httpx.Response(200, json=data)
    monkeypatch.setattr("gateway.personal_endpoint._EndpointTransport", lambda *_: httpx.MockTransport(respond))
    client = GatewayClient(load_config(store.path))
    try:
        model = client.create_generative_model("ocr")
        assert model.generate([TextInput("sample")], schema_name="read", result_type=Reading).text == "yes"
    finally:
        client.close()


def test_structured_visual_analysis_can_complete_a_tool_turn(yaml_model_settings, monkeypatch):
    from langchain_core.tools import tool
    @tool
    def read_scene() -> str:
        """Read the source scene description."""
        return "frame examined"

    c = register(yaml_model_settings, "analysis", "batch", [{"model_id":"custom/tools", "supports_vision":True}])
    yaml_model_settings.assign("vision", c.models[0].id, yaml_model_settings.catalog().revision)
    turns = []
    def respond(req):
        payload = json.loads(req.content)
        turns.append(payload)
        names = [t["function"]["name"] for t in payload["tools"]]
        assert "read_scene" in names and "Reading" in names
        name, args = ("read_scene", {}) if len(turns) == 1 else ("Reading", {"text":"frame examined"})
        message = {"role":"assistant", "content":None, "tool_calls":[
            {"id":"call" + str(len(turns)), "type":"function", "function":{"name":name, "arguments":json.dumps(args)}}]}
        return httpx.Response(200, json={"choices":[{"message":message, "finish_reason":"tool_calls"}]})
    monkeypatch.setattr("gateway.personal_endpoint._EndpointTransport", lambda *_: httpx.MockTransport(respond))
    client = GatewayClient(load_config(yaml_model_settings.path))
    try:
        model = client.create_generative_model("vision")
        result = model.generate([TextInput("Inspect the scene")], schema_name="reading", result_type=Reading,
                                tools=[read_scene], max_model_calls=3, tool_harness="deepagents")
        assert result.text == "frame examined" and len(turns) == 2
        assert any(m["role"] == "tool" and m["content"] == "frame examined" for m in turns[1]["messages"])
    finally:
        client.close()


def test_native_audio_and_ollama_connections_support_custom_models_and_credentials(yaml_model_settings, monkeypatch, tmp_path):
    store = yaml_model_settings
    audio = register(store, "Speech", "speech-key", [{"model_id":"local-whisper", "kind":"transcription"}], "openai_audio")
    vector = register(store, "Vectors", "embedding-key", [{"model_id":"local-embed", "kind":"embedding","dimensions":2}], "ollama_embedding")
    store.assign("transcription", audio.models[0].id, store.catalog().revision)
    store.assign("embedding", vector.models[0].id, store.catalog().revision)
    def reply(req):
        if req.url.path.endswith("/audio/transcriptions"):
            assert req.headers["authorization"] == "Bearer speech-key"
            return httpx.Response(200, json={"text":"speech"})
        assert req.url.path.endswith("/api/embed") and req.headers["authorization"] == "Bearer embedding-key"
        return httpx.Response(200,json={"model":"local-embed","embeddings":[[0,1]]})
    monkeypatch.setattr("gateway.personal_endpoint._EndpointTransport", lambda *_: httpx.MockTransport(reply))
    client=GatewayClient(load_config(store.path))
    try:
        sample=tmp_path/"sample.wav";sample.write_bytes(b"RIFFfixture")
        assert client.create_transcription_model().transcribe(sample)["text"]=="speech"
        assert client.create_embedding_model().embed_text("text").vector==[0,1]
    finally:
        client.close()
    for role, c in (("transcription",audio),("embedding",vector)):
        store.assign(role, None, store.catalog().revision)
        assert c.connection_id not in {row.connection_id for row in store.delete(c.connection_id, store.catalog().revision).connections}


def test_assigning_an_environment_selected_model_preserves_the_displayed_model_id(yaml_model_settings):
    store = ModelSettings(yaml_model_settings.path, allow_local=True, environ={
        **yaml_model_settings.environ, "ARK_API_KEY":"fake", "DEEPSEEK_API_KEY":"fake", "SEEKINO_VIDEO_MODEL":"env/visual",
    })
    base = store.catalog().settings()
    current = next(r for r in base.roles if r.role == "vision")
    selected = store.catalog().models[current.model_id]
    assert selected.option.model_id == "env/visual"
    store.assign("ocr", current.model_id, base.revision)
    assert load_config(store.path).resolve_model_binding("ocr", environ={}).model == "env/visual"


def test_yaml_source_without_models_is_visible_and_can_register_its_first_model(yaml_model_settings):
    store = yaml_model_settings
    original = yaml.safe_load(store.path.read_bytes())
    original["model_sources"]["speech-server"] = {
        "adapter":"openai_audio", "provider":"whisper.cpp", "base_url":"http://localhost:8001/v1", "api_key_env":None,
    }
    store.path.write_text(yaml.safe_dump(original))
    row = next(c for c in store.catalog().connections.values() if c.name == "speech-server")
    assert row.models == [] and row.model_kinds == ["transcription"]
    store.save(row.connection_id, PersonalModelConnectionSave(name=row.name, protocol=row.protocol, base_url=row.base_url,
               model_ids=["my-whisper"], models=[{"model_id":"my-whisper","kind":"transcription"}], revision=row.revision))
    option = store.catalog().connections[row.connection_id].models[0]
    assert option.roles == ["transcription"]
    store.assign("transcription", option.id, store.catalog().revision)
    assert load_config(store.path).resolve_model_binding("transcription").source == "speech-server"
