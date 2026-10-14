from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock

import pytest
from sqlalchemy import delete, event, func, select

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import ChunkParseContent, ChunkParseTask, IntervalParseTask, VideoParseTask
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.video_parse import VideoParseLeaseLostError
from tests.server.test_video_parse_queue import NOW, _prepare, _task
from tests.server.test_video_parse_repository import _refinement_inputs


@pytest.fixture
def mysql_capacity():
    database_url = os.getenv("ANIVAG_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ANIVAG_TEST_DATABASE_URL is not configured")
    engine = create_database_engine(database_url, environment=DatabaseEnvironment.TEST)
    assert engine.dialect.name == "mysql", "Capacity and lease races require real MySQL"
    create_schema(engine)
    factory = create_session_factory(engine)
    series_ids = []
    connections = set()
    mutex = Lock()

    def connection_checked_out(connection, _record, _proxy):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 3")
        with mutex:
            connections.add(connection.thread_id())

    event.listen(engine, "checkout", connection_checked_out)

    def create_task(chunk_count=1, batch_size=None):
        task_id = _task(factory, chunk_count=chunk_count, accepted=True,
                        batch_size=batch_size or chunk_count)
        with factory() as session:
            parent = session.get(VideoParseTask, task_id)
            series_ids.append(session.get(Video, parent.video_id).series_id)
        return task_id

    try:
        yield engine, factory, create_task, connections
    finally:
        with factory.begin() as session:
            session.execute(delete(Series).where(Series.id.in_(series_ids)))
        event.remove(engine, "checkout", connection_checked_out)
        engine.dispose()


def _race(operation, *, workers=6, attempts=20):
    start = Barrier(workers)

    def run(worker):
        start.wait(timeout=10)
        return [operation(worker, attempt) for attempt in range(attempts)]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return [value for values in executor.map(run, range(workers)) for value in values]


def test_mysql_three_slots_are_atomic_and_recovery_precedes_new_admission(mysql_capacity):
    _engine, factory, create_task, connections = mysql_capacity
    task_ids = [create_task() for _ in range(6)]
    queues = [MySqlVideoParseQueue(factory, video_concurrency=3) for _ in range(6)]
    leases = _race(lambda worker, attempt: queues[worker].claim_video(
        f"consumer-{worker}-{attempt}", lease_duration=timedelta(minutes=1), now=NOW,
    ), attempts=8)
    admitted = [lease for lease in leases if lease is not None]
    assert len(admitted) == 3
    assert len({lease.video_parse_task_id for lease in admitted}) == 3
    with factory() as session:
        rows = session.scalars(select(VideoParseTask).where(VideoParseTask.id.in_(task_ids))).all()
        assert sorted(row.scheduler_slot for row in rows if row.scheduler_slot is not None) == [1, 2, 3]
        initial_slots = {row.id: row.scheduler_slot for row in rows}
    assert len(connections) >= 2

    expired_at = NOW + timedelta(minutes=2)
    # Concurrent reapers must release each coordinator once without charging business retries.
    _race(lambda worker, _attempt: queues[worker].reap_expired_videos(
        retry_delay=timedelta(seconds=30), now=expired_at,
    ), attempts=1)
    resumed = [lease for lease in _race(lambda worker, attempt: queues[worker].claim_video(
        f"recovery-{worker}-{attempt}", lease_duration=timedelta(minutes=1), now=expired_at,
    ), attempts=8) if lease is not None]
    assert {lease.video_parse_task_id for lease in resumed} == {lease.video_parse_task_id for lease in admitted}
    with factory() as session:
        for task_id in task_ids:
            parent = session.get(VideoParseTask, task_id)
            assert parent.scheduler_slot == initial_slots[task_id]
            assert parent.prepare_retry_count == 0
        assert all(lease.attempt_count == 2 for lease in resumed)


def test_mysql_concurrent_consumers_share_independent_phase_capacity(mysql_capacity):
    _engine, factory, create_task, connections = mysql_capacity
    # More than ten eligible candidates in each phase proves the limits, while
    # overlapping Refine windows retain the production dependency topology.
    queue = MySqlVideoParseQueue(factory)
    task_ids = []
    for _ in range(3):
        task_id = create_task(chunk_count=52)
        task_ids.append(task_id)
        repo, _parent, _prepared = _prepare(factory, queue, task_id, chunk_count=52)
        for _ in range(32):
            lease = queue.claim_chunk("seed-parse", video_parse_task_id=task_id,
                lease_duration=timedelta(minutes=5), now=NOW)
            repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
        for _ in range(12):
            lease = queue.claim_interval_refine("seed-refine", video_parse_task_id=task_id,
                lease_duration=timedelta(minutes=5), now=NOW)
            repo.commit_interval_refinement(lease, **_refinement_inputs(repo, lease, now=NOW),
                interval_content={"updates": []}, member_updates={}, now=NOW)

    queues = [MySqlVideoParseQueue(factory) for _ in range(6)]

    def claim(worker, attempt):
        methods = (queues[worker].claim_chunk, queues[worker].claim_interval_refine,
                   queues[worker].claim_chunk_index)
        return tuple(methods[(worker + attempt + phase) % 3](
            f"consumer-{worker}-{attempt}", lease_duration=timedelta(minutes=5), now=NOW,
        ) for phase in range(3))

    results = _race(claim, attempts=32)
    leases = [lease for result in results for lease in result if lease is not None]
    assert len(leases) == 90
    with factory() as session:
        for task_id in task_ids:
            for phase in (1, 3):
                assert session.scalar(select(func.count()).select_from(ChunkParseTask).where(
                    ChunkParseTask.video_task_id == task_id, ChunkParseTask.phase == phase,
                    ChunkParseTask.status == "RUNNING",
                )) == 10
                assert session.scalar(select(func.count()).select_from(ChunkParseTask).where(
                    ChunkParseTask.video_task_id == task_id, ChunkParseTask.phase == phase,
                    ChunkParseTask.status == "QUEUED",
                )) > 0
            assert session.scalar(select(func.count()).select_from(IntervalParseTask).where(
                IntervalParseTask.video_task_id == task_id, IntervalParseTask.status == "REFINING",
            )) == 10
    assert len(connections) >= 2


@pytest.mark.parametrize("phase", ["parse", "refine", "index"])
def test_mysql_deadline_recovery_fences_heartbeat_and_late_commit(mysql_capacity, phase):
    _engine, factory, create_task, connections = mysql_capacity
    task_id = create_task()
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.parse_config = {**parent.parse_config, "execution": {
            **parent.parse_config["execution"], phase + "_execution_timeout_seconds": 5.0,
        }}
    queue = MySqlVideoParseQueue(factory)
    repo, _parent, _prepared = _prepare(factory, queue, task_id, chunk_count=1)
    old = queue.claim_chunk("old", lease_duration=timedelta(minutes=1), now=NOW)
    if phase != "parse":
        repo.commit_chunk_result(old, content={"persons": [], "timeline": []}, now=NOW)
        old = queue.claim_interval_refine("old", lease_duration=timedelta(minutes=1), now=NOW)
    if phase == "index":
        repo.commit_interval_refinement(old, **_refinement_inputs(repo, old, now=NOW),
            interval_content={"updates": []}, member_updates={}, now=NOW)
        old = queue.claim_chunk_index("old", lease_duration=timedelta(minutes=1), now=NOW)
    refinement = _refinement_inputs(repo, old, now=NOW) if phase == "refine" else None
    snapshot = repo.load_chunk_indexing_snapshot(old, now=NOW) if phase == "index" else None
    heartbeat = queue.heartbeat_interval if phase == "refine" else queue.heartbeat_chunk
    assert heartbeat(old, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=4))
    model = IntervalParseTask if phase == "refine" else ChunkParseTask
    task_key = old.interval_parse_task_id if phase == "refine" else old.chunk_parse_task_id
    with factory() as session:
        assert session.get(model, task_key).lease_expire_time == NOW + timedelta(seconds=5)

    at_deadline = NOW + timedelta(seconds=5)
    queues = [MySqlVideoParseQueue(factory) for _ in range(6)]

    def expire(worker, _attempt):
        if worker % 2:
            method = queues[worker].heartbeat_interval if phase == "refine" else queues[worker].heartbeat_chunk
            assert not method(old, lease_duration=timedelta(minutes=1), now=at_deadline)
        else:
            queues[worker].reap_expired(retry_delay=timedelta(0), now=at_deadline)

    _race(expire, attempts=1)
    # SKIP LOCKED may make every racing maintenance pass yield to a heartbeat;
    # the next independent scan must recover the row, without double charging.
    queue.reap_expired(retry_delay=timedelta(0), now=at_deadline)
    with factory() as session:
        child = session.get(model, task_key)
        assert child.retry_count == 1
        assert child.lease_owner is None and child.execution_deadline is None
        assert child.lease_version == old.lease_version
        retry_at = child.next_execute_time
    names = {"parse": "claim_chunk", "refine": "claim_interval_refine", "index": "claim_chunk_index"}
    replacements = [lease for lease in _race(lambda worker, _attempt: getattr(queues[worker], names[phase])(
        f"new-{worker}", lease_duration=timedelta(minutes=1), now=retry_at,
    ), attempts=1) if lease is not None]
    assert len(replacements) == 1
    new = replacements[0]
    assert new.lease_version > old.lease_version and new.retry_count == 1
    assert not heartbeat(old, lease_duration=timedelta(minutes=1), now=retry_at)
    with pytest.raises(VideoParseLeaseLostError):
        if phase == "parse":
            repo.commit_chunk_result(old, content={"persons": [], "timeline": []}, now=retry_at)
        elif phase == "refine":
            repo.commit_interval_refinement(old, **refinement, interval_content={"updates": []},
                member_updates={}, now=retry_at)
        else:
            repo.complete_chunk_indexing(old, snapshot.chunk.content_version,
                snapshot.target_index_generation, now=retry_at)
    with factory() as session:
        assert session.get(model, task_key).lease_version == new.lease_version
        if phase != "parse":
            assert session.scalar(select(func.count()).select_from(ChunkParseContent).join(
                ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
            ).where(ChunkParseTask.video_task_id == task_id)) == 1
    assert len(connections) >= 2
