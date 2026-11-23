from contextlib import closing
import sqlite3
import subprocess
import sys

import pytest

from server.config.storage import desktop_storage
from tests.server.test_desktop_pipeline import _fixture_movie


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

    script = """
import os, sys
from pathlib import Path
from server.desktop.runtime import open_library, PyAVMedia
from server.repository.upload import UploadRepository
def interrupt(*args, **kwargs):
    os._exit(17)
if sys.argv[4] == "probe":
    PyAVMedia.probe_duration = interrupt
else:
    UploadRepository.complete_upload = interrupt
with open_library(sys.argv[1]) as library:
    library.import_file(Path(sys.argv[2]), series_id=sys.argv[3], episode_order=1, idempotency_key="same")
"""
    interrupted = subprocess.run([sys.executable, "-c", script, str(root), str(source), series.series_id, stage],
                                 capture_output=True, timeout=20)
    assert interrupted.returncode == 17, interrupted.stderr
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
