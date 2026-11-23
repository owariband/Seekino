"""Compare the two duration readers at the real UploadService entry point."""

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import av
import pytest
from sqlalchemy import func, select

from engine.common.media_probe import probe_video_duration_seconds
from server.database import create_session_factory
from server.model.task import VideoParseTask
from server.model.video import Video
from server.service.upload import UploadService
from tests.server.conftest import repository  # pytest fixture for an isolated SQLite database
from tests.server.test_upload_service import _upload_service


@pytest.fixture(scope="module")
def sample_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("long-audio") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "color=c=blue:s=64x32:r=125:d=8.008",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=9",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], check=True)
    with av.open(path) as container:
        audio = container.streams.audio[0]
        assert round(audio.duration * audio.time_base * 1000) == 9000
    return path.read_bytes()


def read_mp4_duration_with_pyav(path):
    with av.open(path) as container:
        video = container.streams.video[0]
        return round(video.duration * video.time_base * 1000)


@pytest.mark.parametrize("reader", [probe_video_duration_seconds, read_mp4_duration_with_pyav],
                         ids=["ffprobe", "pyav"])
def test_import_with_each_reader(reader, sample_bytes, repository, tmp_path, monkeypatch):
    engine, store, upload_repository, fixture_service = _upload_service(repository, tmp_path)
    service = UploadService(upload_repository, store, parse_defaults=fixture_service.parse_defaults,
                            probe_duration=reader)
    size = len(sample_bytes)
    sha256 = hashlib.sha256(sample_bytes).hexdigest()
    workspace = "local-default"
    if reader is read_mp4_duration_with_pyav:
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

    with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
        upload = service.create_session(
            tenant_id=workspace, series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=1, original_filename="sample.mp4", media_type="video/mp4",
            total_size_bytes=size, content_sha256=sha256, idempotency_key="probe-comparison",
        )
        service.upload_segment(
            upload.upload_session_id, 0, sample_bytes, tenant_id=workspace,
            checksum_sha256=sha256, start_byte=0, end_byte_exclusive=size, total_size_bytes=size,
        )
        completed = service.complete(upload.upload_session_id, tenant_id=workspace)

    commands = [Path(call.args[0][0]).name for call in popen.call_args_list]
    expected = ["ffprobe"] if reader is probe_video_duration_seconds else []
    assert commands == expected
    with create_session_factory(engine)() as session:
        video = session.get(Video, completed.video_id)
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        assert video.duration_ms == 8008
        assert store.resolve(video.source_uri).read_bytes() == sample_bytes
        assert session.scalar(select(func.count()).select_from(VideoParseTask)) == 1
        assert task.video_id == video.id and task.status == "PENDING"
        print(f"{reader.__name__}: {video.duration_ms} ms, {task.status}, Popen={commands}")
