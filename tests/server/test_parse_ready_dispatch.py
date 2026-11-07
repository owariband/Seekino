from datetime import timedelta

import pytest
from sqlalchemy import event

from server.database import create_session_factory
from server.model.task import VideoParseTask
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse_dispatch import EpisodeReadyTasks, ParseDispatch
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server.test_video_parse_queue import NOW, _prepare, _task
from tests.server.test_video_parse_repository import _finish_index, _refinement_inputs

DURATION = timedelta(minutes=5)
RESULT = {"persons": [], "timeline": []}


def _ready(factory, count):
    dispatch = ParseDispatch(factory, reconcile_interval=timedelta(minutes=1))
    queue = MySqlVideoParseQueue(factory, video_concurrency=1, dispatch=dispatch)
    task_id = _task(factory, chunk_count=count, accepted=True)
    _, parent, prepared = _prepare(factory, queue, task_id, chunk_count=count)
    return queue, VideoParsePersistenceService(factory, dispatch=dispatch), parent, prepared


def _refine(repo, lease, now=NOW):
    return repo.commit_interval_refinement(lease, **_refinement_inputs(repo, lease, now=now),
                                          interval_content={"updates": []}, member_updates={}, now=now)


def test_post_commit_ready_ids_open_batches_and_allow_mixed_stages(repository):
    engine, _ = repository
    factory = create_session_factory(engine)
    queue, repo, parent, prepared = _ready(factory, 12)
    claim = dict(lease_duration=DURATION, now=NOW)
    task_id = parent.video_parse_task_id
    for i in range(8):
        lease = queue.claim_chunk("parse", **claim)
        assert lease.chunk_parse_task_id == prepared.chunk_parse_task_ids[i]
        touched = repo.commit_chunk_result(lease, content=RESULT, now=NOW)
        queue.dispatch.after_commit(lease, EpisodeReadyTasks.understood, NOW, touched)  # duplicate delivery
    assert queue.dispatch.candidates(task_id, "parse", NOW)[1] == prepared.chunk_parse_task_ids[8:10]
    ninth = queue.claim_chunk("parse", **claim)
    repo.commit_chunk_result(ninth, content=RESULT, now=NOW)
    assert queue.dispatch.candidates(task_id, "parse", NOW)[1] == prepared.chunk_parse_task_ids[9:]
    window = queue.claim_interval_refine("refine", **claim)
    receipt = _refine(repo, window)
    assert receipt.ready_chunk_ids == prepared.chunk_parse_task_ids[:1]
    index = queue.claim_chunk_index("index", **claim)
    assert index.chunk_parse_task_id == receipt.ready_chunk_ids[0]
    _finish_index(repo, index, now=NOW)
    queue.dispatch.after_commit(window, EpisodeReadyTasks.refined, receipt.ready_chunk_ids, NOW)
    assert not queue.dispatch.candidates(task_id, "index", NOW)[1]
    with factory() as session:
        parent_row = session.get(VideoParseTask, task_id)
        assert "last_dispatch_time_ms" not in parent_row.detail["_worker_runtime_v2"].get("chunk_scheduler", {})


def test_rollback_never_opens_window_and_retry_wakes_only_when_due(repository):
    factory = create_session_factory(repository[0])
    queue, repo, parent, _ = _ready(factory, 1)
    claim = dict(lease_duration=DURATION, now=NOW)
    child = queue.claim_chunk("parse", **claim)

    def abort(_session):
        raise RuntimeError("rollback for test")

    event.listen(factory, "before_commit", abort, once=True)
    with pytest.raises(RuntimeError, match="rollback for test"):
        repo.commit_chunk_result(child, content=RESULT, now=NOW)
    assert queue.claim_interval_refine("refine", **claim) is None
    repo.commit_chunk_result(child, content=RESULT, now=NOW)
    window = queue.claim_interval_refine("refine", **claim)
    due = NOW + timedelta(seconds=7)
    assert queue.retry_interval(window, error="retry", retry_at=due, now=NOW)
    assert queue.dispatch.next_delay(NOW, 30) == 7
    assert queue.claim_interval_refine("early", **claim) is None
    again = queue.claim_interval_refine("due", lease_duration=DURATION, now=due)
    assert again.interval_parse_task_id == window.interval_parse_task_id
    assert again.lease_version > window.lease_version
    _refine(repo, again, now=due)
    index = queue.claim_chunk_index("index", lease_duration=DURATION, now=due)
    _finish_index(repo, index, now=due)
    assert repo.aggregate_task(parent, now=due).status == "COMPLETE"


def test_recovery_observes_commits_from_another_consumer(repository, monkeypatch):
    import server.service.video_parse_dispatch as module

    clock = [0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    factory = create_session_factory(repository[0])
    queue, _repo, parent, _ = _ready(factory, 1)
    claim = dict(lease_duration=DURATION, now=NOW)
    child = queue.claim_chunk("parse", **claim)
    # No notification crossed the process boundary.
    VideoParsePersistenceService(factory).commit_chunk_result(child, content=RESULT, now=NOW)
    assert queue.claim_interval_refine("first", **claim) is None
    clock[0] = 61
    assert queue.claim_interval_refine("recovered", **claim) is not None
    fresh = MySqlVideoParseQueue(factory, video_concurrency=1, dispatch=ParseDispatch(factory, reconcile_interval=DURATION))
    assert not fresh.dispatch.candidates(parent.video_parse_task_id, "refine", NOW)[1]


def test_rebuild_reopens_current_index_and_ignores_a_late_receipt(repository):
    factory = create_session_factory(repository[0])
    queue, repo, parent, prepared = _ready(factory, 1)
    claim = dict(lease_duration=DURATION, now=NOW)
    child = queue.claim_chunk("parse", **claim)
    repo.commit_chunk_result(child, content=RESULT, now=NOW)
    _refine(repo, queue.claim_interval_refine("refine", **claim))
    old = queue.claim_chunk_index("old-index", **claim)
    assert repo.request_index_rebuild(parent.video_parse_task_id, now=NOW) == prepared.chunk_parse_task_ids
    queue.dispatch.after_commit(old, EpisodeReadyTasks.lease_state, indexed_version=1)
    replacement = queue.claim_chunk_index("new-index", **claim)
    assert replacement.lease_version > old.lease_version
    _finish_index(repo, replacement, now=NOW)
    assert repo.aggregate_task(parent, now=NOW).status == "COMPLETE"


def test_refine_prefers_free_members_and_still_fills_overlap_slots(repository):
    factory = create_session_factory(repository[0])
    queue, repo, _parent, _ = _ready(factory, 9)
    claim = dict(lease_duration=DURATION, now=NOW)
    for _ in range(9):
        repo.commit_chunk_result(queue.claim_chunk("parse", **claim), content=RESULT, now=NOW)
    leases = [queue.claim_interval_refine("refine", **claim) for _ in range(4)]
    assert [lease.start_sequence_no for lease in leases] == [0, 3, 6, 1]


def test_pause_skips_recovery_aggregation_until_resumed(repository, monkeypatch):
    from server.mq import video_parse_queue as queue_module
    from server.service.parse_control import set_paused

    factory = create_session_factory(repository[0])
    queue, _, parent, _ = _ready(factory, 3)
    task_id = parent.video_parse_task_id
    visited = []
    aggregate = queue_module._aggregate_video_task
    def counted(session, key, *args, **kwargs):
        visited.append(key)
        return aggregate(session, key, *args, **kwargs)
    monkeypatch.setattr(queue_module, "_aggregate_video_task", counted)
    for paused in (True, False):
        with factory.begin() as session:
            row = session.get(VideoParseTask, task_id)
            set_paused(session, task_id, tenant_id="mine", paused=paused, expected_version=row.version,
                       expected_lease_version=row.lease_version, now=NOW)
        queue.reap_expired(now=NOW)
        assert visited == ([] if paused else [task_id])


def test_active_chunk_timing_never_fetches_content_and_late_receipt_reads_only_version(repository):
    engine, _ = repository
    queue, repo, _, _ = _ready(create_session_factory(engine), 1)
    lease = queue.claim_chunk("parse", lease_duration=DURATION, now=NOW)
    statements = []
    def observe(_conn, _cursor, sql, *_):
        if "from chunk_parse_content" in sql.lower():
            statements.append(sql.lower())
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert repo.record_execution_timing(lease, started_at_ms=123, elapsed_ms=100, now=NOW)
        assert statements == []
        repo.commit_chunk_result(lease, content=RESULT, now=NOW)
        statements.clear()
        assert repo.record_execution_timing(lease, started_at_ms=123, elapsed_ms=999,
                                           finished=True, outcome="completed", now=NOW)
        assert len(statements) == 1 and "json_extract" in statements[0]
        assert "chunk_parse_content.content," not in statements[0]
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_understand_uses_one_snapshot_per_bound_lease(repository):
    import asyncio
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    from types import SimpleNamespace

    from server.service.video_parse import VideoParseRuntimeService

    queue, repo, _, _ = _ready(create_session_factory(repository[0]), 2)
    leases = [queue.claim_chunk("parse", lease_duration=DURATION, now=NOW) for _ in range(2)]
    snapshots = {lease.chunk_parse_task_id: repo.load_chunk_execution(lease, now=NOW) for lease in leases}
    reads = []
    def load(lease):
        reads.append(lease)
        return snapshots[lease.chunk_parse_task_id]

    @asynccontextmanager
    async def model_stage(*_args, **_kwargs):
        yield

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._chunk_binding = ContextVar("snapshot-test", default=None)
    runtime._persistence = SimpleNamespace(load_chunk_execution=load)
    runtime._model_stage = model_stage
    async def invoke(chunk_id):
        first = await runtime.load_chunk_execution(chunk_id)
        assert first == await runtime.load_chunk_execution(chunk_id)
        assert first.chunk_parse_task_id == chunk_id
    runtime.agent = SimpleNamespace(chunk_workflow=SimpleNamespace(InvokeUnderstanding=invoke))
    for lease in leases:
        asyncio.run(runtime.understand(lease))
    assert reads == leases
