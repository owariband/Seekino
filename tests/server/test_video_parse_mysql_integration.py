from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import ChunkParseContent, ChunkParseTask, IntervalParseTask, VideoParseTask
from server.model.video import Series, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse_persistence import VideoParseLeaseLostError, VideoParsePersistenceService
from server.utils.database import serialized_transaction
from tests.server.test_video_parse_queue import NOW, _prepare, _task
from tests.server.test_video_parse_repository import _refinement_inputs


@pytest.fixture
def mysql_capacity():
    database_url = os.getenv("SEEKINO_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SEEKINO_TEST_DATABASE_URL is not configured")
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


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_mysql_serialized_transaction_holds_mutex_through_transaction_boundary(mysql_capacity, outcome):
    engine, factory, create_task, _connections = mysql_capacity
    task_id = create_task()
    name = f"transaction-boundary-{uuid4()}"
    marker = f"pause-{uuid4()}"
    boundary_entered, continue_boundary = Event(), Event()
    lock_connections = []

    def pause_boundary(connection):
        if connection.info.pop(marker, False):
            boundary_entered.set()
            assert continue_boundary.wait(5), "test did not release the transaction boundary"

    def record_lock_connection(connection, _cursor, statement, _parameters, _context, _many):
        if "GET_LOCK(" in statement:
            lock_connections.append(connection.connection.driver_connection.thread_id())

    def hold_transaction():
        try:
            with serialized_transaction(factory, name) as session:
                assert session is not None
                parent = session.get(VideoParseTask, task_id)
                parent.last_error = {"message": "uncommitted business change"}
                session.flush()
                session.connection().info[marker] = True
                if outcome == "rollback":
                    raise ValueError("simulated business failure")
        except ValueError as error:
            assert outcome == "rollback" and str(error) == "simulated business failure"

    event.listen(engine, outcome, pause_boundary)
    event.listen(engine, "after_cursor_execute", record_lock_connection)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold_transaction)
            try:
                assert boundary_entered.wait(5), "holder never reached commit/rollback"
                # SQLAlchemy emits this event before the DBAPI transaction
                # boundary. A different connection must still see the mutex.
                with serialized_transaction(factory, name) as contender:
                    assert contender is None
                assert len(set(lock_connections)) == 2
                with factory() as observer:
                    assert observer.get(VideoParseTask, task_id).last_error is None
            finally:
                continue_boundary.set()
            holder.result(timeout=5)
        with serialized_transaction(factory, name) as resumed:
            assert resumed is not None
            saved = resumed.get(VideoParseTask, task_id).last_error
            assert saved == ({"message": "uncommitted business change"} if outcome == "commit" else None)
    finally:
        continue_boundary.set()
        event.remove(engine, outcome, pause_boundary)
        event.remove(engine, "after_cursor_execute", record_lock_connection)


def test_mysql_serialized_transaction_releases_mutex_if_first_commit_fails(mysql_capacity):
    engine, factory, _create_task, _connections = mysql_capacity
    name = f"acquisition-commit-error-{uuid4()}"
    acquired_name = []
    releases = []
    pending_failure = False

    def record_statement(_connection, _cursor, statement, parameters, _context, _many):
        nonlocal pending_failure
        if "GET_LOCK(" in statement and not acquired_name:
            acquired_name.append(parameters["name"])
            pending_failure = True
        elif "RELEASE_LOCK(" in statement:
            releases.append(parameters["name"])

    def fail_first_commit(_connection):
        nonlocal pending_failure
        if pending_failure:
            pending_failure = False
            raise RuntimeError("simulated first commit failure")

    event.listen(engine, "after_cursor_execute", record_statement)
    event.listen(engine, "commit", fail_first_commit)
    try:
        with pytest.raises(RuntimeError, match="simulated first commit failure"):
            with serialized_transaction(factory, name):
                pytest.fail("business transaction must not start after acquisition commit fails")
        assert releases == acquired_name and len(acquired_name) == 1
        # Query lock ownership directly: a pooled connection can reacquire its
        # own leaked MySQL lock, so reacquisition alone cannot prove cleanup.
        with engine.connect() as observer:
            assert observer.scalar(text("SELECT IS_FREE_LOCK(:name)"), {"name": acquired_name[0]}) == 1
        with serialized_transaction(factory, name) as resumed:
            assert resumed is not None
    finally:
        event.remove(engine, "commit", fail_first_commit)
        event.remove(engine, "after_cursor_execute", record_statement)


@pytest.mark.parametrize("lost_response", ["GET_LOCK", "RELEASE_LOCK"])
def test_mysql_serialized_transaction_invalidates_connection_after_lost_mutex_response(mysql_capacity, lost_response):
    engine, factory, create_task, _connections = mysql_capacity
    task_id = create_task()
    name = f"lost-mutex-response-{uuid4()}"
    lost_connection = []
    invalidated = []
    lock_name = []

    def lose_response(connection, _cursor, statement, parameters, _context, _many):
        if f"{lost_response}(" in statement and not lost_connection:
            # The real server statement has executed. Simulate losing its
            # response before serialized_transaction can inspect the result.
            lost_connection.append(connection.connection.driver_connection.thread_id())
            lock_name.append(parameters["name"])
            raise RuntimeError("simulated lost mutex response")

    def record_invalidation(connection, _record, _error):
        invalidated.append(connection.thread_id())

    event.listen(engine, "after_cursor_execute", lose_response)
    event.listen(engine, "invalidate", record_invalidation)
    try:
        with pytest.raises(RuntimeError, match="simulated lost mutex response"):
            with serialized_transaction(factory, name) as session:
                assert lost_response == "RELEASE_LOCK" and session is not None
                session.get(VideoParseTask, task_id).last_error = {"message": "committed before release"}
        assert invalidated == lost_connection and len(lost_connection) == 1
        with engine.connect() as observer:
            assert observer.connection.driver_connection.thread_id() != lost_connection[0]
            # Allow the server to process the invalidated socket's disconnect;
            # a new physical connection must acquire the mutex within one second.
            assert observer.scalar(text("SELECT GET_LOCK(:name, 1)"), {"name": lock_name[0]}) == 1
            assert observer.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name[0]}) == 1
            assert observer.scalar(text("SELECT IS_FREE_LOCK(:name)"), {"name": lock_name[0]}) == 1
            observer.commit()
        with serialized_transaction(factory, name) as resumed:
            assert resumed is not None
            assert resumed.get(VideoParseTask, task_id).last_error == (
                {"message": "committed before release"} if lost_response == "RELEASE_LOCK" else None
            )
    finally:
        event.remove(engine, "invalidate", record_invalidation)
        event.remove(engine, "after_cursor_execute", lose_response)


def test_mysql_three_video_admissions_are_atomic_and_recovery_precedes_new_work(mysql_capacity):
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
        assert sum(row.capacity_acquired for row in rows) == 3
        initial_capacity = {row.id: row.capacity_acquired for row in rows}
        assert {row.id for row in rows if row.capacity_acquired} == {lease.video_parse_task_id for lease in admitted}
    assert len(connections) >= 2

    expired_at = NOW + timedelta(minutes=2)
    # Concurrent reapers must release each coordinator once without charging business retries.
    _race(lambda worker, _attempt: queues[worker].reap_expired_videos(
        now=expired_at,
    ), attempts=1)
    resumed = [lease for lease in _race(lambda worker, attempt: queues[worker].claim_video(
        f"recovery-{worker}-{attempt}", lease_duration=timedelta(minutes=1), now=expired_at,
    ), attempts=8) if lease is not None]
    assert {lease.video_parse_task_id for lease in resumed} == {lease.video_parse_task_id for lease in admitted}
    with factory() as session:
        for task_id in task_ids:
            parent = session.get(VideoParseTask, task_id)
            assert parent.capacity_acquired is initial_capacity[task_id]
            assert parent.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0
        assert all(lease.attempt_count == 2 for lease in resumed)

    # Stopping a coordinator preserves its unfinished Video's capacity; a new
    # Consumer resumes it before accepting any of the three waiting Videos.
    stopped = resumed[0]
    assert queues[0].release_execution(stopped, now=expired_at)
    with factory() as session:
        assert session.get(VideoParseTask, stopped.video_parse_task_id).capacity_acquired is True
    recovered = queues[1].claim_video("normal-stop-recovery", lease_duration=timedelta(minutes=1), now=expired_at)
    assert recovered is not None and recovered.video_parse_task_id == stopped.video_parse_task_id
    assert recovered.lease_version > stopped.lease_version
    assert queues[2].claim_video("still-full", lease_duration=timedelta(minutes=1), now=expired_at) is None

    repo = VideoParsePersistenceService(factory)
    physical = repo.ensure_video_chunks(recovered, [(0, 0, 10000)], now=expired_at)
    repo.ensure_parse_tasks(recovered, [physical[0].video_chunk_id], [(0, 1)], now=expired_at)
    repo.activate_video_task(recovered, now=expired_at)
    parse = queues[0].claim_chunk("finish-parse", video_parse_task_id=recovered.video_parse_task_id,
        lease_duration=timedelta(minutes=1), now=expired_at)
    repo.commit_chunk_result(parse, content={"persons": [], "timeline": []}, now=expired_at)
    refine = queues[0].claim_interval_refine("finish-refine", video_parse_task_id=recovered.video_parse_task_id,
        lease_duration=timedelta(minutes=1), now=expired_at)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=expired_at),
        interval_content={"updates": []}, member_updates={}, now=expired_at)
    index = queues[0].claim_chunk_index("finish-index", video_parse_task_id=recovered.video_parse_task_id,
        lease_duration=timedelta(minutes=1), now=expired_at)
    snapshot = repo.load_chunk_indexing_snapshot(index, now=expired_at)
    assert snapshot.target_index_generation == recovered.parse_config["indexing"]["index_generation"]
    repo.save_index_embeddings(index, index.chunk_parse_task_id, snapshot.chunk.content_version,
        {"semantic": {"vector": [1.0]}}, receipt={
            "parse_task_id": recovered.video_parse_task_id, "video_chunk_id": index.video_chunk_id,
            "content_version": snapshot.chunk.content_version, "index_generation": snapshot.target_index_generation,
        }, now=expired_at)
    repo.complete_chunk_indexing(index, snapshot.chunk.content_version, snapshot.target_index_generation, now=expired_at)
    # Independent maintenance also finishes a Video whose coordinator exits.
    queues[3].reap_expired(now=expired_at)
    with factory() as session:
        completed = session.get(VideoParseTask, recovered.video_parse_task_id)
        assert completed.status == "COMPLETE" and completed.capacity_acquired is False
        assert completed.target_index_generation == completed.parse_config["indexing"]["index_generation"] == snapshot.target_index_generation
        assert session.get(ChunkParseTask, index.chunk_parse_task_id).indexed_content_version == snapshot.chunk.content_version
    replacement = queues[4].claim_video("new-admission", lease_duration=timedelta(minutes=1), now=expired_at)
    assert replacement is not None and initial_capacity[replacement.video_parse_task_id] is False
    with factory() as session:
        rows = session.scalars(select(VideoParseTask).where(VideoParseTask.id.in_(task_ids))).all()
        assert sum(row.capacity_acquired for row in rows) == 3


def test_mysql_concurrent_consumers_share_independent_phase_capacity(mysql_capacity):
    _engine, factory, create_task, connections = mysql_capacity
    # More than ten eligible candidates in each phase proves the limits, while
    # overlapping Refine windows retain the production dependency topology.
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
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

    queues = [MySqlVideoParseQueue(factory, video_concurrency=3) for _ in range(6)]

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


@pytest.mark.parametrize("scenario", ["overlap", "above_target"])
def test_mysql_voiceprint_registration_rechecks_racing_prechecks(mysql_capacity, scenario):
    from server.constants.parse import VOICEPRINT_REFS_PER_SPEAKER
    from server.service.video_parse_persistence import VoiceprintRegistrationRejectedError

    _engine, factory, create_task, connections = mysql_capacity
    task_id = create_task(chunk_count=2)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo, parent, _prepared = _prepare(factory, queue, task_id, chunk_count=2)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "voiceprint": {
            "max_tool_calls": 32, "execution_timeout_seconds": 120.0,
        }}
    names = ["A", "B"] if scenario == "overlap" else ["A"] * (VOICEPRINT_REFS_PER_SPEAKER + 1)
    timeline = [
        {"event_type": "DIALOGUE", "speaker": name,
         "start_seconds": 100 + index * 1400, "end_seconds": 1100 + index * 1400,
         "description": "independently attributed single-speaker dialogue",
         "participants": [name], "visible_persons": [],
         "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": f"line {index}"}]}
        for index, name in enumerate(names)
    ]
    if scenario == "overlap":
        timeline[1].update(start_seconds=700, end_seconds=1700)
    child = queue.claim_chunk("voiceprint-source", video_parse_task_id=task_id,
        lease_duration=timedelta(minutes=5), now=NOW)
    assert child is not None
    repo.commit_chunk_result(child, content={"persons": sorted(set(names)), "timeline": timeline}, now=NOW)
    remaining = queue.claim_chunk("voiceprint-empty", video_parse_task_id=task_id,
        lease_duration=timedelta(minutes=5), now=NOW)
    assert remaining is not None
    repo.commit_chunk_result(remaining, content={"persons": [], "timeline": []}, now=NOW)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert len(snapshot["member_contents"]) == 2
    arguments = [
        {"source_ref": source_ref, "speaker": source["speaker"],
         "start_ms": source["start_ms"] + 100, "end_ms": source["end_ms"] - 100,
         "model": "campplus", "encoder_revision": "v1.0.0"}
        for source_ref, source in sorted(snapshot["sources"].items(), key=lambda item: item[1]["start_ms"])
    ]

    def register(values):
        return repo.register_voiceprint(parent, **values,
            selection_basis="reliable independent identity evidence", confidence=0.9,
            vector=[1.0] + [0.0] * 191, pcm_sha256=format(values["start_ms"], "064x"),
            duration_ms=values["end_ms"] - values["start_ms"], now=NOW)

    for values in arguments[:-2]:
        register(values)
    prechecked = Barrier(2)

    def race(worker, _attempt):
        values = arguments[-2 + worker]
        assert repo.precheck_voiceprint_registration(parent, **values, now=NOW) is None
        prechecked.wait(timeout=10)
        try:
            return register(values)
        except VoiceprintRegistrationRejectedError as error:
            return error

    results = _race(race, workers=2, attempts=1)
    rejected = [result for result in results if isinstance(result, VoiceprintRegistrationRejectedError)]
    if scenario == "overlap":
        assert sum(isinstance(result, dict) for result in results) == 1
        assert len(rejected) == 1
        assert rejected[0].code == "VOICEPRINT_REFERENCE_OVERLAP"
    else:
        assert len(rejected) == 0
        assert all(isinstance(result, dict) and result["added"] for result in results)
        assert sorted(result["refs_count"] for result in results) == [
            VOICEPRINT_REFS_PER_SPEAKER, VOICEPRINT_REFS_PER_SPEAKER + 1,
        ]
        assert all(result["target_refs_count"] == VOICEPRINT_REFS_PER_SPEAKER for result in results)
    with factory() as session:
        voiceprint = session.get(VideoParseTask, task_id).detail["voiceprint"]
        references = [reference for record in voiceprint["voiceprintList"] for reference in record["references"]]
        assert len(references) == (1 if scenario == "overlap" else VOICEPRINT_REFS_PER_SPEAKER + 1)
        assert voiceprint["ready"] is False
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
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
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
    assert old.execution_deadline == NOW + timedelta(seconds=5)
    assert heartbeat(old, lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=4))
    model = IntervalParseTask if phase == "refine" else ChunkParseTask
    task_key = old.interval_parse_task_id if phase == "refine" else old.chunk_parse_task_id
    with factory() as session:
        child = session.get(model, task_key)
        assert child.start_time == NOW
        assert child.lease_expire_time == child.start_time + timedelta(seconds=5) == old.execution_deadline

    at_deadline = NOW + timedelta(seconds=5)
    queues = [MySqlVideoParseQueue(factory, video_concurrency=3) for _ in range(6)]

    def expire(worker, _attempt):
        if worker % 2:
            method = queues[worker].heartbeat_interval if phase == "refine" else queues[worker].heartbeat_chunk
            assert not method(old, lease_duration=timedelta(minutes=1), now=at_deadline)
        else:
            queues[worker].reap_expired(now=at_deadline)

    _race(expire, attempts=1)
    # SKIP LOCKED may make every racing maintenance pass yield to a heartbeat;
    # the next independent scan must recover the row, without double charging.
    queue.reap_expired(now=at_deadline)
    with factory() as session:
        child = session.get(model, task_key)
        assert child.retry_count == 1
        assert child.lease_owner is None and child.lease_expire_time is None
        assert child.lease_version == old.lease_version
        retry_at = child.next_execute_time
    names = {"parse": "claim_chunk", "refine": "claim_interval_refine", "index": "claim_chunk_index"}
    replacements = [lease for lease in _race(lambda worker, _attempt: getattr(queues[worker], names[phase])(
        f"new-{worker}", lease_duration=timedelta(minutes=1), now=retry_at,
    ), attempts=1) if lease is not None]
    assert len(replacements) == 1
    new = replacements[0]
    assert new.lease_version > old.lease_version and new.retry_count == 1
    assert new.execution_deadline == retry_at + timedelta(seconds=5)
    assert new.execution_deadline > old.execution_deadline
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
        child = session.get(model, task_key)
        assert child.lease_version == new.lease_version and child.start_time == retry_at
        assert child.lease_expire_time == new.execution_deadline
        if phase != "parse":
            assert session.scalar(select(func.count()).select_from(ChunkParseContent).join(
                ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
            ).where(ChunkParseTask.video_task_id == task_id)) == 1
    assert len(connections) >= 2
