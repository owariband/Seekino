"""One local app, from two ordinary uploads to both original-media downloads."""

import hashlib
import json
import os
import re
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw, ImageStat
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from gateway.agent_model.deepagents import DeepAgentSessionStore
from gateway.embedding import EmbeddingResult
from gateway.model_factory import GatewayClient
from server.api.app import create_app
from server.config import ServerSettings
from server.database import create_database_engine, create_schema
from server.storage.elasticsearch import ElasticsearchVideoChunkStore
from tests.server.test_mcp_transport import _call, _rpc
from tests.server.test_video_parse_worker import _complete_refine_prompt


def _source_file(directory, name, *, reference):
    path = directory / name
    frames = []
    for n in range(40):
        frame = Image.new("RGB", (96, 64), "#601518" if reference else "#092142")
        drawing = ImageDraw.Draw(frame)
        if reference:
            half = (46, 32, 23, 14)[(n // 5) % 4]
            drawing.rectangle((48-half, 32-half, 48+half, 32+half), fill="#f6e4c9")
        else:
            drawing.rectangle((n+4, 20, n+24, 40), fill="white")
        frames.append(frame.tobytes())
    subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "96x64",
                    "-r", "20", "-i", "pipe:0", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   input=b"".join(frames), check=True)
    return path


def _model_response(inputs, *, schema_name, result_type, **kwargs):
    # Only inference is replaced. The notes use the source clock the real scanner sent.
    if schema_name == "video_parse_v2_chunk":
        return result_type.model_validate({"persons": [], "timeline": [{"event_type": "ACTION",
            "start_seconds": 250, "end_seconds": 1400, "description": "白色方块向右平移",
            "participants": [], "visible_persons": []}]})
    if schema_name == "IntervalRefinementResult":
        return _complete_refine_prompt(inputs[0].text)
    if schema_name == "technique_observation":
        prompt = inputs[0].text
        start, end = map(int, re.search(r"Source interval \[(\d+), (\d+)\)", prompt).groups())
        times = json.loads(re.search(r"Detector candidates in ms: (\[.*?\])", prompt)[1])
        assert times and len(inputs) > 1
        edges = [start, *times, end]
        return result_type.model_validate({"name": "方块逐级硬切", "summary": "方块在红底上突然改变大小",
            "role": "INTRO", "look": {"ground": "暗红", "subject_treatment": "浅色方块",
            "texture": None, "type_treatment": None}, "slots": [], "signals": ["大小跳变"],
            "motion": {"steps": [{"start_ms": a, "end_ms": b, "framing": "GRAPHIC", "subject": "方块",
                                 "enter": "CUT" if a != start else "NONE"} for a, b in zip(edges, edges[1:])],
                       "easing": None, "camera": "STATIC"}})
    if schema_name == "edit_reference_style_card":
        sources = json.loads(inputs[1].text)["observations"]
        members = {}
        for source in sources:
            note = source["observation"]
            members.setdefault(source["sequence_no"], []).append({
                "segment_id": source["segment_id"], "technique_key": "hard_cut",
                "name": note["name"], "summary": note["summary"], "role": note["role"], "camera": "STATIC"})
        example = {"segment_id": sources[0]["segment_id"], "at_ms": sources[0]["fine_starts_ms"][0],
                   "detail": "方块在此帧换了大小"}
        return result_type.model_validate({"members": [{"sequence_no": k, "observations": v} for k, v in members.items()],
            "style_card": {"scope": "方块图形", "signals": [
                {"name": "硬切", "description": "方块大小跳变", "examples": [example]}]}})
    raise AssertionError(f"unexpected model operation: {schema_name}")


def _embedding(gateway, binding):
    metadata = gateway.describe_embedding(binding)
    def vector(modality):
        return EmbeddingResult(model=metadata.model_name, modality=modality, vector=[1., 0.])
    return SimpleNamespace(provider_name=metadata.provider_name, encoder_model=metadata.encoder_model,
        encoder_revision=metadata.encoder_revision, dimensions=metadata.dimensions, close=lambda: None,
        embed_text=lambda value: vector("text"), embed_query=lambda value: vector("text"),
        embed_video=lambda value: vector("video"), embed_image=lambda value: vector("image"))


def _upload(client, series_id, episode, path, config):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    created = client.post("/api/uploads/create", params={"seriesId": series_id}, headers={"Idempotency-Key": digest},
        json={"tenantId": "local-default", "episodeOrder": episode, "originalFilename": path.name, "mediaType": "video/mp4",
              "totalSizeBytes": len(data), "contentSha256": digest, **config})
    assert created.status_code == 201, created.text
    scope = {"tenantId": "local-default", "uploadSessionId": created.json()["uploadSessionId"]}
    sent = client.put("/api/uploads/segments/upload", params={**scope, "segmentNumber": 0},
        headers={"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}", "X-Content-SHA256": digest}, content=data)
    assert sent.status_code == 200, sent.text
    completed = client.post("/api/uploads/complete", params=scope)
    assert completed.status_code == 200, completed.text
    job = completed.json()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get("/api/jobs/video-parse/get", params={"tenantId": "local-default",
                           "videoParseTaskId": job["videoParseTaskId"]}).json()
        if state["status"] == "COMPLETE":
            return job["videoId"]
        assert not state["status"].endswith("_FAILED"), state.get("lastError")
        time.sleep(.1)
    pytest.fail(f"{path.name} did not publish: {state}")


def _decoded_frames(path):
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                                   "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
    stride = 96 * 64 * 3
    assert len(raw) % stride == 0
    return [Image.frombytes("RGB", (96, 64), raw[i:i+stride]) for i in range(0, len(raw), stride)]


def _check_clip(client, url, target, original, *, start_ms, end_ms, disposition):
    assert url.startswith(str(client.base_url).rstrip("/") + "/api/videos/clip?")
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"].startswith(disposition)
    target.write_bytes(response.content)
    info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames",
        "-show_entries", "stream=codec_name,nb_read_frames:format=duration", "-of", "json", str(target)]))
    assert [item["codec_name"] for item in info["streams"]] == ["h264", "aac"]
    assert round(float(info["format"]["duration"]) * 1000) == end_ms - start_ms
    original_frames = _decoded_frames(original)[start_ms // 50:end_ms // 50]
    assert int(info["streams"][0]["nb_read_frames"]) == len(original_frames)
    for decoded, source in zip(_decoded_frames(target), original_frames, strict=True):
        # H.264 is re-encoded. Compare the picture on every corresponding source frame.
        assert max(ImageStat.Stat(ImageChops.difference(decoded, source)).mean) < 3
    return response.content


@pytest.fixture
def editing_app(tmp_path, monkeypatch):
    es_url = os.getenv("SEEKINO_TEST_ELASTICSEARCH_URL")
    if not es_url:
        pytest.skip("Set SEEKINO_TEST_ELASTICSEARCH_URL to the local test instance")
    if urlsplit(es_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("The editing acceptance test requires a loopback Elasticsearch test instance")
    mysql_url = os.getenv("SEEKINO_EDITING_TEST_DATABASE_URL")
    if mysql_url:
        url = make_url(mysql_url)
        if url.drivername != "mysql+pymysql" or url.host not in {"localhost", "127.0.0.1", "::1"}:
            pytest.fail("Supply a disposable loopback MySQL test database")
    api_key = os.getenv("SEEKINO_TEST_ELASTICSEARCH_API_KEY")
    for key in tuple(os.environ):
        if key.startswith("SEEKINO_"):
            monkeypatch.delenv(key)
    index = f"seekino_editing_test_{uuid4().hex}"
    settings_env = {"ENVIRONMENT": "test", "AUTH_MODE": "local", "ARTIFACT_ROOT": str(tmp_path / "media"),
        "DATABASE_URL": mysql_url or f"sqlite+pysqlite:///{tmp_path / 'editing_test.sqlite'}",
        "ELASTICSEARCH_URL": es_url, "ELASTICSEARCH_INDEX": index, "ELASTICSEARCH_EVENT_INDEX": index+"_events",
        "TEXT_VECTOR_DIMENSIONS": "2", "VISION_VECTOR_DIMENSIONS": "2", "SUMMARIZE_ENABLED": "false",
        "MQ_HEARTBEAT_SECONDS": "1", "MQ_LEASE_SECONDS": "30", "PARSE_CONCURRENCY": "1",
        "VIDEO_CONCURRENCY": "1", "REFINE_CONCURRENCY": "1", "INDEX_CONCURRENCY": "1"}
    if api_key:
        settings_env["ELASTICSEARCH_API_KEY"] = api_key
    for key, value in settings_env.items():
        monkeypatch.setenv("SEEKINO_" + key, value)
    for key in ("ARK_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY"):
        monkeypatch.setenv(key, "isolated-test")
    monkeypatch.setattr("server.bootstrap.create_agent_checkpoint_store", lambda *a, **kw: DeepAgentSessionStore())
    monkeypatch.setattr(GatewayClient, "create_generative_model",
                        lambda *a, **kw: SimpleNamespace(generate=_model_response, close=lambda: None))
    monkeypatch.setattr(GatewayClient, "create_embedding_model", _embedding)
    settings = ServerSettings.from_environment()
    engine = create_database_engine(settings.database_url, environment="test")
    try:
        if mysql_url and inspect(engine).get_table_names():
            pytest.fail("Use a fresh editing test database, as this test publishes two videos")
        create_schema(engine)
    finally:
        engine.dispose()

    with Elasticsearch(es_url, api_key=api_key, request_timeout=15) as es:
        chunk_store = ElasticsearchVideoChunkStore(es, index_name=index, environment="test",
                                                   semantic_dimensions=2, vision_dimensions=2)
        try:
            chunk_store.ensure_index()
            yield create_app(settings=settings)
        finally:
            for owned in (index, index + "_events", index + "_edit_reference"):
                es.indices.delete(index=owned, ignore_unavailable=True)


def test_one_mcp_from_upload_to_both_source_clips(tmp_path, editing_app):
    with TestClient(editing_app, base_url="http://127.0.0.1:9621", client=("127.0.0.1", 50000),
            headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}) as client:
        series = client.post("/api/series/create", json={"tenantId": "local-default", "name": "Editing test"})
        assert series.status_code == 201, series.text
        series_id = series.json()["seriesId"]
        config = editing_app.state.services.uploads.parse_defaults.to_video_parse_v2().model_dump(mode="json")
        config.update(content_kind="script", ocr_enabled=False, voiceprint_mode="off", voiceprint=None)
        config["models"]["asr"]["mode"] = "none"
        config["models"]["diarization"] = None
        config["execution"]["max_retry_count"] = 0
        performance = _source_file(tmp_path, "performance.mp4", reference=False)
        reference = _source_file(tmp_path, "reference.mp4", reference=True)
        content_id = _upload(client, series_id, 1, performance, {"videoParseConfig": config})
        reference_id = _upload(client, series_id, 2, reference, {"parseConfig": {"profile": "edit_reference"}})

        init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "editing-flow-test", "version": "1"}})
        assert init.json()["result"]["serverInfo"]["name"] == "seekino"
        tools = _rpc(client, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"list_series", "retrieve", "browse_events", "read_evidence",
                                              "find_edit_templates", "get_style_card", "get_edit_template", "list_edit_template_versions",
            "save_edit_template_version", "append_edit_template_note", "set_default_edit_template_version"}
        def call(name, **arguments):
            result = _call(client, name, arguments)
            assert not result.get("isError"), result
            return result["structuredContent"]
        group, = call("list_series")["items"]
        assert group["series_id"] == series_id and group["ready_video_count"] == 2
        found = call("retrieve", query="白色方块向右平移")
        assert not found["partial"], found["warnings"]
        event, = found["items"]
        browsed, = call("browse_events", series_id=series_id)["items"]
        assert event["video_id"] == content_id and browsed["evidence_ref"] == event["evidence_ref"]
        evidence = call("read_evidence", evidence_ref=event["evidence_ref"], start_ms=100, end_ms=850)
        _check_clip(client, evidence["media"]["clip_url"], tmp_path / "chosen.mp4", performance,
                    start_ms=100, end_ms=850, disposition="attachment")

        hit = call("find_edit_templates", query="方块逐级硬切")["techniques"][0]
        assert hit["video_id"] == reference_id
        card = call("get_style_card", video_id=hit["video_id"])
        assert card["style_card_format"] == "anchored_signals"
        example = card["style_card"]["signals"][0]["examples"][0]
        assert example["evidence_ref"] == hit["evidence_ref"]
        ref = call("read_evidence", evidence_ref=example["evidence_ref"])
        assert example["at_ms"] in ref["measured"]["fine_starts_ms"]
        assert ref["measured"]["frame_ms"] == list(range(hit["start_ms"], hit["end_ms"], 50))
        played = _check_clip(client, hit["media"]["play_url"], tmp_path / "exemplar.mp4", reference,
                             start_ms=hit["start_ms"], end_ms=hit["end_ms"], disposition="inline")
        assert client.get(ref["media"]["clip_url"]).content == played

        # Serve the same indexed fixture as a published Cloud library, then withdraw the series.
        from unittest.mock import Mock
        from server.api.app import build_app
        from server.config.auth import AuthConfig
        from server.config.sse import SSEConfig
        from server.model.tenant import Tenant
        from server.model.video import Series, SeriesVisibility
        from server.service.auth import AuthService
        from server.service.mcp_retrieval import McpRetrievalService

        services = editing_app.state.services
        local = services.mcp_retrieval
        sessions = services.auth.repository._session_factory
        tenant = "local-default"
        with sessions.begin() as session:
            publisher = session.get(Tenant, tenant)
            publisher.type, publisher.owner_id = "public", None
            session.get(Series, series_id).visibility = SeriesVisibility.PUBLIC
        cloud_auth = AuthService(services.auth.repository,
            AuthConfig(mode="cloud", public_tenant_id=tenant, local_tenant_id="unused-local", secret_key="editing-fixture" * 4),
            redis=Mock(eval=Mock(return_value=[1, 0])))
        reader = McpRetrievalService(library=services.library, media=services.media, event_retrieval=None,
            parse_persistence=local._persistence, reference_store=local._reference_store,
            editing_repository=local._editing, local_tenant_id=tenant, public_tenant_id=tenant)
        origin = "https://editing.example.com"
        app = build_app(SimpleNamespace(auth=cloud_auth, mcp_retrieval=reader, media=services.media),
                        settings=SimpleNamespace(auth=cloud_auth.config, allowed_origins=(origin,), sse=SSEConfig()))
        with TestClient(app, base_url=origin, headers={"Accept": "application/json, text/event-stream"}) as public:
            token = public.post("/api/auth/mcp-token").json()["accessToken"]
            public.headers["Authorization"] = "Bearer " + token
            public_card = _call(public, "get_style_card", {"video_id": reference_id})["structuredContent"]
            assert public_card["exemplars"][0]["evidence_ref"] == hit["evidence_ref"]
            public_hit = _call(public, "find_edit_templates", {"query": "方块逐级硬切"})["structuredContent"]["techniques"][0]
            url = public_hit["media"]["play_url"]
            assert public_hit["media"]["original_url"] is None and "ticket=" in url and token not in url
            del public.headers["Authorization"]
            assert _check_clip(public, url, tmp_path / "cloud-reference.mp4", reference,
                               start_ms=hit["start_ms"], end_ms=hit["end_ms"], disposition="inline") == played
            with sessions.begin() as session:
                session.get(Series, series_id).visibility = SeriesVisibility.PRIVATE
            assert public.get(url).status_code == 404
            public.headers["Authorization"] = "Bearer " + token
            assert _call(public, "get_style_card", {"video_id": reference_id})["isError"]
            assert _call(public, "read_evidence", {"evidence_ref": hit["evidence_ref"]})["isError"]
            assert _call(public, "find_edit_templates", {"query": "方块逐级硬切"})["structuredContent"]["techniques"] == []
