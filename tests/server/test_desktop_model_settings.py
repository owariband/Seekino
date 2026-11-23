"""Desktop v2 model binding, credential scoping and provider HTTP contract."""
from contextlib import closing
import json
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
import yaml

from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput, VideoInput
from server.desktop.model_settings import EXAMPLE, LEGACY_KEYS, ModelProfile


def form(protocol="openai_chat", **changes):
    return {"name": "Studio", "protocol": protocol,
            "baseUrl": ARK_OPENAI_BASE_URL if protocol == "ark" else "http://localhost:7878/v1",
            "auth": "key", "vision": "test-vision", "transcription": "test-asr", "embedding": "test-embed",
            "dimensions": 64, "rerank": "test-rank", **changes}


def request(action, **args):
    from server.desktop.app import dispatch
    answer = json.loads(dispatch(json.dumps({"action": action, "args": args})))
    if "error" in answer:
        raise ValueError(answer["error"])
    return answer["value"]


@pytest.fixture(autouse=True)
def no_vendor_keys(monkeypatch):
    for name in LEGACY_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_single_service_no_defaults_or_global_secrets(tmp_path, monkeypatch):
    import os
    profile = ModelProfile.read(tmp_path)
    initial = profile.view({})
    assert initial["mode"] == "new" and initial["form"]["protocol"] == ""
    assert initial["connections"] == [] and not initial["importReady"] and not initial["searchReady"]
    assert not (tmp_path / "models.yaml").exists()
    first = profile.candidate(form(name=""))
    [slot] = first.view({})["credentialNames"]
    assert first.view({slot: "my-secret"})["importReady"]
    assert first.view({})["form"]["name"] == "localhost"
    ready = first.view({slot: "my-secret"})
    assert set(ready["credentialNames"]) == {slot}
    assert {role["role"] for role in ready["connections"][0]["roles"]} >= {"vision", "transcription", "embedding", "rerank"}
    first.persist()
    second = ModelProfile.read(tmp_path)
    assert second.form == first.form and second.revision == first.revision
    config, env = second.for_gateway({slot: "my-secret"})
    assert env == {slot: "my-secret"} and len(config.model_sources) == 1
    assert not os.environ.get(slot) and "my-secret" not in second.path.read_text()
    changed = second.candidate(form(baseUrl="http://localhost:8888/v1", name=""))
    assert changed.slots()[changed.source] != slot
    assert not changed.view(env)["ready"]
    assert changed.slots()[first.source] == slot  # a retry still has its original destination


def test_preserve_custom_bytes_scoped_secrets_and_role_readiness(tmp_path, monkeypatch):
    document = yaml.safe_load(EXAMPLE.read_text())
    old = document["model_sources"]["ark_default"]
    document["model_sources"]["legacy_alias"] = {**old, "api_key_env": "MY_STUDIO_TOKEN"}
    document["models"]["embedding"]["source"] = "legacy_alias"
    document["model_sources"]["deepseek_api"]["base_url"] = "https://replacement.example/v1"
    raw = b"# operator's notes\n" + yaml.safe_dump(document).encode()
    path = tmp_path / "models.yaml"
    path.write_bytes(raw)
    monkeypatch.setenv("MY_STUDIO_TOKEN", "operator-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-secret")
    profile = ModelProfile.read(tmp_path)
    ready = profile.view({"DEEPSEEK_API_KEY": "saved-old-secret"})
    assert ready["mode"] == "custom"
    assert ready["searchReady"] and not ready["importReady"]
    index_slot = profile.slots()["legacy_alias"]
    cfg, env = profile.for_gateway({})
    assert cfg.source("legacy_alias").api_key_env == index_slot and env[index_slot] == "operator-secret"
    assert "DEEPSEEK_API_KEY" not in env and not env[profile.slots()["deepseek_api"]]
    task = SimpleNamespace(status="INDEXING_FAILED", parse_config={"models": {"embedding": {"source": "legacy_alias"}}})
    assert profile.retry_ready(task, profile.ready_sources({}))
    task.status = "PARSING_FAILED"
    assert not profile.retry_ready(task, profile.ready_sources({}))
    assert profile.prepare({"retainConfig": True, "revision": ready["revision"]}) is profile
    with pytest.raises(ValueError, match="部署管理"):
        profile.prepare({"form": form(), "revision": ready["revision"]})
    with pytest.raises(ValueError, match="设置已变更"):
        profile.prepare({"retainConfig": True, "revision": "outdated"})
    assert path.read_bytes() == raw


def test_anonymous_only_for_explicit_loopback(tmp_path):
    profile = ModelProfile.read(tmp_path)
    prepared = profile.candidate(form(auth="none"))
    assert prepared.view({})["ready"] and not prepared.view({})["credentialNames"]
    for base in ("https://localhost.example/v1", "http://192.168.0.4/v1"):
        with pytest.raises(ValueError, match="本机"):
            profile.candidate(form(auth="none", baseUrl=base))
    with pytest.raises(ValueError, match="本机"):
        profile.candidate(form("ark", auth="none"))
    with pytest.raises(ValueError, match="底层模型"):
        profile.candidate(form("ark", embedding="ep-some-id"))


def test_apply_or_reject_without_losing_active_library(tmp_path, monkeypatch):
    from server.config.storage import desktop_storage
    from server.desktop import app
    pytest.importorskip("sqlite_vec")
    monkeypatch.setattr(app, "enforce_single_process", lambda: None)
    request("open", root=str(tmp_path))
    try:
        series = request("createSeries", name="QA")
        initial = request("modelSettings", root=str(tmp_path))
        selection = form(auth="none")
        args = dict(revision=initial["revision"], form=selection, root=str(tmp_path))
        assert request("prepareModelSettings", **args)["credentialName"] == ""
        saved = request("saveModelSettings", **args)
        assert saved["ready"] and request("modelSettings", root=str(tmp_path)) == saved
        before = (tmp_path / "models.yaml").read_bytes()
        with pytest.raises(ValueError, match="设置已变更"):
            request("saveModelSettings", **args)
        from server.desktop.database import open_desktop_database
        from tests.server.test_video_parse_queue import _task
        request("close")
        with closing(open_desktop_database(tmp_path / desktop_storage().database.file)) as db:
            _task(db.session_factory, chunk_count=1, accepted=False)
        request("open", root=str(tmp_path))
        for change in ({"dimensions": 8}, {"baseUrl": "http://localhost:9876/v1"}):
            for action in ("prepareModelSettings", "saveModelSettings"):
                with pytest.raises(ValueError, match="向量模型"):
                    request(action, root=str(tmp_path), revision=saved["revision"], form=form(auth="none", **change))
        assert (tmp_path / "models.yaml").read_bytes() == before
        assert any(item["id"] == series["id"] for item in request("seriesList"))
        def fail_write(*_args):
            raise OSError("模拟保存失败")
        monkeypatch.setattr(ModelProfile, "persist", fail_write)
        with pytest.raises(ValueError, match="模拟保存失败"):
            request("saveModelSettings", root=str(tmp_path), revision=saved["revision"], form=form(auth="none", vision="vision-two"))
        assert (tmp_path / "models.yaml").read_bytes() == before
        assert request("modelSettings", root=str(tmp_path)) == saved
        assert any(item["id"] == series["id"] for item in request("seriesList"))
    finally:
        if app._application:
            request("close")


def test_single_ark_key_uses_only_ark_sdk_routes(tmp_path, monkeypatch):
    profile = ModelProfile.read(tmp_path).candidate(form("ark"))
    slot = profile.slots()[profile.source]
    config, env = profile.for_gateway({slot: "only-ark-key"})
    calls = []

    def reject_socket(*_):
        raise AssertionError("real network disabled")
    monkeypatch.setattr(socket.socket, "connect", reject_socket)

    def reply(_transport, req):
        body = json.loads(req.content)
        assert req.url.host == "ark.cn-beijing.volces.com"
        assert req.headers["authorization"] == "Bearer only-ark-key"
        path = req.url.path.removeprefix("/api/v3/")
        model = body["model"]
        calls.append((path, model))
        if path == "embeddings/multimodal":
            assert body["dimensions"] == 64 and body["input"][0]["type"] == "text"
            return httpx.Response(200, json={"model": model, "data": {"embedding": [1.] + [0.] * 63}})
        assert path == "responses" and not body.get("stream")
        if model == "test-asr":
            assert body["input"][0]["content"][-1]["type"] == "input_video"
        text = {"type": "output_text", "text": '{"answer":"fixed"}'}
        output = [{"type": "message", "role": "assistant", "content": [text]}]
        return httpx.Response(200, json={"id": "offline", "status": "completed", "model": model, "output": output})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reply)
    class Answer(BaseModel):
        answer: str
    movie = tmp_path / "asr.mp4"
    movie.write_bytes(b"fixture")
    with closing(GatewayClient(config, environ=env, enable_supervisor=False)) as gateway:
        for role in ("vision", "rerank"):
            with closing(gateway.create_generative_model(role)) as model:
                assert model.generate([TextInput("fixture")], schema_name="probe", result_type=Answer).answer == "fixed"
        with closing(gateway.create_transcription_model()) as model:
            assert model.generate([TextInput("fixture"), VideoInput(movie, fps=1.0)], schema_name="probe", result_type=Answer).answer == "fixed"
        with closing(gateway.create_embedding_model()) as model:
            assert len(model.embed_query("fixture").vector) == 64
    assert calls == [("responses", "test-vision"), ("responses", "test-rank"), ("responses", "test-asr"),
                     ("embeddings/multimodal", "test-embed")]


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="requires a packaged desktop video encoder")
def test_ark_profile_import_to_original_at_sdk_http_boundary(tmp_path, monkeypatch):
    """Exercise the production parse, index and search graph with no paid service."""
    from time import monotonic, sleep
    from pathlib import Path
    from server.desktop import app
    from tests.server.test_desktop_pipeline import _fixture_movie

    pytest.importorskip("sqlite_vec")
    pytest.importorskip("av")
    monkeypatch.setattr(app, "enforce_single_process", lambda: None)
    media = tmp_path / "sample.mp4"
    _fixture_movie(media)
    root = str(tmp_path / "library")
    calls = []

    def reject_socket(*_):
        raise AssertionError("real network disabled")
    monkeypatch.setattr(socket.socket, "connect", reject_socket)

    def respond(_transport, req):
        data = json.loads(req.content)
        path = req.url.path.removeprefix("/api/v3/")
        assert req.url.host == "ark.cn-beijing.volces.com"
        assert req.headers["authorization"] == "Bearer one-ark-fixture"
        model = data["model"]
        if path == "embeddings/multimodal":
            kind = data["input"][0]["type"]
            calls.append((model, kind))
            assert model == "test-embed"
            vector = [1.] + [0.] * (data["dimensions"] - 1)
            return httpx.Response(200, json={"model": model, "data": {"embedding": vector}})
        assert path == "responses" and data.get("stream") is False
        schema = data["text"]["format"]["name"]
        calls.append((model, schema))
        if schema == "candidate_transcript":
            result = {"transcript": []}
        elif schema == "video_content_type":
            result = {"content_kind": "script"}
        elif schema == "video_parse_v2_chunk":
            result = {"persons": [], "timeline": [{
                "event_type": "ACTION", "start_seconds": 300, "end_seconds": 1200, "description": "红旗",
                "participants": [], "visible_persons": [],
            }]}
        elif schema == "IntervalRefinementResult":
            prompt = next(part["text"] for item in data["input"] for part in item["content"] if part["type"] == "input_text")
            members = json.loads(prompt.split("Input: ", 1)[1])["members"]
            result = {"members": [{"sequence_no": member["sequence_no"], **member["content"]} for member in members]}
        else:
            raise AssertionError(f"unexpected schema {schema}")
        output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(result)}]}]
        return httpx.Response(200, json={"id": "offline", "status": "completed", "model": model, "output": output})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)

    request("open", root=root)
    try:
        before = request("modelSettings", root=root)
        settings = dict(root=root, form=form("ark"), revision=before["revision"])
        plan = request("prepareModelSettings", **settings)
        after = request("saveModelSettings", **settings, keys={plan["credentialName"]: "one-ark-fixture"})
        assert len(after["credentialNames"]) == 1
        series = request("createSeries", name="颜色")
        imported = request("import", path=str(media), seriesID=series["id"])
        deadline = monotonic() + 30
        while True:
            job, = request("tasks")
            if job["status"] == "COMPLETE":
                break
            assert not job["error"] and monotonic() < deadline, (job["status"], job["error"], calls)
            sleep(0.05)
        for query, keyword in (("红旗", True), ("红色旗帜", False)):
            results = request("search", query=query, seriesIDs=[series["id"]], keywords=keyword)
            hit, = results["hits"]
            assert (hit["video_id"], hit["start_ms"], hit["end_ms"]) == (imported["videoID"], 300, 1200)
            playback = request("openResult", sourceRef=hit["source_ref"])
            assert Path(playback["path"]).read_bytes() == media.read_bytes()
        assert {("test-asr", "candidate_transcript"), ("test-vision", "video_parse_v2_chunk"),
                ("test-vision", "IntervalRefinementResult"), ("test-embed", "text"), ("test-embed", "video_url")} <= set(calls)
    finally:
        request("close")
