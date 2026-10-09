from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import time
from uuid import uuid4

import pytest

from tests.server.test_video_parse_repository import _refinement_inputs

from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.dto.jobDTO import VideoParseLease
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
)
from server.repository.video_parse import VideoParseLeaseLostError, VideoParseRepository


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
            series_id=series_id, workspace_id="mine", name=f"Series {series_id}",
            description=None, status="INDEXING", important_extra_data=None,
            character_bible=[],
        ))
        session.add(Video(
            video_id=video_id, series_id=series_id, episode_order=1,
            upload_sequence=1, original_filename="episode.mp4",
            original_source_uri="artifact://test/episode.mp4",
            content_hash=(video_id.replace("-", "") * 2)[:64], total_bytes=10,
            duration_seconds=chunk_count * 10000, upload_status="COMPLETED",
            parse_status="QUEUED", upload_progress=100, parse_progress=0,
        ))
        session.add(VideoParseTask(
            id=task_id, video_id=video_id, pipeline_version="v2", status="PENDING",
            parse_config=_config(batch_size=batch_size), chunk_count=chunk_count,
            version=1, next_execute_time=NOW, attempt_count=0, max_attempts=3,
            lease_version=0, processing_duration_ms=0,
            processing_timing_complete=True, is_test=True, is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": accepted}},
            create_time=NOW, update_time=NOW,
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
    queue = MySqlVideoParseQueue(factory)

    lease = queue.claim_video(
        "worker", lease_duration=timedelta(minutes=1), now=NOW
    )
    assert lease is not None and lease.video_parse_task_id == accepted_id
    with factory() as session:
        assert session.get(VideoParseTask, legacy_id).status == "PENDING"


def test_dispatch_and_claim_enforce_ninety_percent_batch_gate(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=12, accepted=True, batch_size=10)
    queue = MySqlVideoParseQueue(factory)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=12)

    schedule = queue.load_chunk_schedule(task_id, now=NOW)
    assert [item.dispatchable for item in schedule.tasks[:10]] == [True] * 10
    assert [item.dispatchable for item in schedule.tasks[10:]] == [False, False]

    with factory.begin() as session:
        for child_id in prepared.chunk_parse_task_ids[:8]:
            session.get(ChunkParseTask, child_id).status = "COMPLETE"
    assert not queue.load_chunk_schedule(task_id, now=NOW).tasks[10].dispatchable

    with factory.begin() as session:
        session.get(
            ChunkParseTask, prepared.chunk_parse_task_ids[8]
        ).status = "COMPLETE"
    opened = queue.load_chunk_schedule(task_id, now=NOW)
    assert opened.tasks[10].dispatchable and opened.tasks[11].dispatchable

    # Queue consumption still requires the scheduler's durable dispatch marker.
    assert queue.claim_chunk(
        "chunk-worker", lease_duration=timedelta(minutes=1), now=NOW
    ) is None
    assert queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[10], _lease, now=NOW
    )
    claimed = queue.claim_chunk(
        "chunk-worker", lease_duration=timedelta(minutes=1), now=NOW
    )
    assert claimed is not None
    assert claimed.chunk_parse_task_id == prepared.chunk_parse_task_ids[10]


def test_active_and_dispatched_chunks_share_capacity(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=4, accepted=True, batch_size=2)
    queue = MySqlVideoParseQueue(factory)
    _repo, _lease, prepared = _prepare(factory, queue, task_id, chunk_count=4)

    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], _lease, now=NOW)
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[1], _lease, now=NOW)
    assert not queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[2], _lease, now=NOW
    )
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


def test_video_lease_survives_activation_and_can_be_reclaimed_after_expiry(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory)
    _repo, lease, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING"
        assert (task.lease_owner, task.lease_version) == ("main", lease.lease_version)
    assert queue.heartbeat_video(
        lease, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=1)
    )
    assert queue.reap_expired(
        retry_delay=timedelta(0), now=NOW + timedelta(minutes=2)
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
    queue = MySqlVideoParseQueue(factory)
    _repo, video_lease, prepared = _prepare(
        factory, queue, task_id, chunk_count=1
    )
    assert queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[0], video_lease, now=NOW
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
        retry_delay=timedelta(0),
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


def test_reclaimed_coordinator_fences_old_dispatch_and_aggregate(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory)
    repo, old, prepared = _prepare(factory, queue, task_id, chunk_count=1)

    reclaimed_at = NOW + timedelta(minutes=21)
    assert queue.reap_expired(
        retry_delay=timedelta(0), now=reclaimed_at
    ) == 1
    replacement = queue.claim_video(
        "replacement",
        lease_duration=timedelta(minutes=5),
        now=reclaimed_at,
    )
    assert replacement is not None
    assert replacement.lease_version > old.lease_version

    assert not queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[0], old, now=reclaimed_at
    )
    with pytest.raises(VideoParseLeaseLostError):
        repo.aggregate_task(old, now=reclaimed_at)
    with pytest.raises(VideoParseLeaseLostError):
        repo.reserve_model_call(
            old,
            "stale-call",
            lease_duration=timedelta(minutes=1),
            now=reclaimed_at,
        )
    assert queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[0], replacement, now=reclaimed_at
    )


@pytest.mark.parametrize("failure_entry", ["retry", "expiry"])
def test_exhausted_coordinator_drains_and_a_new_consumer_finalizes_without_reclaim(
    tmp_path, monkeypatch, failure_entry,
):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'v2-failure-drain.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=2, accepted=True, batch_size=2)
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).max_attempts = 1
    queue = MySqlVideoParseQueue(factory)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=2)
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
    child = queue.claim_chunk("in-flight", lease_duration=timedelta(minutes=1), now=NOW)
    assert child is not None
    failed_at = NOW + timedelta(seconds=6)
    if failure_entry == "retry":
        assert queue.retry_video(
            video_lease, error="coordinator exhausted", retry_at=failed_at, now=failed_at,
        )
    else:
        with factory.begin() as session:
            session.get(VideoParseTask, task_id).lease_expire_time = NOW + timedelta(seconds=5)
        assert queue.reap_expired_videos(retry_delay=timedelta(0), now=failed_at) == 1
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING"
        assert parent.finish_time is None
        assert parent.lease_owner is None
        assert parent.attempt_count == parent.max_attempts == 1
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == "PARSING_FAILED"

    replacement = MySqlVideoParseQueue(factory)
    assert replacement.claim_video(
        "replacement", lease_duration=timedelta(minutes=1), now=failed_at,
    ) is None
    assert replacement.heartbeat_chunk(
        child, lease_duration=timedelta(minutes=1), now=failed_at,
    )
    repo.commit_chunk_result(child, content={"persons": ["retained"], "timeline": []}, now=failed_at)
    poll_at = failed_at + timedelta(seconds=1)
    monkeypatch.setattr("server.mq.video_parse_queue.now_utc", lambda: poll_at)

    async def unexpected(_lease):
        pytest.fail("draining parent must not deliver new work")

    consumer = VideoParseConsumer(
        replacement,
        VideoParseConsumerHandlers(unexpected, unexpected, unexpected, unexpected),
        worker_id="replacement", lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(seconds=1),
    )
    assert not asyncio.run(consumer.run_once())
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING_FAILED"
        assert parent.finish_time == poll_at
        assert parent.lease_owner is None
        assert session.get(ChunkParseTask, child.chunk_parse_task_id).status == "COMPLETE"
        content = session.query(ChunkParseContent).filter_by(
            chunk_parse_task_id=child.chunk_parse_task_id,
        ).one()
        assert content.content == {"persons": ["retained"], "timeline": []}
        assert content.version == 1
        assert session.get(ChunkParseTask, prepared.chunk_parse_task_ids[1]).status == "QUEUED"
    engine.dispose()


@pytest.mark.parametrize("interval_phase", ["refine", "index"])
def test_failure_intent_blocks_each_delivery_but_not_existing_chunk_execution(
    repository, interval_phase,
):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=6, accepted=True, batch_size=6)
    queue = MySqlVideoParseQueue(factory)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=6)
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
    in_flight = queue.claim_chunk("existing", lease_duration=timedelta(minutes=1), now=NOW)
    assert in_flight is not None
    for child_id in prepared.chunk_parse_task_ids[3:]:
        assert queue.dispatch_chunk(child_id, video_lease, now=NOW)
        lease = queue.claim_chunk("completed", lease_duration=timedelta(minutes=1), now=NOW)
        assert lease is not None and lease.chunk_parse_task_id == child_id
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[2], video_lease, now=NOW)
    if interval_phase == "index":
        refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
        assert refine is not None
        repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW), interval_content={"ready": True}, member_updates={}, now=NOW)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {"_worker_runtime_v2": {"accepted": True, "failure_status": "PARSING_FAILED"}}

    assert not any(item.dispatchable for item in queue.load_chunk_schedule(task_id, now=NOW).tasks)
    assert not queue.dispatch_chunk(prepared.chunk_parse_task_ids[1], video_lease, now=NOW)
    assert queue.claim_chunk("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    claim_interval = queue.claim_interval_refine if interval_phase == "refine" else queue.claim_interval_index
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
        assert session.get(ChunkParseTask, in_flight.chunk_parse_task_id).status == "COMPLETE"
        content = session.query(ChunkParseContent).filter_by(
            chunk_parse_task_id=in_flight.chunk_parse_task_id,
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
    queue = MySqlVideoParseQueue(factory)
    _repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=3)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).retry_count = 1
    for child_id in prepared.chunk_parse_task_ids[:2]:
        assert queue.dispatch_chunk(child_id, video_lease, now=NOW)
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
        video_lease, error="coordinator observed the exhausted Chunk", retry_at=NOW, now=NOW,
    )
    assert queue.claim_video("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    assert queue.retry_chunk(sibling, error="temporary sibling error", retry_at=NOW, now=NOW)
    with factory() as session:
        child = session.get(ChunkParseTask, sibling.chunk_parse_task_id)
        assert child.status == "QUEUED"
        assert child.retry_count == 1
        assert child.lease_owner is None
    assert not queue.dispatch_chunk(prepared.chunk_parse_task_ids[2], video_lease, now=NOW)
    assert queue.claim_chunk("new", lease_duration=timedelta(minutes=1), now=NOW) is None
    MySqlVideoParseQueue(factory).reap_expired(retry_delay=timedelta(0), now=NOW)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"
        assert session.get(ChunkParseTask, sibling.chunk_parse_task_id).retry_count == 1


def test_expired_child_owner_must_be_reaped_before_failure_is_published(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    task_id = _task(factory, chunk_count=1, accepted=True, batch_size=1)
    queue = MySqlVideoParseQueue(factory)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=1)
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
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
        video_lease, error="coordinator exits during failure drain", retry_at=reaped_at, now=reaped_at,
    )
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING"
        assert session.get(ChunkParseTask, old.chunk_parse_task_id).lease_owner == "lost"
    MySqlVideoParseQueue(factory).reap_expired(retry_delay=timedelta(0), now=reaped_at)
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
    queue = MySqlVideoParseQueue(factory)
    repo, video_lease, prepared = _prepare(factory, queue, task_id, chunk_count=4)
    for child_id in prepared.chunk_parse_task_ids[1:]:
        assert queue.dispatch_chunk(child_id, video_lease, now=NOW)
        lease = queue.claim_chunk("completed", lease_duration=timedelta(minutes=1), now=NOW)
        assert lease is not None and lease.chunk_parse_task_id == child_id
        repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    if failed_phase == "index":
        repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW), interval_content={"ready": True}, member_updates={}, now=NOW)
        failed = queue.claim_interval_index("index", lease_duration=timedelta(minutes=1), now=NOW)
        assert failed is not None
    else:
        assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
        in_flight = queue.claim_chunk("existing", lease_duration=timedelta(minutes=1), now=NOW)
        assert in_flight is not None
        failed = refine
    assert queue.retry_interval(failed, error="interval exhausted", retry_at=NOW, now=NOW)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING" and parent.finish_time is None
        interval = session.get(IntervalParseTask, failed.interval_parse_task_id)
        assert interval.status == ("INDEXING_FAILED" if failed_phase == "index" else "PARSING_FAILED")
        assert interval.lease_owner is None
        runtime = parent.detail["_worker_runtime_v2"]
        if failed_phase == "index":
            assert "failure_status" not in runtime
        else:
            assert runtime["failure_status"] == "PARSING_FAILED"
    if failed_phase == "index":
        assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
        assert queue.claim_chunk("remaining", lease_duration=timedelta(minutes=1), now=NOW) is not None
    else:
        assert queue.retry_video(
            video_lease, error="coordinator observed the exhausted Refine", retry_at=NOW, now=NOW,
        )
        assert queue.heartbeat_chunk(in_flight, lease_duration=timedelta(minutes=1), now=NOW)
        repo.commit_chunk_result(in_flight, content={"persons": ["retained"], "timeline": []}, now=NOW)
        MySqlVideoParseQueue(factory).reap_expired(retry_delay=timedelta(0), now=NOW)
        with factory() as session:
            assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"


def test_consumer_heartbeat_interval_is_bounded_by_lease(repository):
    engine, _unused = repository
    queue = MySqlVideoParseQueue(create_session_factory(engine))

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
        )
    with pytest.raises(ValueError, match="maintenance_interval"):
        VideoParseConsumer(
            queue,
            handlers,
            worker_id="worker",
            lease_duration=timedelta(seconds=5),
            maintenance_interval=timedelta(0),
        )


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

        def claim_interval_index(self, *_args, **_kwargs):
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
        max_concurrent_handlers=2,
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
        def claim_interval_index(*_args, **_kwargs):
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
        max_concurrent_handlers=2,
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
        def claim_interval_index(*_args, **_kwargs):
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
        max_concurrent_handlers=2,
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
        def claim_interval_index(*_args, **_kwargs):
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
        max_concurrent_handlers=2,
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
    queue = MySqlVideoParseQueue(factory)
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
            schedule = queue.load_chunk_schedule(task_id)
            for item in schedule.tasks:
                if item.dispatchable:
                    queue.dispatch_chunk(item.chunk_parse_task_id, lease)
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
        snapshot = repo.load_interval_indexing_snapshot(lease)
        repo.complete_interval_indexing(
            lease,
            {item.chunk_parse_task_id: item.content_version for item in snapshot.chunks},
        )

    consumer = VideoParseConsumer(
        queue,
        VideoParseConsumerHandlers(prepare, understand, refine, index),
        worker_id="one-process",
        lease_duration=timedelta(seconds=5),
        retry_initial_delay=timedelta(milliseconds=10),
        retry_max_delay=timedelta(seconds=1),
        poll_interval=timedelta(milliseconds=10),
        max_concurrent_handlers=2,
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
