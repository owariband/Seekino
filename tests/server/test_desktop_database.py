from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import sqlite3
from threading import Barrier, Event

import pytest
from sqlalchemy import event, select

from server.database import SchemaUpgradeRequiredError
from server.desktop.database import open_desktop_database
from server.model.parse_content import ChunkParseContent
from server.model.task import VideoParseTask
from server.mq.video_parse_queue import VideoParseQueue
from server.repository.job import ParseJobRepository
from server.service.parse_control import set_paused
from server.service.video_parse_persistence import VideoParseLeaseLostError
from tests.server.test_video_parse_queue import NOW, _prepare, _task


def test_library_owner_and_database_settings_survive_reopen(tmp_path):
    path = tmp_path / "Seekino" / "library.sqlite3"
    with closing(open_desktop_database(path)) as first:
        with pytest.raises(RuntimeError, match="already open"):
            open_desktop_database(path)
        task_id = _task(first.session_factory, chunk_count=1, accepted=True)
    with closing(open_desktop_database(path)) as reopened:
        with reopened.engine.connect() as first, reopened.engine.connect() as second:
            for connection in (first, second):
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        with reopened.session_factory() as reader:
            task = reader.get(VideoParseTask, task_id)
            assert task.lease_version == 0
            queue = VideoParseQueue(reopened.session_factory, video_concurrency=1)
            assert queue.claim_video("one", lease_duration=timedelta(minutes=1), now=NOW)
            reader.refresh(task)
            assert task.lease_version == 0  # The existing reader retains its WAL snapshot.
        with reopened.session_factory() as reader:
            assert reader.get(VideoParseTask, task_id).lease_version == 1


@pytest.mark.parametrize("foreign", [True, False])
def test_reject_unknown_library_without_changing_it(tmp_path, foreign):
    path = tmp_path / "library.db"
    if not foreign:
        open_desktop_database(path).close()
    with closing(sqlite3.connect(path)) as sqlite:
        if foreign:
            sqlite.execute("CREATE TABLE foreign_data(value TEXT)")
        else:
            sqlite.execute("PRAGMA user_version=999")
        mode = sqlite.execute("PRAGMA journal_mode").fetchone()
    with pytest.raises(SchemaUpgradeRequiredError):
        open_desktop_database(path)
    with closing(sqlite3.connect(path)) as sqlite:
        assert sqlite.execute("PRAGMA journal_mode").fetchone() == mode
        assert sqlite.execute("PRAGMA user_version").fetchone()[0] == (0 if foreign else 999)


@pytest.mark.parametrize("tasks", [1, 2])
def test_competing_connections_admit_only_one_video(tmp_path, tasks):
    with closing(open_desktop_database(tmp_path / "library.db")) as library:
        for _ in range(tasks):
            _task(library.session_factory, chunk_count=1, accepted=True)
        ready = Barrier(2)

        def claim(worker):
            queue = VideoParseQueue(library.session_factory, video_concurrency=1)
            ready.wait(timeout=5)
            return queue.claim_video(worker, lease_duration=timedelta(minutes=1), now=NOW)

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [lease for lease in pool.map(claim, ("left", "right")) if lease]
        assert len(winners) == 1
        with library.session_factory() as session:
            rows = session.scalars(select(VideoParseTask)).all()
            assert sum(task.capacity_acquired for task in rows) == 1
            assert sum(task.lease_version for task in rows) == 1


def test_pause_fences_a_chunk_commit_waiting_for_the_writer(tmp_path):
    with closing(open_desktop_database(tmp_path / "library.db")) as library:
        factory = library.session_factory
        queue = VideoParseQueue(factory, video_concurrency=1)
        task_id = _task(factory, chunk_count=1, accepted=True)
        persistence, parent, _ = _prepare(factory, queue, task_id, chunk_count=1)
        child = queue.claim_chunk("worker", lease_duration=timedelta(minutes=1), now=NOW)
        assert child
        blocked = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with ParseJobRepository(factory).transaction() as session:
                set_paused(session, task_id, tenant_id="mine", paused=True, expected_version=1,
                           expected_lease_version=parent.lease_version, now=NOW)
                @event.listens_for(library.engine, "before_cursor_execute")
                def writer_attempt(_conn, _cursor, statement, _params, _context, _many):
                    if statement == "BEGIN IMMEDIATE":
                        blocked.set()
                commit = pool.submit(persistence.commit_chunk_result, child,
                                     content={"persons": [], "timeline": []}, now=NOW)
                assert blocked.wait(timeout=5)
                assert not commit.done()
            with pytest.raises(VideoParseLeaseLostError):
                commit.result(timeout=5)
        with factory() as session:
            assert session.scalar(select(ChunkParseContent)) is None
        assert queue.claim_video("next", lease_duration=timedelta(minutes=1), now=NOW) is None


def test_expired_lease_cannot_commit_after_waiting_for_the_writer(tmp_path, monkeypatch):
    from server.service import video_parse_persistence

    with closing(open_desktop_database(tmp_path / "library.db")) as library:
        factory = library.session_factory
        queue = VideoParseQueue(factory, video_concurrency=1)
        task_id = _task(factory, chunk_count=1, accepted=True)
        persistence, _, _ = _prepare(factory, queue, task_id, chunk_count=1)
        child = queue.claim_chunk("worker", lease_duration=timedelta(seconds=1), now=NOW)
        blocked = Event()
        clock = NOW
        monkeypatch.setattr(video_parse_persistence, "now_utc", lambda: clock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin():
                @event.listens_for(library.engine, "before_cursor_execute")
                def writer_attempt(_conn, _cursor, statement, _params, _context, _many):
                    if statement == "BEGIN IMMEDIATE":
                        blocked.set()
                commit = pool.submit(persistence.commit_chunk_result, child,
                                     content={"persons": [], "timeline": []})
                assert blocked.wait(timeout=5)
                clock += timedelta(seconds=2)
            with pytest.raises(VideoParseLeaseLostError):
                commit.result(timeout=5)


def test_host_exit_releases_the_lock_and_expired_leases_can_be_reclaimed(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "library.db"
    with closing(open_desktop_database(path)) as library:
        task_id = _task(library.session_factory, chunk_count=1, accepted=True)
    script = """
import os, sys
from datetime import datetime, timedelta
from pathlib import Path
from server.desktop.database import open_desktop_database
from server.mq.video_parse_queue import VideoParseQueue
now = datetime.fromisoformat(sys.argv[2])
db = open_desktop_database(Path(sys.argv[1]))
queue = VideoParseQueue(db.session_factory, video_concurrency=1)
lease = queue.claim_video("interrupted", lease_duration=timedelta(seconds=1), now=now)
assert lease is not None and lease.lease_version == 1
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path), NOW.isoformat()], check=True, timeout=25)
    with closing(open_desktop_database(path)) as library:
        queue = VideoParseQueue(library.session_factory, video_concurrency=1)
        later = NOW + timedelta(minutes=1)
        assert queue.reap_expired_videos(now=later) == 1
        lease = queue.claim_video("replacement", lease_duration=timedelta(minutes=1),
                                  now=later + timedelta(minutes=5))
        assert lease.video_parse_task_id == task_id
        assert lease.lease_version > 1


def test_concurrent_reparse_retains_one_new_version(tmp_path):
    from server.model.upload import UploadSession
    from server.model.video import Video
    from server.repository.upload import UploadConflictError, UploadRepository

    with closing(open_desktop_database(tmp_path / "library.db")) as library:
        factory = library.session_factory
        original_id = _task(factory, chunk_count=1, accepted=True)
        with factory.begin() as session:
            original = session.get(VideoParseTask, original_id)
            original.status = "COMPLETE"
            video = session.get(Video, original.video_id)
            session.add(UploadSession(
                id="upload-1", tenant_id="mine", series_id=video.series_id, video_id=video.id,
                initial_task_id=original.id, episode_order=1, upload_sequence=1, filename=video.filename,
                media_type="video/mp4", size_bytes=10, segment_size_bytes=10, segment_count=1,
                status="COMPLETED", idempotency_key="fixture", expire_time=NOW,
            ))
        repo = UploadRepository(factory)
        ready = Barrier(2)

        def reparse(_worker):
            ready.wait(timeout=5)
            try:
                with repo.transaction() as session:
                    return repo.reparse("upload-1", session=session, tenant_id="mine", expected_job_version=1,
                                        now=NOW).version
            except UploadConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert set(pool.map(reparse, range(2))) == {2, None}
        with factory() as session:
            assert list(session.scalars(select(VideoParseTask.version).order_by(VideoParseTask.version))) == [1, 2]
