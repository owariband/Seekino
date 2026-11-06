from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, text

from server.config.parse_profile import default_video_parse_v2_config
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.task import VideoParseTask
from server.model.video import Series, Video
from server.repository.job import ParseJobNotFoundError, ParseJobRepository
from tests.server.test_job_repository import _seed_video


@pytest.fixture(params=["sqlite", "mysql"])
def large_jobs(request):
    url = "sqlite+pysqlite:///:memory:"
    if request.param == "mysql":
        url = os.getenv("SEEKINO_TEST_DATABASE_URL")
        if not url:
            pytest.skip("SEEKINO_TEST_DATABASE_URL is not configured")
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    if request.param == "mysql":
        assert engine.dialect.name == "mysql"

        @event.listens_for(engine, "checkout")
        def small_sort_buffer(connection, _record, _proxy):
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION sort_buffer_size = 262144")

    create_schema(engine)
    factory = create_session_factory(engine)
    series_ids = []

    def seed(tenant="local-default"):
        jobs, video, at = _seed_video((engine, None), tenant_id=tenant)
        with factory() as session:
            series = session.get(Video, video).series_id
        series_ids.append(series)
        return jobs, video, series, at

    try:
        yield factory, seed, engine
    finally:
        with factory.begin() as session:
            session.execute(delete(Series).where(Series.id.in_(series_ids)))
        engine.dispose()


def _task(video, at, *, version, detail, status="PARSING", deleted=False):
    return VideoParseTask(
        id=str(uuid4()), video_id=video, pipeline_version="video-parse-v2",
        version=version, parse_config=default_video_parse_v2_config().model_dump(mode="json"), chunk_count=1, status=status, max_attempts=3,
        detail=detail, is_deleted=deleted, create_time=at, update_time=at,
    )


def test_large_detail_preserves_authorized_pagination_and_latest_version(large_jobs):
    factory, seed, engine = large_jobs
    jobs, video, series, at = seed()
    _, other_video, other_series, _ = seed("foreign")
    # Larger than the real 256 KiB incident; exercise several retained versions.
    detail = {"large_execution_history": "x" * (1024 * 1024)}
    records = [_task(video, at, version=i, detail=detail) for i in range(1, 5)]
    foreign = _task(other_video, at + 1, version=1, detail=detail)
    deleted = _task(video, at + 2, version=5, detail=detail, deleted=True)
    with factory.begin() as session:
        session.add_all([*records, foreign, deleted])
    expected = sorted((item.id for item in records), reverse=True)
    for offset in (0, 2, 4):
        page = jobs.list_video_parse_tasks_for_workspace("local-default", limit=2, offset=offset)
        assert [item.task.video_parse_task_id for item in page] == expected[offset:offset + 2]
        assert all(item.task.detail["large_execution_history"] == detail["large_execution_history"] for item in page)
    assert jobs.list_video_parse_tasks_for_workspace("foreign", series_id=series) == []
    assert jobs.list_video_parse_tasks_for_workspace("local-default", series_id=other_series) == []
    assert jobs.list_video_parse_tasks_for_workspace("local-default", statuses=("COMPLETE",)) == []
    assert len(jobs.list_video_parse_tasks_for_workspace("local-default", series_id=series, statuses=("PARSING",))) == 4
    for record in records:
        result = jobs.get_video_parse_task_detail(record.id, tenant_id="local-default")
        assert result.is_latest == (record.version == 4)
        with pytest.raises(ParseJobNotFoundError):
            jobs.get_video_parse_task_detail(record.id, tenant_id="foreign")
    if engine.dialect.name == "mysql":
        with factory() as session:
            assert session.scalar(text("SELECT @@sort_buffer_size")) == 262144


@pytest.mark.parametrize("runtime,active", [
    (None, False),
    ([], False),
    ({"prefix": None}, False),
    ({"prefix": []}, False),
    ({"prefix": {"lease_owner": None, "lease_expire_time_ms": 1001}}, False),
    ({"prefix": {"lease_owner": "worker", "lease_expire_time_ms": True}}, False),
    ({"prefix": {"lease_owner": "worker", "lease_expire_time_ms": "1001"}}, False),
    ({"prefix": {"lease_owner": "worker", "lease_expire_time_ms": 1000}}, False),
    ({"prefix": {"lease_owner": "worker", "lease_expire_time_ms": 1001}}, True),
    ({"prefix": {"lease_owner": "", "lease_expire_time_ms": 1001}}, True),
])
def test_projected_prefix_keeps_json_types_expiry_and_latest_version(large_jobs, runtime, active):
    factory, seed, _ = large_jobs
    jobs, video, series, at = seed()
    old = _task(video, at, version=1, detail={"_worker_runtime_v2": {
        "prefix": {"lease_owner": "old", "lease_expire_time_ms": 999999}}})
    current = _task(video, at, version=2, detail={
        "_worker_runtime_v2": runtime, "large_execution_history": "x" * (1024 * 1024)})
    deleted = _task(video, at, version=3, detail=old.detail, deleted=True)
    with factory.begin() as session:
        session.add_all([old, current, deleted])
    with factory() as session:
        assert jobs._active_prefix_max_episode_by_series(session, (series,), observed_at_ms=1000) == (
            {series: 1} if active else {})


def test_large_runtime_keeps_queue_candidates_and_claim_priority(large_jobs):
    from datetime import timedelta
    from server.mq.video_parse_queue import MySqlVideoParseQueue
    from server.utils.time import now_utc

    factory, seed, _ = large_jobs
    at = now_utc()
    records = []
    for status, acquired, accepted in [
        ("PARSING", True, True), ("PENDING", False, True),
        ("PARSING", False, True), ("PARSING", True, True),
        ("PARSING", True, False),
    ]:
        _, video, _, created = seed()
        record = _task(video, created, version=1, status=status, detail={
            "_worker_runtime_v2": {"accepted": accepted, "capacity_acquired": acquired,
                                   "model_calls": {"padding": "x" * (1024 * 1024)}}})
        record.next_execute_time = at
        records.append(record)
    with factory.begin() as session:
        session.add_all(records)
    queue = MySqlVideoParseQueue(factory, video_concurrency=10)
    acquired_ids = sorted([records[0].id, records[3].id])
    assert queue._parent_candidates("index", None) == acquired_ids
    queue._last_parent["index"] = acquired_ids[0]
    assert queue._parent_candidates("index", None) == acquired_ids[1:] + acquired_ids[:1]
    assert queue._parent_candidates("index", acquired_ids[0]) == acquired_ids[:1]
    expected = acquired_ids + [records[2].id, records[1].id]
    actual = []
    for _ in expected:
        lease = queue.claim_video("worker", lease_duration=timedelta(minutes=1), now=at)
        assert lease is not None
        actual.append(lease.video_parse_task_id)
    assert actual == expected
    assert queue.claim_video("worker", lease_duration=timedelta(minutes=1), now=at) is None
