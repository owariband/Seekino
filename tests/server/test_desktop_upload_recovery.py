from contextlib import closing
from hashlib import sha256
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from server.config.storage import desktop_storage
from tests.server.test_desktop_pipeline import _fixture_movie


def _crash_import(root, source, series_id, stage):
    script = """
import os, sys
from pathlib import Path
from server.desktop.runtime import open_library, PyAVMedia
from server.repository.upload import UploadRepository
from server.storage.artifact import LocalArtifactStore
def interrupt(*args, **kwargs):
    os._exit(17)
if sys.argv[4] == "probe":
    PyAVMedia.probe_duration = interrupt
elif sys.argv[4] == "commit":
    UploadRepository.complete_upload = interrupt
else:
    LocalArtifactStore.delete_upload_staging = interrupt
with open_library(sys.argv[1]) as library:
    library.import_file(Path(sys.argv[2]), series_id=sys.argv[3], episode_order=1, idempotency_key="same")
"""
    interrupted = subprocess.run([sys.executable, "-c", script, str(root), str(source), series_id, stage],
                                 capture_output=True, timeout=20)
    assert interrupted.returncode == 17, interrupted.stderr


@pytest.mark.parametrize("stage", ["probe", "commit"])
def test_interrupted_completion_resumes_as_soon_as_library_reopens(tmp_path, monkeypatch, stage):
    pytest.importorskip("sqlite_vec")
    from server.desktop.runtime import open_library
    from server.mq.video_parse_consumer import VideoParseConsumer

    source = tmp_path / "episode.mp4"
    root = tmp_path / "library"
    _fixture_movie(source)
    with open_library(root) as library:
        series = library.create_series("Interrupted import")

    _crash_import(root, source, series.series_id, stage)
    db_path = root / desktop_storage().database.file
    with closing(sqlite3.connect(db_path)) as connection:
        status, token = connection.execute("SELECT status,completion_token FROM upload_session").fetchone()
        assert status == "COMPLETING" and token
        assert connection.execute("SELECT COUNT(*) FROM video_parse_task").fetchone() == (0,)
    # This test ends at the committed upload. Leave its pending task for a real consumer.
    monkeypatch.setattr(VideoParseConsumer, "start", lambda self: None)
    with open_library(root) as library:
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT status,completion_token FROM upload_session").fetchone() == ("UPLOADING", None)
        upload = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="same")
        again = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="same")
        assert (again.video_id, again.video_parse_task_id) == (upload.video_id, upload.video_parse_task_id)
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT id FROM video_parse_task").fetchall() == [(upload.video_parse_task_id,)]


def test_reopen_removes_only_completed_staging_without_reimport(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    from server.constants.tenant import DEFAULT_LOCAL_TENANT_ID
    from server.desktop.runtime import open_library
    from server.mq.video_parse_consumer import VideoParseConsumer

    root, source = tmp_path / "library", tmp_path / "episode.mp4"
    _fixture_movie(source)
    with open_library(root) as library:
        series = library.create_series("Committed import")
        uploads = library._video.uploads
        incomplete = uploads.create_session(
            tenant_id=DEFAULT_LOCAL_TENANT_ID, series_id=series.series_id, episode_order=2,
            original_filename="partial.mp4", media_type="video/mp4", total_size_bytes=8, idempotency_key="pending",
        )
        uploads.upload_segment(incomplete.upload_session_id, 0, b"abcdefgh", tenant_id=DEFAULT_LOCAL_TENANT_ID,
                               checksum_sha256=sha256(b"abcdefgh").hexdigest(), start_byte=0,
                               end_byte_exclusive=8, total_size_bytes=8)
    _crash_import(root, source, series.series_id, "cleanup")
    with closing(sqlite3.connect(root / desktop_storage().database.file)) as connection:
        session_id, task_id = connection.execute(
            "SELECT id,initial_task_id FROM upload_session WHERE status='COMPLETED'"
        ).fetchone()
    staging = root / "media" / "production" / "staging" / "uploads"
    completed_dir = staging / session_id
    assert any(file.stat().st_size for file in completed_dir.rglob("*") if file.is_file())
    unknown, named, link = staging / str(uuid4()), staging / "notes", staging / str(uuid4())
    unknown.mkdir()
    named.mkdir()
    link.symlink_to(unknown, target_is_directory=True)
    monkeypatch.setattr(VideoParseConsumer, "start", lambda self: None)
    with open_library(root) as library:
        assert not completed_dir.exists()
        assert (staging / incomplete.upload_session_id).is_dir()
        assert unknown.is_dir() and named.is_dir() and link.is_symlink()
        assert library.task(task_id).task.video_parse_task_id == task_id
        # A retry also repairs leftover staging without waiting for a restart.
        completed_dir.mkdir()
        (completed_dir / "leftover").write_bytes(b"old upload")
        again = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="same")
        assert again.video_parse_task_id == task_id
        assert not completed_dir.exists()
