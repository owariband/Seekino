from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent.chat.realtime_understanding.state import ContextSummary, FrameCaption, RealtimeRequest, VisualObservation
from agent.video_parse_v2.input import FallbackModelConfig
from engine.parsev2.schemas import ActionEvent, VideoParseResult
from gateway.multimodal import ImageInput
from gateway.errors import ContentFilterError
from server.config.parse_profile import default_video_parse_v2_config
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.job import ChunkParseContent, ChunkParseTask, IntervalParseTask, VideoParseTask
from server.model.upload import UploadSession
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.upload import UploadConflictError, UploadRepository
from server.repository.video_parse import RUNTIME_DETAIL_KEY, VideoParseConflictError, VideoParseRepository, VideoParseTaskNotFoundError
from server.service.realtime_understanding import RealtimeUnderstandingService
from server.service.video_parse import VideoParseRuntimeService
from server.storage.artifact import LocalArtifactStore
from server.utils.time import now_utc


class _ParseRuntime:
    def __init__(self, repository):
        self.repository = repository
        self.base_calls = []
        self.derived_calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_derived = False
        self.commit_lock = threading.Lock()

    async def understand(self, lease):
        self.base_calls.append(lease.chunk_parse_task_id)
        self.started.set()
        await asyncio.to_thread(self.release.wait, 5)
        # SQLite ignores SELECT FOR UPDATE; serialize these test commits to
        # model MySQL's existing Interval row locks rather than lose ready bits.
        with self.commit_lock:
            self.repository.commit_chunk_result(lease, content=VideoParseResult(persons=[], timeline=[]).model_dump(mode="json"))

    async def generate_realtime(self, binding, inputs, *, result_type, **kwargs):
        self.derived_calls.append((result_type, inputs, binding, kwargs))
        await asyncio.sleep(0.03)
        if self.fail_derived:
            raise RuntimeError("fake model failure")
        if result_type is VisualObservation:
            assert isinstance(inputs[0], ImageInput) and Path(inputs[0].path).is_file()
            return VisualObservation(summary="投影中显示速度公式", captions=[FrameCaption(frame_index=0, caption="v=s/t")])
        return ContextSummary(summary="老师在解释速度公式。")


@pytest.fixture
def realtime(tmp_path, monkeypatch):
    # File-backed SQLite allows the request, base executor and other process
    # simulations to use separate sessions without in-memory connection races.
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'realtime.db'}", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    factory = create_session_factory(engine)
    repository = VideoParseRepository(factory)
    queue = MySqlVideoParseQueue(factory)
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id, video_id = str(uuid4()), str(uuid4())
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-source")
    artifact = artifacts.publish_video_artifact(source, series_id=series_id, video_id=video_id, category="chunks", filename="source.mp4")
    with factory.begin() as session:
        session.add(Series(series_id=series_id, tenant_id="mine", name="Realtime lesson", status="EMPTY", character_bible=[]))
        session.add(Video(
            video_id=video_id, series_id=series_id, episode_order=1, upload_sequence=1,
            original_filename="source.mp4", original_source_uri=artifact.uri,
            content_hash=artifact.sha256, total_bytes=artifact.size_bytes,
            duration_seconds=45_000, upload_status="COMPLETED", parse_status="QUEUED",
            upload_progress=100, parse_progress=0,
        ))
    config = default_video_parse_v2_config().model_copy(update={
        "chunk_duration": 10, "interval_duration": 30, "batch_max_size": 3,
        "dispatch_interval_seconds": 0.0,
    })
    runtime = _ParseRuntime(repository)
    service = RealtimeUnderstandingService(
        repository, queue, runtime, artifacts, gateway_config=SimpleNamespace(),
        default_parse_config=config, worker_id="realtime-test",
        lease_duration=timedelta(seconds=2), heartbeat_interval=timedelta(milliseconds=50),
    )
    extracted = []

    def extract(_source, timestamps, *, output_dir, max_height, timeout_seconds=None):
        assert timeout_seconds is not None and timeout_seconds > 0
        result = []
        for timestamp in timestamps:
            extracted.append(timestamp)
            path = Path(output_dir) / f"{timestamp}.jpg"
            path.write_bytes(f"frame:{timestamp}:{max_height}".encode())
            result.append((timestamp, path))
        return result

    monkeypatch.setattr("server.service.realtime_understanding.extract_video_frames", extract)
    value = SimpleNamespace(
        service=service, repository=repository, queue=queue, artifacts=artifacts,
        factory=factory, runtime=runtime, config=config, video_id=video_id,
        series_id=series_id, extracted=extracted,
    )
    yield value
    runtime.release.set()
    service.close()
    engine.dispose()


def _request(realtime, anchor=25_000, *, seconds=5, question="这个公式是什么意思？", tenant="mine"):
    return RealtimeRequest(tenant, realtime.video_id, anchor, question, time.monotonic() + seconds, lambda: None)


def _context(realtime, anchor=25_000):
    return asyncio.run(realtime.service.prepare_context(_request(realtime, anchor)))


def _upload(realtime, task_id):
    upload_id = str(uuid4())
    with realtime.factory.begin() as session:
        session.add(UploadSession(
            upload_session_id=upload_id, tenant_id="mine", series_id=realtime.series_id,
            episode_order=1, upload_sequence=1, original_filename="source.mp4",
            media_type="video/mp4", total_size_bytes=11, preferred_segment_size_bytes=11,
            total_segments=1, version=1, status="COMPLETED", idempotency_key=upload_id,
            expires_at=now_utc() + timedelta(hours=1), completed_at=now_utc(),
            video_id=realtime.video_id, initial_video_parse_task_id=task_id,
        ))
    return upload_id


@pytest.mark.parametrize(("anchor", "sequences"), [(0, [0, 1]), (25_000, [1, 2, 3]), (45_000, [3, 4])])
def test_topology_selects_fixed_neighbors_and_does_not_start_refine(realtime, anchor, sequences):
    context = _context(realtime, anchor)
    assert [item.sequence_no for item in context.targets] == sequences
    with realtime.factory() as session:
        task = session.get(VideoParseTask, context.video_parse_task_id)
        assert task.status == "PENDING" and task.detail[RUNTIME_DETAIL_KEY]["accepted"] is False
        assert len(list(session.scalars(select(ChunkParseTask)))) == 5
        assert len(list(session.scalars(select(IntervalParseTask)))) == 3
    assert realtime.queue.claim_video("full", lease_duration=timedelta(minutes=1)) is None
    assert realtime.queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1)) is None


def test_scope_and_invalid_position_do_not_leak_video(realtime):
    with pytest.raises(VideoParseTaskNotFoundError):
        asyncio.run(realtime.service.prepare_context(_request(realtime, tenant="another")))
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(realtime.service.prepare_context(_request(realtime, 45_001)))


def test_base_work_is_shared_and_persists_after_waiter_cancellation(realtime):
    context = _context(realtime)
    target = context.targets[1]
    realtime.runtime.release.clear()

    async def run():
        first = asyncio.create_task(realtime.service.ensure_chunk(context, target))
        while not realtime.runtime.started.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.create_task(realtime.service.ensure_chunk(context, target))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0.15)  # shared lease heartbeats outlive the waiter
        realtime.runtime.release.set()
        evidence = await second
        cached = await realtime.service.ensure_chunk(context, target)
        assert evidence.result == cached.result and evidence.content_version == 1

    asyncio.run(run())
    assert realtime.runtime.base_calls == [target.chunk_parse_task_id]
    assert realtime.queue.claim_realtime_chunk(target.chunk_parse_task_id, "other", lease_duration=timedelta(seconds=2)) is None


def test_base_deadline_does_not_cancel_persistent_job(realtime):
    context = _context(realtime)
    context = replace(context, request=_request(realtime, seconds=0.04))
    target = context.targets[1]
    realtime.runtime.release.clear()
    with pytest.raises(TimeoutError):
        asyncio.run(realtime.service.ensure_chunk(context, target))
    realtime.runtime.release.set()
    evidence = asyncio.run(realtime.service.ensure_chunk(replace(context, request=_request(realtime)), target))
    assert evidence.content_version == 1 and realtime.runtime.base_calls == [target.chunk_parse_task_id]


def test_running_claim_and_batch_capacity_are_shared_across_workers(realtime):
    context = _context(realtime)
    leases = [realtime.queue.claim_realtime_chunk(target.chunk_parse_task_id, f"worker-{index}", lease_duration=timedelta(seconds=2))
              for index, target in enumerate(context.targets)]
    assert all(leases)
    assert realtime.queue.claim_realtime_chunk(context.targets[0].chunk_parse_task_id, "duplicate", lease_duration=timedelta(seconds=2)) is None
    rows = realtime.repository.load_realtime_chunks(context.video_parse_task_id, tenant_id="mine", video_id=realtime.video_id, task_version=1)
    assert realtime.queue.claim_realtime_chunk(rows[0].chunk_parse_task_id, "capacity", lease_duration=timedelta(seconds=2)) is None


def test_failed_on_demand_chunk_does_not_block_another_missing_chunk(realtime):
    context = _context(realtime)
    lease = realtime.queue.claim_realtime_chunk(context.targets[0].chunk_parse_task_id, "failing", lease_duration=timedelta(seconds=2))
    # Simulate the last allowed retry with the same true fencing token.
    lease = replace(lease, max_retry_count=0)
    assert realtime.queue.retry_chunk(lease, error="recognition failed", retry_at=now_utc())
    with realtime.factory() as session:
        parent = session.get(VideoParseTask, context.video_parse_task_id)
        assert parent.status == "PENDING"
        assert "failure_status" not in parent.detail[RUNTIME_DETAIL_KEY]
        assert session.get(ChunkParseTask, lease.chunk_parse_task_id).status == "FAILED"
    assert realtime.queue.claim_realtime_chunk(context.targets[1].chunk_parse_task_id, "next", lease_duration=timedelta(seconds=2)) is not None


@pytest.mark.parametrize("parent_status", ["PARSING", "PARSING_FAILED"])
def test_failed_full_parse_returns_completed_evidence_and_missing_targets_promptly(realtime, parent_status):
    context = _context(realtime)
    target = context.targets[0]
    asyncio.run(realtime.service.ensure_chunk(context, target))
    with realtime.factory.begin() as session:
        task = session.get(VideoParseTask, context.video_parse_task_id)
        task.status = parent_status
        task.detail = {**task.detail, RUNTIME_DETAIL_KEY: {
            **task.detail[RUNTIME_DETAIL_KEY], "accepted": True,
            "realtime_only": False, "failure_status": "PARSING_FAILED",
        }}
    recovered = _context(realtime)
    async def gather():
        return await asyncio.gather(*(realtime.service.ensure_chunk(recovered, item) for item in recovered.targets))
    started = time.monotonic()
    evidence = asyncio.run(gather())
    assert time.monotonic() - started < 1
    assert evidence[0].result is not None
    assert all("requires retry" in item.error for item in evidence[1:])
    assert realtime.runtime.base_calls == [target.chunk_parse_task_id]


def test_full_parse_promotion_reuses_identity_content_and_refine_ready_mask(realtime):
    context = _context(realtime)
    async def complete():
        return await asyncio.gather(*(realtime.service.ensure_chunk(context, target) for target in context.targets))
    assert all(item.content_version == 1 for item in asyncio.run(complete()))
    assert realtime.queue.claim_interval_refine("refine", lease_duration=timedelta(seconds=2)) is None
    upload_id = _upload(realtime, context.video_parse_task_id)
    promoted = UploadRepository(realtime.factory).reparse(upload_id, tenant_id="mine", expected_job_version=1, now=now_utc(), execution_mode="v2")
    assert promoted.video_parse_task_id == context.video_parse_task_id and promoted.version == 1
    lease = realtime.queue.claim_video("full", lease_duration=timedelta(seconds=2))
    assert lease is not None
    chunks = realtime.repository.load_realtime_chunks(context.video_parse_task_id, tenant_id="mine", video_id=realtime.video_id, task_version=1)
    prepared = realtime.repository.ensure_prepared(lease, [item.video_chunk_id for item in chunks])
    assert len(prepared.chunk_parse_task_ids) == 5
    refinement = realtime.queue.claim_interval_refine("refine", lease_duration=timedelta(seconds=2))
    assert refinement is not None and refinement.start_sequence_no == 1
    with realtime.factory() as session:
        assert len(list(session.scalars(select(ChunkParseContent)))) == 3


def test_promotion_refuses_config_changes_and_recovers_failed_on_demand_children(realtime):
    context = _context(realtime)
    target = context.targets[1]
    asyncio.run(realtime.service.ensure_chunk(context, context.targets[0]))
    with realtime.factory.begin() as session:
        child = session.get(ChunkParseTask, target.chunk_parse_task_id)
        child.status = "FAILED"
        child.retry_count = 99
        task = session.get(VideoParseTask, context.video_parse_task_id)
        task.status = "PARSING_FAILED"
        task.detail = {**task.detail, RUNTIME_DETAIL_KEY: {**task.detail[RUNTIME_DETAIL_KEY], "failure_status": "PARSING_FAILED"}}
    upload_id = _upload(realtime, context.video_parse_task_id)
    upload = UploadRepository(realtime.factory)
    changed = realtime.config.model_copy(update={"understanding_enabled": False}).model_dump(mode="json")
    with pytest.raises(UploadConflictError, match="freezes"):
        upload.reparse(upload_id, tenant_id="mine", expected_job_version=1, now=now_utc(), execution_mode="v2", video_parse_config=changed)
    promoted = upload.reparse(upload_id, tenant_id="mine", expected_job_version=1, now=now_utc(), execution_mode="v2")
    assert promoted.video_parse_task_id == context.video_parse_task_id and promoted.status == "PENDING"
    with realtime.factory() as session:
        assert session.get(ChunkParseTask, target.chunk_parse_task_id).status == "QUEUED"
        assert session.get(ChunkParseTask, target.chunk_parse_task_id).retry_count == 0
        assert len(list(session.scalars(select(ChunkParseContent)))) == 1


def test_observation_uses_durable_exact_frame_and_singleflight_cache(realtime):
    context = _context(realtime)
    async def observe():
        return await asyncio.gather(*(realtime.service.observe_frames(context, prompt="解释当前图示") for _ in range(4)))
    observations = asyncio.run(observe())
    assert len(realtime.runtime.derived_calls) == 1
    frame = observations[0].frames[0]
    assert frame.timestamp_ms == 25_000 and frame.caption == "v=s/t"
    assert realtime.artifacts.resolve(frame.artifact_uri).is_file()
    assert frame.image_path.is_file() and "realtime" in frame.image_path.name
    asyncio.run(realtime.service.observe_frames(context, prompt="解释当前图示"))
    assert len(realtime.runtime.derived_calls) == 1
    changed = replace(context, request=_request(realtime, question="鼠标指的是什么？"))
    asyncio.run(realtime.service.observe_frames(changed, prompt="解释当前图示"))
    assert len(realtime.runtime.derived_calls) == 2


def test_composition_cache_fences_content_version_prompt_model_and_task(realtime):
    context = _context(realtime)
    asyncio.run(realtime.service.ensure_chunk(context, context.targets[1]))
    async def compose():
        return await asyncio.gather(*(realtime.service.compose_context(context, prompt="current evidence") for _ in range(3)))
    asyncio.run(compose())
    assert len(realtime.runtime.derived_calls) == 1
    with realtime.factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        content.version += 1
    asyncio.run(realtime.service.compose_context(context, prompt="current evidence"))
    asyncio.run(realtime.service.compose_context(context, prompt="changed prompt"))
    assert len(realtime.runtime.derived_calls) == 3
    with realtime.factory.begin() as session:
        task = session.get(VideoParseTask, context.video_parse_task_id)
        config = dict(task.parse_config)
        models = dict(config["models"])
        models["understanding"] = {**models["understanding"], "model": "another-model"}
        task.parse_config = {**config, "models": models}
    asyncio.run(realtime.service.compose_context(context, prompt="changed prompt"))
    assert len(realtime.runtime.derived_calls) == 4
    with realtime.factory.begin() as session:
        session.get(VideoParseTask, context.video_parse_task_id).is_deleted = True
    with pytest.raises(VideoParseConflictError):
        asyncio.run(realtime.service.compose_context(context, prompt="changed prompt"))


def test_generic_visual_events_still_require_exact_frame_observation(realtime):
    context = _context(realtime)
    async def complete():
        await asyncio.gather(*(realtime.service.ensure_chunk(context, target) for target in context.targets))
    asyncio.run(complete())
    target = context.targets[1]
    with realtime.factory.begin() as session:
        content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_parse_task_id == target.chunk_parse_task_id))
        content.content = VideoParseResult(persons=[], timeline=[ActionEvent(
            event_type="ACTION", start_seconds=24_000, end_seconds=26_000,
            description="老师指着投影上的公式", participants=[], visible_persons=[],
        )]).model_dump(mode="json")
    visual = asyncio.run(realtime.service.observe_frames(context, prompt="observe"))
    assert visual.frames[0].timestamp_ms == 25_000 and visual.frames[0].image_path.is_file()
    assert visual.frames[0].caption == "v=s/t" and visual.summary
    assert len(realtime.runtime.derived_calls) == 1


def test_expired_preparation_recovers_even_when_children_are_already_committed(realtime):
    context = _context(realtime)
    with realtime.factory.begin() as session:
        task = session.get(VideoParseTask, context.video_parse_task_id)
        task.status = "PREPARING"
        task.lease_owner = "crashed-topology-worker"
        task.lease_version += 1
        task.lease_expire_time = now_utc() - timedelta(seconds=1)
        task.heartbeat_time = now_utc() - timedelta(seconds=2)
    recovered = _context(realtime)
    assert recovered.video_parse_task_id == context.video_parse_task_id
    with realtime.factory() as session:
        task = session.get(VideoParseTask, context.video_parse_task_id)
        assert task.status == "PENDING" and task.lease_owner is None
        assert len(list(session.scalars(select(ChunkParseTask)))) == 5
    assert asyncio.run(realtime.service.ensure_chunk(recovered, recovered.targets[1])).content_version == 1


def test_derived_failures_do_not_enter_cache_and_different_event_loops_share_success(realtime):
    context = _context(realtime)
    realtime.runtime.fail_derived = True
    with pytest.raises(RuntimeError, match="fake model failure"):
        asyncio.run(realtime.service.compose_context(context, prompt="evidence"))
    realtime.runtime.fail_derived = False
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: asyncio.run(realtime.service.compose_context(context, prompt="evidence")), range(3)))
    assert all(result.summary for result in results)
    assert len(realtime.runtime.derived_calls) == 2


def test_realtime_gateway_call_bounds_primary_and_fallback_by_remaining_deadline():
    runtime = VideoParseRuntimeService.__new__(VideoParseRuntimeService)
    primary = default_video_parse_v2_config().models.understanding
    binding = primary.model_copy(update={"fallback": FallbackModelConfig(
        source=primary.source, model="fallback-model", request_timeout_seconds=30.0,
    )})
    calls = []
    def invoke(selected, _inputs, **_kwargs):
        calls.append(selected.request_timeout_seconds)
        if len(calls) == 1:
            time.sleep(0.03)
            raise ContentFilterError("blocked primary")
        return ContextSummary(summary="valid result")
    runtime._invoke_multimodal = invoke
    result = asyncio.run(runtime.generate_realtime(binding, [], schema_name="summary", result_type=ContextSummary,
                                                  deadline=time.monotonic() + 0.2, check_control=lambda: None))
    assert result.summary == "valid result" and 0 < calls[1] < calls[0] <= 0.2
