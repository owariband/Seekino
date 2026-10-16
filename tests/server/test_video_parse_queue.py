from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
import logging
import time
from uuid import uuid4

import pytest
import httpx
from arkruntime._exceptions import ArkAPIError
from gateway.agent_model.errors import ModelFailureReason, QueryModelIncompleteError
from gateway.errors import SSEProxyError

from tests.server.test_video_parse_repository import _refinement_inputs

from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.dto.jobDTO import ChunkParseLease, IntervalParseLease, VideoParseLease, VideoParseRetryResult
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseTask,
    VideoParseTask,
)
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
    _failure_log_text,
    _log_retry_failure,
)
from server.repository.video_parse import VideoParseLeaseLostError, VideoParseRepository
from server.utils.time import datetime_to_unix_time_ms


NOW = datetime(2026, 9, 29, 4, 0, tzinfo=UTC)


def _config(*, batch_size: int) -> dict:
    value = default_video_parse_v2_config().model_dump(mode="json")
    value.update({
        "chunk_duration": 10,
        "interval_duration": 30,
        "batch_max_size": batch_size,
        "dispatch_interval_seconds": 0.0,
    })
    return value


def _task(factory, *, chunk_count: int, accepted: bool, batch_size: int = 10):
    series_id, video_id, task_id = str(uuid4()), str(uuid4()), str(uuid4())
    with factory.begin() as session:
        session.add(Series(
            id=series_id, tenant_id="mine", name=f"Series {series_id}",
            description=None, status="INDEXING", important_extra_data=None,
            character_bible=[],
        ))
        session.flush()
        session.add(Video(
            id=video_id, series_id=series_id, episode_order=1,
            upload_sequence=1, filename="episode.mp4",
            source_uri="artifact://test/episode.mp4",
            hash=(video_id.replace("-", "") * 2)[:64], size_bytes=10,
            duration_ms=chunk_count * 10000, upload_status="COMPLETED",
            parse_status="QUEUED", upload_progress=100, parse_progress=0,
        ))
        session.flush()
        session.add(VideoParseTask(
            id=task_id, video_id=video_id, pipeline_version="v2", status="PENDING",
            parse_config=_config(batch_size=batch_size), chunk_count=chunk_count,
            version=1, next_execute_time=NOW, attempt_count=0, max_attempts=3,
            lease_version=0, processing_duration_ms=0,
            processing_timing_complete=True, is_test=True, is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": accepted}},
            create_time=datetime_to_unix_time_ms(NOW), update_time=datetime_to_unix_time_ms(NOW),
        ))
    return task_id


def _prepare(factory, queue, task_id: str, *, chunk_count: int):
    repo = VideoParseRepository(factory)
    lease = queue.claim_video(
        "main", lease_duration=timedelta(minutes=20), now=NOW
    )
    assert lease is not None and lease.video_parse_task_id == task_id
    chunks = repo.ensure_video_chunks(
        lease,
        [(i, i * 10000, (i + 1) * 10000) for i in range(chunk_count)],
        now=NOW,
    )
    specs = ((0, chunk_count),) if chunk_count < 3 else tuple(
        (start, 3) for start in range(chunk_count - 2)
    )
    prepared = repo.ensure_parse_tasks(
        lease, [item.video_chunk_id for item in chunks], specs, now=NOW
    )
    repo.activate_video_task(lease, now=NOW)
    return repo, lease, prepared


def test_video_claim_requires_explicit_v2_acceptance(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    legacy_id = _task(factory, chunk_count=1, accepted=False)
    accepted_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)

    lease = queue.claim_video(
        "worker", lease_duration=timedelta(minutes=1), now=NOW
    )
    assert lease is not None and lease.video_parse_task_id == accepted_id
    with factory() as session:
        assert session.get(VideoParseTask, legacy_id).status == "PENDING"


def test_claim_enforces_ninety_percent_batch_gate_without_dispatch(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=12, accepted=True, batch_size=10)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=12)

    for _ in range(8):
        child = queue.claim_chunk("understand", lease_duration=timedelta(minutes=1), now=NOW)
        _repo.commit_chunk_result(child, content={"persons": [], "timeline": []}, now=NOW)
    with factory.begin() as session:
        for child_id in prepared.chunk_parse_task_ids[8:10]:
            session.get(ChunkParseTask, child_id).next_execute_time = NOW + timedelta(minutes=1)
    assert queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[8]).next_execute_time = NOW
    ninth = queue.claim_chunk("ninth", lease_duration=timedelta(minutes=1), now=NOW)
    _repo.commit_chunk_result(ninth, content={"persons": [], "timeline": []}, now=NOW)
    claimed = queue.claim_chunk("chunk-worker", lease_duration=timedelta(minutes=1), now=NOW)
    assert claimed is not None and claimed.chunk_parse_task_id == prepared.chunk_parse_task_ids[10]
    with factory() as session:
        assert all(session.get(ChunkParseTask, item).phase == 2 for item in prepared.chunk_parse_task_ids[:9])


def test_claims_share_active_chunk_capacity(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=4, accepted=True, batch_size=4)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.parse_config = {**parent.parse_config, "execution": {**parent.parse_config["execution"], "parse_concurrency": 2}}
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=4)

    first = queue.claim_chunk(
        "worker-0", lease_duration=timedelta(minutes=1), now=NOW
    )
    second = queue.claim_chunk(
        "worker-1", lease_duration=timedelta(minutes=1), now=NOW
    )
    assert first is not None and second is not None
    assert queue.claim_chunk(
        "worker-2", lease_duration=timedelta(minutes=1), now=NOW
    ) is None


@pytest.mark.parametrize("legacy_marker", [None, False, True])
def test_queued_chunk_claim_ignores_legacy_dispatch_markers(repository, legacy_marker):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    if legacy_marker is not None:
        with factory.begin() as session:
            child = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0])
            child.detail = {**(child.detail or {}), "dispatch_requested": legacy_marker}
    claimed = queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW)
    assert claimed is not None
    assert claimed.chunk_parse_task_id == prepared.chunk_parse_task_ids[0]


def test_chunk_claim_interval_and_retry_backoff_are_independent(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=2, accepted=True, batch_size=2)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=2)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.parse_config = {**parent.parse_config, "dispatch_interval_seconds": 5}
    first = queue.claim_chunk("first", lease_duration=timedelta(minutes=1), now=NOW)
    assert first is not None
    assert queue.claim_chunk("early", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=4)) is None
    second = queue.claim_chunk("second", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=5))
    assert second is not None and second.chunk_parse_task_id == prepared.chunk_parse_task_ids[1]
    retry_at = NOW + timedelta(seconds=20)
    assert queue.retry_chunk(first, error="transient", retry_at=retry_at, now=NOW + timedelta(seconds=6))
    assert queue.claim_chunk("early-retry", lease_duration=timedelta(minutes=1), now=retry_at - timedelta(seconds=1)) is None
    retried = queue.claim_chunk("retry", lease_duration=timedelta(minutes=1), now=retry_at)
    assert retried is not None and retried.chunk_parse_task_id == first.chunk_parse_task_id
    assert retried.lease_version > first.lease_version
    assert retried.retry_count == 1


def test_video_lease_survives_activation_and_can_be_reclaimed_after_expiry(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, lease, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING"
        assert (task.lease_owner, task.lease_version) == ("main", lease.lease_version)
    assert queue.heartbeat_video(
        lease, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=1)
    )
    assert queue.reap_expired(
         now=NOW + timedelta(minutes=2)
    ) == 1
    replacement = queue.claim_video(
        "replacement", lease_duration=timedelta(minutes=1), now=NOW + timedelta(minutes=2)
    )
    assert replacement is not None
    assert replacement.video_parse_task_id == task_id
    assert replacement.lease_version > lease.lease_version


def test_reaper_limits_are_independent_across_video_and_chunk_families(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, video_lease, prepared = _prepare(
        factory, queue, task_id, chunk_count=1
    )
    chunk_lease = queue.claim_chunk(
        "chunk", lease_duration=timedelta(seconds=5), now=NOW
    )
    assert chunk_lease is not None
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.lease_expire_time = NOW + timedelta(seconds=5)

    # Each family receives its own limit, so a due coordinator cannot starve
    # an expired child when the configured batch size is one.
    assert queue.reap_expired(

        now=NOW + timedelta(seconds=6),
        limit=1,
    ) == 2
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        child = session.get(
            ChunkParseTask, chunk_lease.chunk_parse_task_id
        )
        assert parent.lease_owner is None
        assert child.status == "QUEUED"
        assert child.retry_count == 1
    resumed = queue.claim_chunk(
        "replacement", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=6 + chunk_lease.retry_initial_delay_seconds),
    )
    assert resumed is not None and resumed.chunk_parse_task_id == chunk_lease.chunk_parse_task_id
    assert resumed.lease_version > chunk_lease.lease_version


@pytest.mark.parametrize("family", ["video", "chunk", "interval"])
def test_reaper_skips_deleted_tasks_before_applying_its_limit(repository, family):
    engine, _unused = repository
    factory = create_session_factory(engine)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    model, identity = {
        "video": (VideoParseTask, "video_parse_task_id"),
        "chunk": (ChunkParseTask, "chunk_parse_task_id"),
        "interval": (IntervalParseTask, "interval_parse_task_id"),
    }[family]
    task_ids = []
    for index in range(2):
        task_id = _task(factory, chunk_count=1, accepted=True)
        repo, parent, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
        lease = parent
        if family != "video":
            lease = queue.claim_chunk(
                "child", video_parse_task_id=task_id,
                lease_duration=timedelta(minutes=1), now=NOW,
            )
            if family == "interval":
                repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
                lease = queue.claim_interval_refine(
                    "refine", video_parse_task_id=task_id,
                    lease_duration=timedelta(minutes=1), now=NOW,
                )
        task_ids.append(getattr(lease, identity))
        with factory.begin() as session:
            task = session.get(VideoParseTask, task_id)
            task.is_deleted = index == 0
            if task.is_deleted:
                task.capacity_acquired = False
            child = session.get(model, task_ids[-1])
            child.lease_expire_time = NOW + timedelta(seconds=index + 1)
    reaper = getattr(queue, f"reap_expired_{'videos' if family == 'video' else family + 's'}")
    assert reaper(now=NOW + timedelta(seconds=3), limit=1) == 1
    with factory() as session:
        for index, task_id in enumerate(task_ids):
            child = session.get(model, task_id)
            assert (child.lease_owner is not None) == (index == 0)
            if family != "video":
                assert child.retry_count == index


def test_reclaimed_coordinator_fences_old_aggregate_without_blocking_chunks(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, old, prepared = _prepare(factory, queue, task_id, chunk_count=1)

    reclaimed_at = NOW + timedelta(minutes=21)
    assert queue.reap_expired(
         now=reclaimed_at
    ) == 1
    child = queue.claim_chunk(
        "child-worker", lease_duration=timedelta(minutes=1), now=reclaimed_at,
    )
    assert child is not None
    assert child.chunk_parse_task_id == prepared.chunk_parse_task_ids[0]
    replacement = queue.claim_video(
        "replacement",
        lease_duration=timedelta(minutes=5),
        now=reclaimed_at,
    )
    assert replacement is not None
    assert replacement.lease_version > old.lease_version

    with pytest.raises(VideoParseLeaseLostError):
        repo.aggregate_task(old, now=reclaimed_at)
    with pytest.raises(VideoParseLeaseLostError):
        repo.reserve_model_call(
            old,
            "stale-call",
            lease_duration=timedelta(minutes=1),
            now=reclaimed_at,
        )


@pytest.mark.parametrize("failure_entry", ["retry", "expiry"])
def test_coordinator_recovery_preserves_capacity_and_does_not_exhaust_business_budget(
    tmp_path, monkeypatch, failure_entry,
):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'v2-coordinator-recovery.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=2, accepted=True, batch_size=2)
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).max_attempts = 1
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=2)
    child = queue.claim_chunk("in-flight", lease_duration=timedelta(minutes=1), now=NOW)
    failed_at = NOW + timedelta(seconds=6)
    resumed_at = failed_at
    if failure_entry == "retry":
        result = queue.retry_video(video_lease, error="coordinator exited", now=failed_at)
        assert result is not None and result.prepare_failure_count is None
        resumed_at += timedelta(seconds=video_lease.parse_config["execution"]["retry_initial_delay_seconds"])
        assert result.next_execute_time == resumed_at
    else:
        with factory.begin() as session:
            session.get(VideoParseTask, task_id).lease_expire_time = NOW + timedelta(seconds=5)
        assert queue.reap_expired_videos(now=failed_at) == 1
    replacement = MySqlVideoParseQueue(factory, video_concurrency=3)
    resumed = replacement.claim_video("replacement", lease_duration=timedelta(minutes=1), now=resumed_at)
    assert resumed is not None and resumed.attempt_count == 2
    assert resumed.lease_version > video_lease.lease_version
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING" and parent.capacity_acquired is True
        assert parent.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0
        assert "failure_status" not in parent.detail["_worker_runtime_v2"]
    assert replacement.heartbeat_chunk(child, lease_duration=timedelta(minutes=1), now=failed_at)
    repo.commit_chunk_result(child, content={"persons": ["retained"], "timeline": []}, now=failed_at)
    assert replacement.claim_chunk("next", lease_duration=timedelta(minutes=1), now=failed_at) is not None
    engine.dispose()


@pytest.mark.parametrize("interruption", ["expiry", "normal_stop"])
def test_prepare_failures_have_a_finite_budget_that_coordinator_takeover_preserves(repository, interruption):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.max_attempts = 1  # Observed coordinator attempts do not cap admission.
        config = {**parent.parse_config, "execution": {
            **parent.parse_config["execution"], "max_retry_count": 2,
            "retry_initial_delay_seconds": 3.0, "retry_max_delay_seconds": 5.0,
        }}
        parent.parse_config = config
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    lease = queue.claim_video("prepare", lease_duration=timedelta(minutes=1), now=NOW)
    result = queue.retry_video(lease, error="prepare failed", now=NOW)
    assert result == VideoParseRetryResult(NOW + timedelta(seconds=3), 1)
    assert not queue.retry_video(lease, error="duplicate failure", now=NOW)
    due = NOW + timedelta(seconds=3)
    assert queue.claim_video("too-early", lease_duration=timedelta(minutes=1), now=due - timedelta(seconds=1)) is None
    lease = queue.claim_video("prepare-retry", lease_duration=timedelta(seconds=1), now=due)
    assert lease is not None and lease.attempt_count == 2
    if interruption == "expiry":
        resumed_at = due + timedelta(seconds=2)
        assert queue.reap_expired_videos(now=resumed_at) == 1
    else:
        resumed_at = due
        assert queue.release_execution(lease, now=resumed_at)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.detail["_worker_runtime_v2"]["prepare_failures"] == 1
        assert parent.status == "PREPARING" and parent.capacity_acquired is True
    old_version = lease.lease_version
    lease = queue.claim_video("takeover", lease_duration=timedelta(minutes=1), now=resumed_at)
    assert lease is not None and lease.attempt_count == 3 and lease.lease_version > old_version
    assert queue.retry_video(lease, error="second prepare failure", now=resumed_at)
    due = resumed_at + timedelta(seconds=5)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.detail["_worker_runtime_v2"]["prepare_failures"] == 2
        assert parent.next_execute_time == due and parent.capacity_acquired is True
    lease = queue.claim_video("last-prepare", lease_duration=timedelta(minutes=1), now=due)
    assert lease is not None and lease.attempt_count == 4
    assert queue.retry_video(lease, error="prepare exhausted", now=due) == VideoParseRetryResult(None, 3)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.detail["_worker_runtime_v2"]["prepare_failures"] == 3
        assert parent.status == "PARSING_FAILED" and parent.capacity_acquired is False
        assert parent.lease_owner is None and parent.lease_version == lease.lease_version
        assert parent.parse_config == config
    assert queue.claim_video("exhausted", lease_duration=timedelta(minutes=1), now=due + timedelta(minutes=1)) is None


@pytest.mark.parametrize("active_chunk", [False, True])
def test_realtime_prepare_reaper_releases_only_unused_capacity(repository, active_chunk):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=False)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {"_worker_runtime_v2": {"accepted": False, "realtime_only": True}}
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    lease = queue.claim_realtime_preparation(
        task_id, "realtime-prepare", lease_duration=timedelta(seconds=1), now=NOW,
    )
    assert lease is not None
    if active_chunk:
        repo = VideoParseRepository(factory)
        chunks = repo.ensure_video_chunks(lease, [(0, 0, 10000)], now=NOW)
        prepared = repo.ensure_parse_tasks(
            lease, [chunks[0].video_chunk_id], [(0, 1)], now=NOW,
        )
        with factory.begin() as session:
            child = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0])
            child.status = "RUNNING"
            child.lease_owner = "active-chunk"
            child.lease_version = 1
            child.start_time = NOW
            child.lease_expire_time = NOW + timedelta(minutes=1)
    waiting = _task(factory, chunk_count=1, accepted=True)
    reaped_at = NOW + timedelta(seconds=2)
    assert queue.reap_expired_videos(now=reaped_at) == 1
    assert queue.reap_expired_videos(now=reaped_at) == 0
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PREPARING" and parent.lease_owner is None
        assert parent.capacity_acquired is active_chunk
        assert parent.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0
    admitted = queue.claim_video("next-video", lease_duration=timedelta(minutes=1), now=reaped_at)
    if active_chunk:
        assert admitted is None
    else:
        assert admitted is not None and admitted.video_parse_task_id == waiting


@pytest.mark.parametrize("preparing", [True, False])
def test_video_retry_uses_frozen_policy_and_logs_the_committed_result(tmp_path, monkeypatch, caplog, preparing):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'retry-result.sqlite'}", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.attempt_count = 99
        parent.parse_config = {**parent.parse_config, "execution": {
            **parent.parse_config["execution"], "retry_initial_delay_seconds": 2.5,
            "retry_max_delay_seconds": 7.0,
        }}
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    if preparing:
        lease = queue.claim_video("worker", lease_duration=timedelta(minutes=1), now=NOW)
    else:
        _repo, lease, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    monkeypatch.setattr("server.mq.video_parse_queue.now_utc", lambda: NOW)
    caplog.set_level(logging.WARNING, logger="server.mq.video_parse_consumer")

    async def fail(_lease):
        raise RuntimeError("prepare or coordinator failed")

    consumer = VideoParseConsumer(
        queue, VideoParseConsumerHandlers(fail, fail, fail, fail), worker_id="worker",
        lease_duration=timedelta(minutes=1), heartbeat_interval=timedelta(seconds=1),
        poll_interval=timedelta(seconds=2), maintenance_interval=timedelta(minutes=1),
        video_concurrency=3,
    )
    asyncio.run(consumer._run_video(lease))
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.next_execute_time == NOW + timedelta(seconds=2.5)
        assert task.detail["_worker_runtime_v2"].get("prepare_failures", 0) == int(preparing)
        assert task.attempt_count == 100
    message = caplog.records[-1].getMessage()
    assert f'retry_at="{(NOW + timedelta(seconds=2.5)).isoformat()}"' in message
    assert "coordinator_attempt=100" in message
    assert f'prepare_failure_count={1 if preparing else "null"}' in message
    assert queue.retry_video(lease, error="duplicate", now=NOW) is None
    engine.dispose()


@pytest.mark.parametrize("interval_phase", ["refine", "index"])
def test_failure_intent_blocks_each_delivery_but_not_existing_chunk_execution(
    repository, interval_phase,
):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=6, accepted=True, batch_size=6)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=6)
    in_flight = queue.claim_chunk("existing", lease_duration=timedelta(minutes=1), now=NOW)
    assert in_flight is not None
    with factory.begin() as session:
        for child_id in prepared.chunk_parse_task_ids[1:3]:
            session.get(ChunkParseTask, child_id).next_execute_time = NOW + timedelta(minutes=1)
    for child_id in prepared.chunk_parse_task_ids[3:]:
        lease = queue.claim_chunk("completed", lease_duration=timedelta(minutes=1), now=NOW)
        assert lease is not None and lease.chunk_parse_task_id == child_id
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
    if interval_phase == "index":
        refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
        assert refine is not None
        repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW), interval_content={"ready": True}, member_updates={}, now=NOW)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {"_worker_runtime_v2": {"accepted": True, "failure_status": "PARSING_FAILED"}}
        for child_id in prepared.chunk_parse_task_ids[1:3]:
            session.get(ChunkParseTask, child_id).next_execute_time = NOW

    assert queue.load_chunk_schedule(task_id, now=NOW).terminal_error is not None
    assert queue.claim_chunk("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    claim_interval = queue.claim_interval_refine if interval_phase == "refine" else queue.claim_chunk_index
    assert claim_interval("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.lease_owner = None
        parent.lease_expire_time = None
        parent.heartbeat_time = None
        assert parent.attempt_count < parent.max_attempts
    assert queue.claim_video("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    assert queue.heartbeat_chunk(in_flight, lease_duration=timedelta(minutes=1), now=NOW)
    repo.commit_chunk_result(in_flight, content={"persons": ["existing"], "timeline": []}, now=NOW)
    with factory() as session:
        assert session.get(ChunkParseTask, in_flight.chunk_parse_task_id).status == "WAITING"
        content = session.query(ChunkParseContent).filter_by(
            chunk_task_id=in_flight.chunk_parse_task_id,
        ).one()
        assert content.content["persons"] == ["existing"]


def test_exhausted_chunk_stops_sibling_redelivery_after_an_underbudget_error(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=3, accepted=True, batch_size=3)
    with factory.begin() as session:
        config = _config(batch_size=3)
        config["execution"]["max_retry_count"] = 1
        session.get(VideoParseTask, task_id).parse_config = config
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=3)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).retry_count = 1
    failed = queue.claim_chunk("failed", lease_duration=timedelta(minutes=1), now=NOW)
    sibling = queue.claim_chunk("sibling", lease_duration=timedelta(minutes=1), now=NOW)
    assert failed is not None and sibling is not None
    assert queue.retry_chunk(failed, error="retry exhausted", retry_at=NOW, now=NOW)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING" and parent.finish_time is None
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == "PARSING_FAILED"
        assert session.get(ChunkParseTask, sibling.chunk_parse_task_id).lease_owner == "sibling"
    assert queue.retry_video(
        video_lease, error="coordinator observed the exhausted Chunk", now=NOW,
    )
    assert queue.claim_video("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    assert queue.retry_chunk(sibling, error="temporary sibling error", retry_at=NOW, now=NOW)
    with factory() as session:
        child = session.get(ChunkParseTask, sibling.chunk_parse_task_id)
        assert child.status == "QUEUED"
        assert child.retry_count == 1
        assert child.lease_owner is None
    assert queue.claim_chunk("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    MySqlVideoParseQueue(factory, video_concurrency=3).reap_expired(now=NOW)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"
        assert session.get(ChunkParseTask, sibling.chunk_parse_task_id).retry_count == 1


def test_expired_child_owner_must_be_reaped_before_failure_is_published(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    old = queue.claim_chunk("lost", lease_duration=timedelta(seconds=5), now=NOW)
    assert old is not None
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {"_worker_runtime_v2": {"accepted": True, "failure_status": "PARSING_FAILED"}}
    reaped_at = NOW + timedelta(seconds=6)
    state = repo.aggregate_task(video_lease, now=reaped_at)
    assert state.status == "PARSING" and state.finish_time is None
    with factory() as session:
        assert session.get(ChunkParseTask, old.chunk_parse_task_id).lease_owner == "lost"
    assert queue.retry_video(
        video_lease, error="coordinator exits during failure drain", now=reaped_at,
    )
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING"
        assert session.get(ChunkParseTask, old.chunk_parse_task_id).lease_owner == "lost"
    MySqlVideoParseQueue(factory, video_concurrency=3).reap_expired(now=reaped_at)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING_FAILED"
        assert parent.finish_time == reaped_at
        assert parent.lease_owner is None
        child = session.get(ChunkParseTask, old.chunk_parse_task_id)
        assert child.status == "QUEUED" and child.lease_owner is None
        assert child.retry_count == 1
    with pytest.raises(VideoParseLeaseLostError):
        repo.commit_chunk_result(old, content={"persons": [], "timeline": []}, now=reaped_at)


@pytest.mark.parametrize("failed_phase", ["refine", "index"])
def test_interval_failure_intent_respects_the_still_running_parse_phase(repository, failed_phase):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=4, accepted=True, batch_size=4)
    with factory.begin() as session:
        config = _config(batch_size=4)
        config["execution"]["max_retry_count"] = 0
        session.get(VideoParseTask, task_id).parse_config = config
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=4)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).next_execute_time = NOW + timedelta(minutes=1)
    for child_id in prepared.chunk_parse_task_ids[1:]:
        lease = queue.claim_chunk("completed", lease_duration=timedelta(minutes=1), now=NOW)
        assert lease is not None and lease.chunk_parse_task_id == child_id
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).next_execute_time = NOW
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    if failed_phase == "index":
        repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW), interval_content={"ready": True}, member_updates={}, now=NOW)
        failed = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=1), now=NOW)
        assert failed is not None
    else:
        in_flight = queue.claim_chunk("existing", lease_duration=timedelta(minutes=1), now=NOW)
        assert in_flight is not None
        failed = refine
    retry = queue.retry_chunk if failed_phase == "index" else queue.retry_interval
    assert retry(failed, error="stage exhausted", retry_at=NOW, now=NOW)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        expected = "INDEXING_FAILED" if failed_phase == "index" else "PARSING"
        assert parent.status == expected
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == (
            "INDEXING_FAILED" if failed_phase == "index" else "PARSING_FAILED"
        )
        if failed_phase == "index":
            child = session.get(ChunkParseTask, failed.chunk_parse_task_id)
            assert child.phase == 3 and child.status == "FAILED"
            assert parent.capacity_acquired is False
            assert session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).phase == 1
        else:
            assert session.get(IntervalParseTask, failed.interval_parse_task_id).status == "PARSING_FAILED"
    assert queue.claim_chunk("remaining", lease_duration=timedelta(minutes=1), now=NOW) is None
    if failed_phase == "refine":
        assert queue.heartbeat_chunk(in_flight, lease_duration=timedelta(minutes=1), now=NOW)
        repo.commit_chunk_result(in_flight, content={"persons": ["retained"], "timeline": []}, now=NOW)
        MySqlVideoParseQueue(factory, video_concurrency=3).reap_expired(now=NOW)
        with factory() as session:
            assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"


def test_consumer_heartbeat_interval_is_bounded_by_lease(repository):
    engine, _unused = repository
    queue = MySqlVideoParseQueue(create_session_factory(engine), video_concurrency=3)

    async def unused(_lease):
        return None

    handlers = VideoParseConsumerHandlers(unused, unused, unused, unused)
    with pytest.raises(ValueError, match="heartbeat_interval"):
        VideoParseConsumer(
            queue,
            handlers,
            worker_id="worker",
            lease_duration=timedelta(seconds=5),
            heartbeat_interval=timedelta(seconds=5),
            poll_interval=timedelta(seconds=2),
            maintenance_interval=timedelta(minutes=5),
            video_concurrency=3,
        )
    with pytest.raises(ValueError, match="maintenance_interval"):
        VideoParseConsumer(
            queue,
            handlers,
            worker_id="worker",
            lease_duration=timedelta(seconds=5),
            maintenance_interval=timedelta(0),
            heartbeat_interval=timedelta(seconds=1),
            poll_interval=timedelta(seconds=2),
            video_concurrency=3,
        )


@pytest.mark.parametrize("realtime", [False, True])
def test_chunk_lease_carries_saved_retry_configuration(repository, realtime):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        config = dict(parent.parse_config)
        config["execution"] = {**config["execution"],
            "retry_initial_delay_seconds": 2.5, "retry_max_delay_seconds": 7.0,
        }
        parent.parse_config = config
    lease = (
        queue.claim_realtime_chunk(prepared.chunk_parse_task_ids[0], "worker", lease_duration=timedelta(minutes=1), now=NOW)
        if realtime else queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW)
    )
    assert lease is not None
    assert (lease.retry_initial_delay_seconds, lease.retry_max_delay_seconds) == (2.5, 7.0)


@pytest.mark.parametrize("phase,retry_count,task_delay", [
    (phase, count, delay)
    for phase in ("understand", "refine", "index")
    for count, delay in ((0, 2.5), (2, 7.0), (10000, 7.0))
    if phase != "index" or count < 10000
])
def test_worker_retry_all_phases_use_saved_task_policy(
    phase, retry_count, task_delay, monkeypatch,
):
    monkeypatch.setattr("server.mq.video_parse_consumer.now_utc", lambda: NOW)
    common = dict(
        video_parse_task_id="task", worker_id="worker", lease_version=1,
        lease_expire_time=NOW + timedelta(minutes=1), retry_count=retry_count,
        max_retry_count=10001, retry_initial_delay_seconds=2.5, retry_max_delay_seconds=7.0,
    )
    if phase in {"understand", "index"}:
        lease = ChunkParseLease(**common, chunk_parse_task_id="chunk-task", video_chunk_id="chunk", phase=1 if phase == "understand" else 3)
    else:
        lease = IntervalParseLease(**common, interval_parse_task_id="interval",
            chunk_parse_task_ids=("chunk-task",), start_sequence_no=0, chunk_count=1,
            phase="REFINING" if phase == "refine" else "INDEXING")
    calls = []

    class Queue:
        def heartbeat_chunk(self, *_args, **_kwargs):
            return True

        heartbeat_interval = heartbeat_chunk

        def retry_chunk(self, actual, **kwargs):
            calls.append((actual, kwargs))
            return True

        retry_interval = retry_chunk

    async def fail(_lease):
        raise RuntimeError("transient")

    consumer = VideoParseConsumer(
        Queue(),
        VideoParseConsumerHandlers(fail, fail, fail, fail),
        worker_id="worker",
        lease_duration=timedelta(minutes=5),
        heartbeat_interval=timedelta(seconds=1),
        poll_interval=timedelta(seconds=2),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )
    asyncio.run(consumer._run_chunk(lease) if phase != "refine" else consumer._run_interval(lease, fail))
    expected = task_delay
    assert calls == [(lease, {"error": "transient", "retry_at": NOW + timedelta(seconds=expected)})]


@pytest.mark.parametrize("phase", ["understand", "refine", "index"])
@pytest.mark.parametrize("retry_count,task_delay", [(0, 2.5), (2, 7.0)])
def test_expired_lease_all_phases_use_saved_task_backoff(repository, phase, retry_count, task_delay):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    model = ChunkParseTask if phase != "refine" else IntervalParseTask
    child_id = prepared.chunk_parse_task_ids[0] if phase != "refine" else prepared.interval_parse_task_ids[0]
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        config = dict(parent.parse_config)
        config["execution"] = {**config["execution"], "max_retry_count": 5,
            "retry_initial_delay_seconds": 2.5, "retry_max_delay_seconds": 7.0}
        parent.parse_config = config
        child = session.get(model, child_id)
        child.status = "REFINING" if phase == "refine" else "RUNNING"
        if phase != "refine":
            child.phase = 1 if phase == "understand" else 3
        child.start_time = NOW
        child.retry_count = retry_count
        child.lease_owner = "interrupted-worker"
        child.lease_expire_time = NOW + timedelta(seconds=1)
    reaped_at = NOW + timedelta(seconds=2)
    if phase != "refine":
        assert queue.reap_expired_chunks(now=reaped_at) == 1
    else:
        assert queue.reap_expired_intervals(now=reaped_at, ) == 1
    with factory() as session:
        child = session.get(model, child_id)
        assert child.retry_count == retry_count + 1
        assert child.lease_owner is None
        expected = task_delay
        assert child.next_execute_time == reaped_at + timedelta(seconds=expected)


@pytest.mark.parametrize("phase", ["prepare", "understand", "refine", "index"])
@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("failure_kind", ["sdk", "sdk_nested", "sse", "private", "cancel"])
def test_consumer_logs_retry_cause_without_sensitive_payloads(
    phase, recorded, failure_kind, monkeypatch, caplog,
):
    monkeypatch.setattr("server.mq.video_parse_consumer.now_utc", lambda: NOW)
    caplog.set_level(logging.WARNING, logger="server.mq.video_parse_consumer")
    common = dict(
        video_parse_task_id="video-task", worker_id="worker", lease_version=4,
        lease_expire_time=NOW + timedelta(minutes=1),
    )
    if phase == "prepare":
        lease = VideoParseLease(
            **common, video_id="video", attempt_count=2, max_attempts=3,
            parse_config={},
        )
    elif phase in {"understand", "index"}:
        lease = ChunkParseLease(
            phase=1 if phase == "understand" else 3,
            **common, chunk_parse_task_id="chunk-task", video_chunk_id="chunk",
            retry_count=1, max_retry_count=2,
            retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
        )
    else:
        lease = IntervalParseLease(
            **common, interval_parse_task_id="interval-task",
            chunk_parse_task_ids=("chunk-task",), start_sequence_no=0, chunk_count=1,
            phase="REFINING" if phase == "refine" else "INDEXING",
            retry_count=1, max_retry_count=2,
            retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
        )
    calls = []

    class Queue:
        @staticmethod
        def heartbeat_video(*_args, **_kwargs):
            return True

        heartbeat_chunk = heartbeat_video
        heartbeat_interval = heartbeat_video

        @staticmethod
        def retry_video(actual, **kwargs):
            calls.append((actual, kwargs))
            return VideoParseRetryResult(NOW + timedelta(seconds=10), 2) if recorded else None

        @staticmethod
        def retry_chunk(actual, **kwargs):
            calls.append((actual, kwargs))
            return recorded

        retry_interval = retry_chunk

        def release_execution(self, *_args, **_kwargs):
            return True

    if failure_kind.startswith("sdk"):
        root = ArkAPIError(
            "Provider unavailable\nAuthorization: Bearer private-auth\n"
            '"Cookie": "session=private-cookie"\n'
            "api_key='private-api' password=private-password token=private-token\n"
            "https://name:private-userinfo@host/path?signature=private-signature\n"
            "body: private-body\ninput: private-model-input\n"
            "sk-privateapikey123 eyJprivate.private.private\n",
            body={"code": "server_error", "input": "private-body-object"},
            request_id="provider-request",
        )
    elif failure_kind == "sse":
        root = SSEProxyError(
            "Ark Responses SSE ended without a terminal event",
            code="stream_incomplete", response_id="provider-response",
            partial_output_chars=7,
        )
        root.request_id = "provider-request"
    else:
        root = RuntimeError("SDK reader rejected response password=private-password")
    root.status_code = 502
    root.headers = {"authorization": "private-header-object"}
    error = QueryModelIncompleteError(
        "Multimodal Responses request failed", reason=ModelFailureReason.MODEL_TIMEOUT,
    )
    if failure_kind == "sse":
        error.__context__ = root
    else:
        error.__cause__ = root
    root.__context__ = error  # Malformed chains must not hang diagnostics.
    deepest = root
    if failure_kind == "sdk_nested":
        deepest = httpx.ReadTimeout("SDK read failed token=private-transport")
        root.__cause__ = deepest
        deepest.__context__ = error

    async def fail(_lease):
        if failure_kind == "cancel":
            raise asyncio.CancelledError("private cancellation text")
        raise error

    consumer = VideoParseConsumer(
        Queue(),
        VideoParseConsumerHandlers(fail, fail, fail, fail),
        worker_id="worker",
        lease_duration=timedelta(minutes=5),
        heartbeat_interval=timedelta(seconds=1),
        poll_interval=timedelta(seconds=2),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )
    if phase == "prepare":
        run = consumer._run_video(lease)
    elif phase in {"understand", "index"}:
        run = consumer._run_chunk(lease)
    else:
        run = consumer._run_interval(lease, fail)
    if failure_kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run)
        assert calls == [] and caplog.records == []
        return
    asyncio.run(run)
    retry_at = NOW + timedelta(seconds=10)
    assert calls == [(lease, {"error": str(error), **({"retry_at": retry_at} if phase != "prepare" else {})})]
    assert consumer.failure is None
    assert len(caplog.records) == 1
    record = caplog.records[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert len(message.splitlines()) == 1
    for key, value in {
        "video_parse_task_id": "video-task", "phase": phase, "attempt": None if phase == "prepare" else 2,
        "lease_version": 4, "retry_recorded": recorded,
        "retry_at": retry_at.isoformat() if phase != "prepare" or recorded else None,
        "error_type": "QueryModelIncompleteError", "reason": "model_timeout",
        "root_error_type": type(deepest).__name__, "root_status_code": 502,
    }.items():
        assert f"{key}={json.dumps(value)}" in message
    if phase == "prepare":
        assert 'coordinator_attempt=2' in message
        assert f'prepare_failure_count={2 if recorded else "null"}' in message
    if phase in {"understand", "index"}:
        assert 'chunk_parse_task_id="chunk-task"' in message
    elif phase != "prepare":
        assert 'interval_parse_task_id="interval-task"' in message
    if failure_kind.startswith("sdk"):
        assert 'root_error_code="server_error"' in message
        assert 'model_request_id="provider-request"' in message
        assert "Provider unavailable" in message and "[redacted" in message
    elif failure_kind == "sse":
        assert 'root_error_code="stream_incomplete"' in message
        assert 'response_id="provider-response"' in message
        assert "partial_output_chars=7" in message
        assert "ended without a terminal event" in message
    else:
        assert 'root_error_message="SDK reader rejected response password=[redacted]"' in message
    assert f'"error_type": "{type(deepest).__name__}"' in message
    assert 'error_chain_truncated=false' in message
    if failure_kind == "sdk_nested":
        assert 'root_error_message="SDK read failed token=[redacted]"' in message
    assert "private" not in message


@pytest.mark.parametrize("phase", ["prepare", "understand", "refine", "index"])
@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("retry_count", [1, 2])
def test_terminal_error_identifies_only_recorded_exhausted_child_failures(
    phase, recorded, retry_count, caplog,
):
    common = dict(
        video_parse_task_id="video-task", worker_id="worker", lease_version=4,
        lease_expire_time=NOW + timedelta(minutes=1),
    )
    child = dict(retry_count=retry_count, max_retry_count=2,
                 retry_initial_delay_seconds=1.0, retry_max_delay_seconds=30.0)
    if phase == "prepare":
        lease = VideoParseLease(**common, video_id="video", attempt_count=9,
                               max_attempts=3, parse_config={})
        error = RuntimeError("VideoParseTask is draining after a terminal failure")
    elif phase == "refine":
        lease = IntervalParseLease(
            **common, **child, interval_parse_task_id="interval-task",
            chunk_parse_task_ids=("chunk-task",), start_sequence_no=9, chunk_count=1,
            phase="REFINING",
        )
        error = RuntimeError("SDK turn failed token=private-token")
    else:
        lease = ChunkParseLease(
            **common, **child, chunk_parse_task_id="chunk-task", video_chunk_id="chunk",
            phase=1 if phase == "understand" else 3,
        )
        error = RuntimeError("SDK turn failed token=private-token")
    caplog.set_level(logging.WARNING, logger="server.mq.video_parse_consumer")
    _log_retry_failure(lease, phase=phase, error=error, recorded=recorded, retry_at=NOW)
    terminal = [record for record in caplog.records if record.levelno == logging.ERROR]
    if phase == "prepare" or not recorded or retry_count == 1:
        assert terminal == []
    else:
        assert len(terminal) == 1
        message = terminal[0].getMessage()
        assert 'event=video_parse_v2_terminal_error' in message
        assert 'terminal_reason="child_retry_budget_exhausted"' in message
        assert 'video_parse_task_id="video-task"' in message
        assert f'phase="{phase}"' in message
        assert 'attempt=3' in message and 'max_retry_count=2' in message
        assert 'root_error_message="SDK turn failed token=[redacted]"' in message
        assert f'{"interval" if phase == "refine" else "chunk"}_parse_task_id=' in message
        assert "private" not in message


@pytest.mark.parametrize("sensitive", [
    "Authorization: Bearer private-auth\nprivate-header-continuation",
    '"Cookie": "session=private-cookie"\nprivate-header-continuation',
    "api_key='private-api' password=private-password token=private-token",
    '"access_token": "private-access-token"; refresh_token=private-refresh',
    "https://name:private-userinfo@host/path?signature=private-signature",
    "body: {\nprivate-body-on-another-line\n}",
    "input: {\nprivate-model-input\n}",
    "data:video/mp4;base64,cHJpdmF0ZS1tZWRpYQ==",
    "sk-privateapikey123 eyJprivate.private.private",
])
def test_provider_failure_message_redacts_credentials_and_payloads(sensitive):
    safe = _failure_log_text("Provider request rejected: " + sensitive)
    assert safe.startswith("Provider request rejected: ")
    assert "[redacted" in safe and "private" not in safe
    assert "cHJpdmF0ZS1tZWRpYQ==" not in safe
    assert len(_failure_log_text("x" * 10000)) == 2048


def test_run_once_sync_finishes_the_task_it_claimed():
    handled: list[str] = []
    lease = VideoParseLease(
        video_parse_task_id="task",
        video_id="video",
        worker_id="worker",
        lease_version=1,
        lease_expire_time=NOW + timedelta(minutes=1),
        attempt_count=1,
        max_attempts=3,
        parse_config={},
    )

    class OneClaimQueue:
        def __init__(self):
            self.claimed = False

        def reap_expired(self, **_kwargs):
            return 0

        def claim_video(self, *_args, **_kwargs):
            if self.claimed:
                return None
            self.claimed = True
            return lease

        def claim_chunk(self, *_args, **_kwargs):
            return None

        def claim_interval_refine(self, *_args, **_kwargs):
            return None

        def claim_chunk_index(self, *_args, **_kwargs):
            return None

        def heartbeat_video(self, *_args, **_kwargs):
            return True

    async def prepare(claimed):
        await asyncio.sleep(0)
        handled.append(claimed.video_parse_task_id)

    async def unused(_lease):
        return None

    consumer = VideoParseConsumer(
        OneClaimQueue(),
        VideoParseConsumerHandlers(prepare, unused, unused, unused),
        worker_id="worker",
        lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(seconds=1),
        poll_interval=timedelta(milliseconds=1),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )
    assert consumer.run_once_sync()
    assert handled == ["task"]


def test_consumer_treats_a_committed_lease_as_success_during_cleanup():
    cleaned: list[str] = []
    retried: list[str] = []
    lease = VideoParseLease(
        video_parse_task_id="task",
        video_id="video",
        worker_id="worker",
        lease_version=1,
        lease_expire_time=NOW + timedelta(minutes=1),
        attempt_count=1,
        max_attempts=3,
        parse_config={},
    )

    class Queue:
        claimed = False

        @staticmethod
        def reap_expired(**_kwargs):
            return 0

        def claim_video(self, *_args, **_kwargs):
            if self.claimed:
                return None
            self.claimed = True
            return lease

        @staticmethod
        def claim_chunk(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_interval_refine(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_chunk_index(*_args, **_kwargs):
            return None

        @staticmethod
        def heartbeat_video(*_args, **_kwargs):
            return False

        @staticmethod
        def is_execution_lease_committed(_lease):
            return True

        @staticmethod
        def retry_video(claimed, **_kwargs):
            retried.append(claimed.video_parse_task_id)
            return False

    async def prepare(claimed):
        await asyncio.sleep(0.01)
        cleaned.append(claimed.video_parse_task_id)

    async def unused(_lease):
        return None

    consumer = VideoParseConsumer(
        Queue(),
        VideoParseConsumerHandlers(prepare, unused, unused, unused),
        worker_id="worker",
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=1),
        poll_interval=timedelta(milliseconds=1),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )

    assert consumer.run_once_sync()
    assert cleaned == ["task"]
    assert retried == []
    assert consumer.failure is None


def test_consumer_surfaces_retry_persistence_failure():
    lease = VideoParseLease(
        video_parse_task_id="task",
        video_id="video",
        worker_id="worker",
        lease_version=1,
        lease_expire_time=NOW + timedelta(minutes=1),
        attempt_count=1,
        max_attempts=3,
        parse_config={},
    )

    class Queue:
        claimed = False

        @staticmethod
        def reap_expired(**_kwargs):
            return 0

        def claim_video(self, *_args, **_kwargs):
            if self.claimed:
                return None
            self.claimed = True
            return lease

        @staticmethod
        def claim_chunk(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_interval_refine(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_chunk_index(*_args, **_kwargs):
            return None

        @staticmethod
        def heartbeat_video(*_args, **_kwargs):
            return True

        @staticmethod
        def retry_video(*_args, **_kwargs):
            raise RuntimeError("retry database unavailable")

    async def prepare(_lease):
        raise ValueError("provider failed")

    async def unused(_lease):
        return None

    consumer = VideoParseConsumer(
        Queue(),
        VideoParseConsumerHandlers(prepare, unused, unused, unused),
        worker_id="worker",
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=10),
        poll_interval=timedelta(milliseconds=1),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )

    with pytest.raises(RuntimeError, match="retry database unavailable"):
        consumer.run_once_sync()
    assert isinstance(consumer.failure, RuntimeError)


def test_consumer_throttles_async_maintenance():
    calls: list[str] = []

    class IdleQueue:
        @staticmethod
        def reap_expired(**_kwargs):
            return 0

        @staticmethod
        def claim_video(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_chunk(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_interval_refine(*_args, **_kwargs):
            return None

        @staticmethod
        def claim_chunk_index(*_args, **_kwargs):
            return None

    async def unused(_lease):
        return None

    async def maintenance():
        calls.append("scan")

    consumer = VideoParseConsumer(
        IdleQueue(),
        VideoParseConsumerHandlers(
            unused,
            unused,
            unused,
            unused,
            maintenance=maintenance,
        ),
        worker_id="worker",
        lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(seconds=1),
        maintenance_interval=timedelta(hours=1),
        poll_interval=timedelta(seconds=2),
        video_concurrency=3,
    )

    async def run_twice():
        assert not await consumer.run_once()
        assert not await consumer.run_once()

    asyncio.run(run_twice())
    assert calls == ["scan"]


def test_one_consumer_keeps_coordinator_alive_while_processing_children(tmp_path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'v2-consumer.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).next_execute_time = datetime(
            2020, 1, 1, tzinfo=UTC
        )
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo = VideoParseRepository(factory)

    async def prepare(lease):
        chunks = repo.ensure_video_chunks(lease, [(0, 0, 10000)])
        prepared = repo.ensure_parse_tasks(
            lease,
            [chunks[0].video_chunk_id],
            [(0, 1)],
        )
        repo.activate_video_task(lease)
        while True:
            state = repo.aggregate_task(lease)
            if state.status == "COMPLETE":
                return
            await asyncio.sleep(0.01)

    async def understand(lease):
        execution = repo.load_chunk_execution(lease)
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": []},
            detail={"sequence": execution.sequence_no},
        )

    async def refine(lease):
        repo.load_interval_members(lease)
        repo.commit_interval_refinement(
            lease,
            **_refinement_inputs(repo, lease, now=None), interval_content={"summary": "done"},
            member_updates={},
        )

    async def index(lease):
        snapshot = repo.load_chunk_indexing_snapshot(lease)
        chunk = snapshot.chunk
        repo.save_index_embeddings(lease, chunk.chunk_parse_task_id, chunk.content_version,
            {"semantic": {"vector": [1.0]}}, receipt={
                "parse_task_id": lease.video_parse_task_id, "video_chunk_id": lease.video_chunk_id,
                "content_version": chunk.content_version, "index_generation": snapshot.target_index_generation,
            })
        repo.complete_chunk_indexing(lease, chunk.content_version, snapshot.target_index_generation)

    consumer = VideoParseConsumer(
        queue,
        VideoParseConsumerHandlers(prepare, understand, refine, index),
        worker_id="one-process",
        lease_duration=timedelta(seconds=5),
        poll_interval=timedelta(milliseconds=10),
        heartbeat_interval=timedelta(seconds=1),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )
    consumer.start()
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            with factory() as session:
                if session.get(VideoParseTask, task_id).status == "COMPLETE":
                    break
            time.sleep(0.02)
        else:
            raise AssertionError(f"V2 consumer did not finish: {consumer.failure!r}")
    finally:
        consumer.close(timeout=2)
    assert consumer.failure is None


def test_service_capacity_recovers_before_admitting_new_video(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    admitted = []
    for _ in range(3):
        task_id = _task(factory, chunk_count=2, accepted=True, batch_size=2)
        _repo, lease, _prepared = _prepare(factory, queue, task_id, chunk_count=2)
        admitted.append(lease)
    waiting = _task(factory, chunk_count=1, accepted=True)
    assert queue.claim_video("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory.begin() as session:
        interrupted = session.get(VideoParseTask, admitted[0].video_parse_task_id)
        assert interrupted.capacity_acquired is True
        interrupted.lease_expire_time = NOW + timedelta(seconds=1)
    later = NOW + timedelta(seconds=2)
    queue.reap_expired_videos(now=later)
    replacement = MySqlVideoParseQueue(factory, video_concurrency=3)
    recovered = replacement.claim_video("recover", lease_duration=timedelta(minutes=1), now=later)
    assert recovered.video_parse_task_id == admitted[0].video_parse_task_id
    with factory() as session:
        assert session.get(VideoParseTask, recovered.video_parse_task_id).capacity_acquired is True
        assert session.get(VideoParseTask, waiting).capacity_acquired is False
    assert replacement.claim_video("fourth", lease_duration=timedelta(minutes=1), now=later) is None


def test_chunk_claims_rotate_between_admitted_videos(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    task_ids = []
    for _ in range(3):
        task_id = _task(factory, chunk_count=3, accepted=True, batch_size=3)
        _prepare(factory, queue, task_id, chunk_count=3)
        task_ids.append(task_id)
    leases = [queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW) for _ in range(6)]
    assert {lease.video_parse_task_id for lease in leases[:3]} == set(task_ids)
    assert {lease.video_parse_task_id for lease in leases[3:]} == set(task_ids)


def test_realtime_request_does_not_reserve_capacity_for_a_child_still_in_backoff(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, _parent, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).capacity_acquired = False
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).next_execute_time = NOW + timedelta(minutes=1)
    assert queue.claim_realtime_chunk(prepared.chunk_parse_task_ids[0], "realtime",
        lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory() as session:
        assert session.get(VideoParseTask, task_id).capacity_acquired is False


def test_parse_refine_index_each_have_independent_ten_execution_capacity(repository):
    # This protects phase semantics. Cross-connection lock races use the MySQL suite.
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=32, accepted=True, batch_size=32)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, _parent, _prepared = _prepare(factory, queue, task_id, chunk_count=32)
    for _ in range(22):
        lease = queue.claim_chunk("parse", lease_duration=timedelta(minutes=5), now=NOW)
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
    parses = [queue.claim_chunk("parse", lease_duration=timedelta(minutes=5), now=NOW) for _ in range(10)]
    assert all(parses)
    assert queue.claim_chunk("parse-full", lease_duration=timedelta(minutes=5), now=NOW) is None
    for _ in range(10):
        lease = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=NOW)
        repo.commit_interval_refinement(lease, **_refinement_inputs(repo, lease, now=NOW),
            interval_content={"updates": []}, member_updates={}, now=NOW)
    refines = [queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=NOW) for _ in range(10)]
    indexes = [queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=NOW) for _ in range(10)]
    assert all(refines) and all(indexes)
    assert all(lease.phase == 3 for lease in indexes)
    assert queue.claim_interval_refine("refine-full", lease_duration=timedelta(minutes=5), now=NOW) is None
    assert queue.claim_chunk_index("index-full", lease_duration=timedelta(minutes=5), now=NOW) is None
    with factory() as session:
        assert session.query(ChunkParseTask).filter_by(video_task_id=task_id, phase=1, status="RUNNING").count() == 10
        assert session.query(IntervalParseTask).filter_by(video_task_id=task_id, status="REFINING").count() == 10
        assert session.query(ChunkParseTask).filter_by(video_task_id=task_id, phase=3, status="RUNNING").count() == 10


@pytest.mark.parametrize("phase", ["parse", "refine", "index"])
def test_stage_deadline_is_not_extended_by_heartbeats_and_recovery_keeps_phase(repository, phase):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.parse_config = {**parent.parse_config, "execution": {
            **parent.parse_config["execution"], phase + "_execution_timeout_seconds": 5.0,
        }}
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, _parent, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    lease = queue.claim_chunk("parse", lease_duration=timedelta(minutes=1), now=NOW)
    if phase != "parse":
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
        lease = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    if phase == "index":
        repo.commit_interval_refinement(lease, **_refinement_inputs(repo, lease, now=NOW),
            interval_content={"updates": []}, member_updates={}, now=NOW)
        lease = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=1), now=NOW)
    assert lease.execution_deadline == NOW + timedelta(seconds=5)
    heartbeat = queue.heartbeat_interval if phase == "refine" else queue.heartbeat_chunk
    assert heartbeat(lease, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=4))
    model = IntervalParseTask if phase == "refine" else ChunkParseTask
    task_key = lease.interval_parse_task_id if phase == "refine" else lease.chunk_parse_task_id
    with factory() as session:
        child = session.get(model, task_key)
        assert child.start_time == NOW
        assert child.lease_expire_time == child.start_time + timedelta(seconds=5) == lease.execution_deadline
    assert not heartbeat(lease, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=5))
    queue.reap_expired(now=NOW + timedelta(seconds=5))
    with factory() as session:
        child = session.get(model, task_key)
        assert child.retry_count == 1 and child.lease_expire_time is None and child.lease_owner is None
        assert child.status == ("WAITING" if phase == "refine" else "QUEUED")
        if phase != "refine":
            assert child.phase == (1 if phase == "parse" else 3)
        assert child.lease_version == lease.lease_version
        assert child.last_error["message"] == "execution deadline exceeded"
        retry_at = child.next_execute_time
    claim = {"parse": queue.claim_chunk, "refine": queue.claim_interval_refine, "index": queue.claim_chunk_index}[phase]
    renewed = claim("retry", lease_duration=timedelta(minutes=1), now=retry_at)
    assert renewed is not None and renewed.lease_version > lease.lease_version
    assert renewed.execution_deadline == retry_at + timedelta(seconds=5)
    assert renewed.execution_deadline > lease.execution_deadline
    with factory() as session:
        child = session.get(model, task_key)
        assert child.start_time == retry_at
        assert child.lease_expire_time == renewed.execution_deadline
    assert not heartbeat(lease, lease_duration=timedelta(minutes=1), now=retry_at)


def test_phase_commit_receipt_survives_next_lease_and_queue_finalizes_success(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, parent_lease, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    parse = queue.claim_chunk("parse", lease_duration=timedelta(minutes=1), now=NOW)
    repo.commit_chunk_result(parse, content={"persons": [], "timeline": []}, now=NOW)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW),
        interval_content={"updates": []}, member_updates={}, now=NOW)
    index = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=1), now=NOW)
    assert index.lease_version > parse.lease_version
    assert queue.is_execution_lease_committed(parse)
    assert not queue.is_execution_lease_committed(index)
    snapshot = repo.load_chunk_indexing_snapshot(index, now=NOW)
    assert snapshot.target_index_generation == parent_lease.parse_config["indexing"]["index_generation"]
    repo.save_index_embeddings(index, index.chunk_parse_task_id, snapshot.chunk.content_version,
        {"semantic": {"vector": [1.0]}}, receipt={
            "parse_task_id": task_id, "video_chunk_id": index.video_chunk_id,
            "content_version": snapshot.chunk.content_version, "index_generation": snapshot.target_index_generation,
        }, now=NOW)
    repo.complete_chunk_indexing(index, snapshot.chunk.content_version, snapshot.target_index_generation, now=NOW)
    assert queue.is_execution_lease_committed(index)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.lease_owner = None
        parent.lease_expire_time = None
        parent.heartbeat_time = None
        parent.status = "INDEXING"  # Coordinator exited before its final aggregation.
    MySqlVideoParseQueue(factory, video_concurrency=3).reap_expired(now=NOW)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "COMPLETE" and parent.capacity_acquired is False
        assert parent.target_index_generation == parent.parse_config["indexing"]["index_generation"] == snapshot.target_index_generation
        assert session.get(ChunkParseTask, index.chunk_parse_task_id).indexed_content_version == snapshot.chunk.content_version


def test_consumer_rotates_phases_without_a_shared_handler_limit():
    selected = []
    common = dict(video_parse_task_id="task", worker_id="worker", lease_version=1,
        lease_expire_time=NOW + timedelta(minutes=1), retry_count=0, max_retry_count=1,
        retry_initial_delay_seconds=1.0, retry_max_delay_seconds=2.0)
    chunk = ChunkParseLease(**common, chunk_parse_task_id="chunk", video_chunk_id="video-chunk")
    index = ChunkParseLease(**common, chunk_parse_task_id="index", video_chunk_id="video-chunk", phase=3)
    refine = IntervalParseLease(**common, interval_parse_task_id="interval", chunk_parse_task_ids=("chunk",),
        start_sequence_no=0, chunk_count=1, phase="REFINING")

    class Queue:
        def reap_expired(self, **_kwargs):
            return 0
        def claim_video(self, *_args, **_kwargs):
            return None
        def claim_chunk(self, *_args, **_kwargs):
            selected.append("parse")
            return chunk
        def claim_interval_refine(self, *_args, **_kwargs):
            selected.append("refine")
            return refine
        def claim_chunk_index(self, *_args, **_kwargs):
            selected.append("index")
            return index
        def heartbeat_chunk(self, *_args, **_kwargs):
            return True
        heartbeat_interval = heartbeat_chunk

    async def run():
        stopped = asyncio.Event()
        async def handler(_lease):
            await stopped.wait()
        consumer = VideoParseConsumer(
            Queue(),
            VideoParseConsumerHandlers(handler, handler, handler, handler),
            worker_id="worker",
            lease_duration=timedelta(minutes=5),
            heartbeat_interval=timedelta(seconds=1),
            poll_interval=timedelta(seconds=2),
            maintenance_interval=timedelta(minutes=5),
            video_concurrency=3,
        )
        for _ in range(9):
            assert await consumer.run_once()
        assert selected == ["parse", "refine", "index"] * 3
        stopped.set()
        await asyncio.gather(*consumer._work_tasks)
    asyncio.run(run())


def test_consumer_deadline_cancels_a_stuck_call_even_while_heartbeats_succeed():
    now = datetime.now(UTC)
    lease = ChunkParseLease(video_parse_task_id="task", chunk_parse_task_id="chunk", video_chunk_id="physical",
        worker_id="worker", lease_version=1, lease_expire_time=now + timedelta(minutes=1),
        execution_deadline=now + timedelta(milliseconds=35), retry_count=0, max_retry_count=1,
        retry_initial_delay_seconds=1.0, retry_max_delay_seconds=2.0)
    retries, cancelled = [], []
    class Queue:
        def heartbeat_chunk(self, *_args, **_kwargs):
            return True
        def is_execution_lease_committed(self, _lease):
            return False
        def retry_chunk(self, claimed, **kwargs):
            retries.append((claimed, kwargs))
            return True
    async def handler(_lease):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    consumer = VideoParseConsumer(
        Queue(),
        VideoParseConsumerHandlers(handler, handler, handler, handler),
        worker_id="worker",
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=5),
        poll_interval=timedelta(seconds=2),
        maintenance_interval=timedelta(minutes=5),
        video_concurrency=3,
    )
    asyncio.run(asyncio.wait_for(consumer._run_chunk(lease), timeout=1))
    assert cancelled == [True] and len(retries) == 1
    assert "deadline" in retries[0][1]["error"]


def test_normal_stop_releases_only_after_blocking_execution_has_exited():
    from concurrent.futures import Future
    from server.utils.execution import BlockingCallCancelled

    completion = Future()
    released = []
    lease = ChunkParseLease(video_parse_task_id="task", chunk_parse_task_id="chunk", video_chunk_id="physical",
        worker_id="worker", lease_version=1, lease_expire_time=NOW + timedelta(minutes=1),
        retry_count=0, max_retry_count=1, retry_initial_delay_seconds=1.0, retry_max_delay_seconds=2.0)
    class Queue:
        def heartbeat_chunk(self, *_args, **_kwargs):
            return True
        def release_execution(self, claimed):
            released.append(claimed)
            return True
    async def run():
        entered = asyncio.Event()
        async def handler(_lease):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise BlockingCallCancelled(completion)
        consumer = VideoParseConsumer(
            Queue(),
            VideoParseConsumerHandlers(handler, handler, handler, handler),
            worker_id="worker",
            lease_duration=timedelta(minutes=5),
            heartbeat_interval=timedelta(seconds=1),
            poll_interval=timedelta(seconds=2),
            maintenance_interval=timedelta(minutes=5),
            video_concurrency=3,
        )
        work = asyncio.create_task(consumer._run_chunk(lease))
        await entered.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert released == []
        completion.set_result(None)
        assert released == [lease]
    asyncio.run(run())


def test_normal_stop_returns_same_phase_without_charging_retry_budget(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    _repo, parent, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    lease = queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW)
    assert queue.release_execution(lease, now=NOW)
    assert queue.release_execution(parent, now=NOW)
    with factory() as session:
        child = session.get(ChunkParseTask, lease.chunk_parse_task_id)
        task = session.get(VideoParseTask, task_id)
        assert child.phase == 1 and child.status == "QUEUED" and child.retry_count == 0
        assert child.lease_version == lease.lease_version and child.lease_expire_time is None
        assert task.capacity_acquired is True
        assert task.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0
    assert not queue.release_execution(lease, now=NOW)
    resumed_at = NOW + timedelta(seconds=1)
    resumed = queue.claim_chunk("replacement", lease_duration=timedelta(minutes=1), now=resumed_at)
    assert resumed.lease_version == lease.lease_version + 1
    assert resumed.execution_deadline == lease.execution_deadline + timedelta(seconds=1)
    with factory() as session:
        assert session.get(ChunkParseTask, resumed.chunk_parse_task_id).start_time == resumed_at
