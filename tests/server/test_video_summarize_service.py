from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.video_summarize import (
    SummaryChunk, SummaryCompletion, SummaryEvidence, VideoSummarizeInput,
    VideoSummaryResult, VideoSummarySubmission,
)
from gateway.config import load_config
from gateway.media import ToolImage
from gateway.usage import TokenUsage, track_model_call
from server.config.video_summarize import VideoSummarizeConfig
from server.model.dto.jobDTO import ModelCallReservation
from server.service.parse_usage import ParseUsageService
from server.service.video_summarize import VideoSummarizeService
from server.utils.time import now_utc


class _Persistence:
    def __init__(self, path, config, model):
        self.lease = SimpleNamespace(
            video_parse_task_id=str(uuid4()), video_id=str(uuid4()), worker_id="worker",
            lease_version=1, retry_count=0, source_fingerprint="version-1",
            config_snapshot={"limits": asdict(config), "model": model.model_dump(mode="json")},
        )
        self.lease.task_input = SimpleNamespace(
            original_source_uri=str(path), series_id=str(uuid4()), tenant_id="test",
            task=SimpleNamespace(video_id=self.lease.video_id, detail={}),
        )
        self.inputs = VideoSummarizeInput(
            video_id=self.lease.video_id, duration_ms=10_000, episode_order=1,
            background={}, chunks=(SummaryChunk("chunk", 0, 1, {"timeline": []}),),
        )
        self.completed = []
        self.retries = []
        self.released = []
        self.calls = []
        self.retry_recorded = threading.Event()
        self.release_recorded = threading.Event()
        self.valid = True

    def claim_next_summary(self, worker_id, **kwargs):
        self.claim_options = kwargs
        return self.lease

    def load_summary_input(self, lease):
        return self.inputs

    def heartbeat_summary(self, lease, **kwargs):
        return self.valid

    def complete_summary(self, lease, result, **kwargs):
        self.completed.append((result, kwargs["cover_uri"]))
        return True

    def retry_summary(self, lease, **kwargs):
        self.retries.append(kwargs)
        self.retry_recorded.set()
        return True

    def release_summary(self, lease):
        self.released.append(lease)
        self.release_recorded.set()
        return True

    def reserve_model_call(self, lease, call_id, *, lease_duration):
        self.calls.append(call_id)
        return ModelCallReservation(
            lease.video_parse_task_id, call_id, lease.worker_id, True, 1, 100, 1,
            now_utc() + lease_duration,
        )

    def renew_model_call(self, *args, **kwargs):
        return self.valid

    def release_model_call(self, *args, **kwargs):
        return True


class _Model:
    def __init__(self):
        self.options = {}
        self.closed = False
        self.operation = lambda tracker: None

    def generate(self, inputs, **kwargs):
        self.options = kwargs
        with track_model_call(provider="fake", model="summary", operation="generate") as tracker:
            self.operation(tracker)
            tracker.usage = TokenUsage(10, 2, 12, None, None)
        return SummaryCompletion(completed=True)

    def close(self):
        self.closed = True


class _Artifacts:
    def __init__(self):
        self.published = []

    def resolve(self, uri):
        return Path(uri)

    def publish_video_artifact(self, path, **kwargs):
        self.published.append((path.read_bytes(), kwargs))
        return SimpleNamespace(uri="artifact://cover")


def _service(tmp_path, *, frozen_config=None, cover=None):
    gateway_config = load_config()
    binding = gateway_config.resolve_model_binding("vision").model_copy(update={"fallback": None})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"source")
    config = frozen_config or VideoSummarizeConfig()
    persistence = _Persistence(path, config, binding)
    model = _Model()
    artifacts = _Artifacts()
    usage = []
    service = VideoSummarizeService(
        persistence, artifacts, SimpleNamespace(create_generative_model=lambda *args, **kwargs: model),
        gateway_config=gateway_config, model=binding, config=VideoSummarizeConfig(),
        worker_id="worker", lease_duration=timedelta(seconds=2),
        heartbeat_interval=timedelta(seconds=1),
        parse_usage=ParseUsageService(SimpleNamespace(record_call=usage.append)),
    )
    result = VideoSummaryResult(VideoSummarySubmission(
        description="A concise episode description.", evidence=[SummaryEvidence(
            field="description", chunk_parse_task_id="chunk", quote="A scene.",
        )],
    ), cover_image=cover)

    async def invoke(inputs, **kwargs):
        service.received_limits = kwargs["limits"]
        await service.generate_video_summary(inputs, prompt="Summarize this video.", tools=[])
        return result

    service.agent = SimpleNamespace(InvokeSummarize=invoke)
    return service, persistence, model, artifacts, usage


@pytest.mark.parametrize("legacy_call_limits", [False, True])
def test_summary_uses_frozen_limits_records_usage_and_publishes_exact_cover(tmp_path, legacy_call_limits):
    data = b"frame supplied by the inspection tool"
    cover = ToolImage("data:image/png;base64," + base64.b64encode(data).decode(), len(data))
    config = VideoSummarizeConfig(maximum_read_chars=3000, execution_timeout_seconds=120)
    service, persistence, model, artifacts, usage = _service(tmp_path, frozen_config=config, cover=cover)
    if legacy_call_limits:
        persistence.lease.config_snapshot["limits"].update(
            max_model_calls=3, max_tool_calls=7, maximum_frame_inspections=2,
        )

    assert asyncio.run(service.run_next()) is True

    assert model.options.get("max_model_calls") is None
    assert model.options["request_timeout_seconds"] == 120
    assert service.received_limits.maximum_read_chars == 3000
    assert model.closed
    assert len(persistence.completed) == 1
    assert persistence.completed[0][1] == "artifact://cover"
    assert artifacts.published[0][0] == data
    assert artifacts.published[0][1]["category"] == "keyframes"
    assert len(usage) == len(persistence.calls) == 1
    assert usage[0].run_id == persistence.lease.video_parse_task_id
    assert usage[0].metadata["context"]["stage"] == "summarize"
    assert usage[0].task_type == "video_parse"


def test_summary_model_failure_retries_only_summary(tmp_path):
    service, persistence, model, _, _ = _service(tmp_path)

    def fail(_tracker):
        raise ValueError("bad model result")

    model.operation = fail
    assert asyncio.run(service.run_next()) is True
    assert not persistence.completed
    assert len(persistence.retries) == 1
    assert persistence.retries[0]["max_retries"] == 2
    assert persistence.retries[0]["error"] == "ValueError"
    assert model.closed


@pytest.mark.parametrize("timed_out", [False, True], ids=["shutdown", "timeout"])
def test_summary_cancellation_keeps_lease_until_provider_really_exits(tmp_path, timed_out):
    service, persistence, model, _, _ = _service(
        tmp_path, frozen_config=VideoSummarizeConfig(execution_timeout_seconds=0.08 if timed_out else 2),
    )
    started, cancelled, finish = (threading.Event() for _ in range(3))

    def block(tracker):
        tracker.add_cancel_callback(cancelled.set)
        started.set()
        assert finish.wait(3)

    model.operation = block

    async def scenario():
        run = asyncio.create_task(service.run_next())
        assert await asyncio.to_thread(started.wait, 2)
        if timed_out:
            assert await asyncio.wait_for(run, 1) is True
        else:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        assert cancelled.is_set()
        assert not persistence.completed and not persistence.retries and not persistence.released
        assert not model.closed
        finish.set()
        recorded = persistence.retry_recorded if timed_out else persistence.release_recorded
        assert await asyncio.to_thread(recorded.wait, 2)

    try:
        asyncio.run(scenario())
    finally:
        finish.set()
    assert model.closed
    if timed_out:
        assert persistence.retries[0]["error"] == "TimeoutError"
    else:
        assert not persistence.retries
        assert persistence.released == [persistence.lease]


def test_summary_source_lease_loss_does_not_publish_a_result(tmp_path):
    service, persistence, model, _, _ = _service(tmp_path)
    persistence.valid = False
    assert asyncio.run(service.run_next()) is True
    assert not persistence.completed
    assert not model.options


def test_summary_rejects_a_text_only_model(tmp_path):
    gateway_config = load_config()
    with pytest.raises(ValueError, match="vision-capable"):
        VideoSummarizeService(
            None, None, None, gateway_config=gateway_config,
            model=gateway_config.resolve_model_binding("vision").model_copy(update={"supports_vision_input": False}),
            config=VideoSummarizeConfig(), worker_id="worker", lease_duration=timedelta(seconds=2),
            heartbeat_interval=timedelta(seconds=1),
        )


def test_real_summary_agent_tools_host_and_sql_complete_offline(tmp_path, monkeypatch):
    from sqlalchemy import select

    from agent.video_summarize import VideoSummarizeAgent
    from server.constants.parse import RUNTIME_DETAIL_KEY
    from server.database import DatabaseEnvironment, create_database_engine, create_schema
    from server.model.job import ChunkParseContent, VideoParseTask
    from server.model.video import Video
    from server.repository.model_usage import ModelUsageRepository
    from server.storage.artifact import LocalArtifactStore
    from tests.server.test_video_parse_repository import _finish_ready_indexes
    from tests.server.test_video_summarize_persistence import AT, _ready

    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'summary-e2e.sqlite'}", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    try:
        factory, queue, persistence, task_id, video_lease = _ready((engine, None))
        _finish_ready_indexes(queue, persistence, now=AT)
        queue.release_execution(video_lease, now=AT)
        artifacts = LocalArtifactStore(tmp_path / "artifacts", environment=DatabaseEnvironment.TEST)
        source = artifacts.base_directory / "episode.mp4"
        source.write_bytes(b"offline source; this test does not request frames")
        expected_description = "A person waves toward the camera."
        with factory.begin() as session:
            task = session.get(VideoParseTask, task_id)
            video = session.get(Video, task.video_id)
            video.source_uri = artifacts.uri_for_path(source)
            video.hash = hashlib.sha256(source.read_bytes()).hexdigest()
            content = session.scalar(select(ChunkParseContent))
            content.content = {"persons": [], "timeline": [{
                "event_type": "ACTION", "start_seconds": 1000, "end_seconds": 4000,
                "description": expected_description, "participants": [], "visible_persons": [],
            }]}
            content.version += 1
            original_status = task.status
            assert original_status == "COMPLETE"
        monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: AT)

        class ToolModel(_Model):
            def generate(self, inputs, **options):
                tools = {tool.name: tool for tool in options["tools"]}
                assert set(tools) == {"get_context_canvas", "inspect_video_frames", "submit_video_summary"}
                with track_model_call(provider="fixture", model="summary", operation="generate"):
                    catalog = json.loads(tools["get_context_canvas"].invoke({"collection": "chunks"}))
                    references = [item["reference"] for item in catalog["items"]]
                    reviewed = json.loads(tools["get_context_canvas"].invoke({
                        "collection": "chunks", "references": references, "section": "content",
                    }))
                    assert not reviewed.get("truncated")
                    quote = json.loads(reviewed["items"][0]["content"])["timeline"][0]["description"]
                    receipt = json.loads(tools["submit_video_summary"].invoke({
                        "description": quote,
                        "evidence": [{"field": "description", "chunk_parse_task_id": references[0], "quote": quote}],
                    }))
                    assert receipt == {"submitted": True}
                return options["result_type"](completed=True)

        model = ToolModel()
        gateway_config = load_config()
        binding = gateway_config.resolve_model_binding("vision").model_copy(update={"fallback": None})
        service = VideoSummarizeService(
            persistence, artifacts, SimpleNamespace(create_generative_model=lambda *args, **kwargs: model),
            gateway_config=gateway_config, model=binding, config=VideoSummarizeConfig(),
            worker_id="e2e-summary", lease_duration=timedelta(minutes=5),
            heartbeat_interval=timedelta(seconds=30),
            parse_usage=ParseUsageService(ModelUsageRepository(factory)),
        )
        assert isinstance(service.agent, VideoSummarizeAgent)
        assert asyncio.run(service.run_next()) is True
        with factory() as session:
            task = session.get(VideoParseTask, task_id)
            video = session.get(Video, task.video_id)
            summary = task.detail[RUNTIME_DETAIL_KEY]["summarize"]
            assert summary["status"] == "COMPLETE"
            assert summary["result"]["description"] == expected_description
            assert video.description == expected_description
            assert video.display_name is None and video.cover_uri is None and video.episode_order == 1
            assert task.status == original_status
            assert len(task.detail[RUNTIME_DETAIL_KEY]["model_calls"]["used_call_ids"]) == 1
        assert model.closed
    finally:
        engine.dispose()
