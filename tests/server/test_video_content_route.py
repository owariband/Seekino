from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent.video_parse_v2.content_route import ContentDecision, routing_evidence
from gateway.config import load_config
from gateway.multimodal import ImageInput
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_errors import VideoParseLeaseLostError
from server.service.video_parse_persistence import VideoParsePersistenceService
from server.service.video_parse_routing import ContentRoute, classify_video
from tests.server.test_upload_service import _create, _put, _upload_service


@pytest.fixture
def file_repository(tmp_path):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'routing.sqlite'}", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    try:
        yield engine, VideoParsePersistenceService(create_session_factory(engine))
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind,expected_fps,fail_first", [
    ("script", 3.0, False), ("course", 0.5, True), ("clip", 3.0, False),
])
def test_ordinary_upload_routes_before_episode_frames_and_reuses_committed_decision(
    file_repository, tmp_path, monkeypatch, kind, expected_fps, fail_first,
):
    engine, store, _, uploads = _upload_service(file_repository, tmp_path)
    # This matches the CPU deployment: the GPU transcription API supplies chunk ASR.
    uploads._parse_defaults = uploads.parse_defaults.model_copy(update={
        "asr": uploads.parse_defaults.asr.model_copy(update={"mode": "lite", "scope": "chunk"}),
    })
    upload = _create(uploads)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(uploads, upload.upload_session_id, number, data)
    completed = uploads.complete(upload.upload_session_id, tenant_id="local-default")
    factory = create_session_factory(engine)
    persistence = VideoParsePersistenceService(factory)
    task_id = completed.video_parse_task_id
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    lease = queue.claim_video("route", lease_duration=timedelta(minutes=5))
    assert lease.video_parse_task_id == task_id and lease.parse_config["auto_content_kind"]
    chunks = persistence.ensure_video_chunks(lease, [(0, 0, 12000)])
    persistence.ensure_parse_tasks(lease, [chunks[0].video_chunk_id], [(0, 1)])
    persistence.activate_video_task(lease)
    assert queue.claim_chunk("early", lease_duration=timedelta(minutes=2)) is None
    calls, effective = [], []
    config = load_config()

    @contextmanager
    def frames(_identity, *, timestamps_ms, **kwargs):
        assert 1 <= len(timestamps_ms) <= 3 and kwargs["max_height"] <= 360
        yield [(ms, tmp_path / f"{ms}.jpg") for ms in timestamps_ms]

    def generate(inputs, **kwargs):
        assert "Test Upload Series" in inputs[0].text
        assert any(isinstance(item, ImageInput) for item in inputs)
        assert kwargs["result_type"] is ContentDecision and kwargs["max_model_calls"] == 1
        calls.append("classify")
        if fail_first and len(calls) == 1:
            raise TimeoutError("router timeout")
        return ContentDecision(content_kind=kind)

    def episode(_identity, *, frame_fps, **kwargs):
        snapshot = persistence.load_task_input(task_id).task.parse_config
        effective.append((snapshot["content_kind"], snapshot["visual_input"]["fps"], frame_fps))
        return nullcontext()

    gateway = SimpleNamespace(config=config, create_generative_model=lambda _: SimpleNamespace(
        generate=generate, close=lambda: None))
    runtime = VideoParseRuntimeService(
        persistence, queue, store, SimpleNamespace(prepare_event_frames=frames, prepare_episode=episode),
        config, object(), worker_id="route", lease_duration=timedelta(minutes=5),
        heartbeat_interval=timedelta(seconds=10), gateway_client=gateway,
    )
    monkeypatch.setattr(runtime, "_transcription_model", lambda _: SimpleNamespace(input_mode="audio", close=lambda: None))

    async def load_harness_config(_):
        assert runtime._parse_input(persistence.load_task_input(task_id)).parse_config.content_kind == kind

    runtime.agent = SimpleNamespace(InvokeVideoParse=load_harness_config)
    with pytest.raises(VideoParseLeaseLostError):
        persistence.commit_content_route(replace(lease, lease_version=lease.lease_version + 1),
                                         ContentRoute("clip", "sampled_frames", "stale"))
    if fail_first:
        with pytest.raises(TimeoutError, match="router timeout"):
            asyncio.run(runtime.prepare(lease))
        assert "content_kind" not in persistence.load_task_input(task_id).task.parse_config
        assert not effective
        assert queue.claim_chunk("still-pending", lease_duration=timedelta(minutes=2)) is None
    asyncio.run(runtime.prepare(lease))
    asyncio.run(runtime.prepare(lease))
    assert calls == ["classify"] * (2 if fail_first else 1)
    assert effective == [(kind, expected_fps, expected_fps)] * 2
    state = persistence.load_task_input(task_id).task
    assert state.detail["_worker_runtime_v2"]["content_route"]["basis"] == "sampled_frames"
    assert persistence.commit_content_route(lease, ContentRoute("course", "transcript", "late")).content_kind == kind
    assert queue.claim_chunk("ready", lease_duration=timedelta(minutes=2)) is not None


def test_full_transcript_routes_using_bounded_evidence_without_frames():
    config = load_config()
    decision_model = config.resolve_model_binding("vision").model_copy(update={"model": "named-router"})
    fake_config = SimpleNamespace(query_hook=SimpleNamespace(enabled=True), normalized_models={"query_hook": decision_model},
                                  resolve_model_binding=lambda _: decision_model)
    seen = []

    def generate(inputs, **kwargs):
        seen.extend(inputs)
        return ContentDecision(content_kind="course")

    gateway = SimpleNamespace(config=fake_config, create_generative_model=lambda _: SimpleNamespace(generate=generate, close=lambda: None))
    transcript = [{"start_seconds": i * 1000, "text": "x" * 500} for i in range(20)]
    evidence = routing_evidence("/private/lecture.mp4", 20000, {"description": "a" * 1000, "secret": "omit"}, transcript)
    assert len(evidence["transcript"]) == 8
    assert evidence["transcript"][0]["start_ms"] == 0 and evidence["transcript"][-1]["start_ms"] == 19000
    assert evidence["filename"] == "lecture.mp4" and len(evidence["series_description"]) == 800
    result = classify_video(evidence, gateway=gateway, visual_model=None,
                            prepare_frames=lambda: pytest.fail("unexpected frame decode"), check_active=lambda: None)
    assert result == ContentRoute("course", "transcript", "named-router")
    assert "secret" not in seen[0].text and "lecture.mp4" in seen[0].text
