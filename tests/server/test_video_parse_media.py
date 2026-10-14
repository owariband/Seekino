from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from engine.common.media_probe import probe_video_duration_seconds

from server.service.video_parse_media import (
    MediaLeaseIdentity,
    VideoParseMediaService,
)


def _identity(*, lease_version=1):
    return MediaLeaseIdentity(str(uuid4()), str(uuid4()), lease_version)


def _fake_runner(commands):
    def run(command):
        commands.append(tuple(command))
        output = command[-1]
        with open(output, "wb") as stream:
            stream.write(b"media")

    return run


def _hold_workspace_in_another_process(base, source, identity, ready):
    service = VideoParseMediaService(base, command_runner=_fake_runner([]))
    with service.prepare_video_segment(
        identity, source_video=source, start_seconds=0,
        end_seconds=1000, max_height=360,
    ) as video:
        ready.send(str(video.parent))
        ready.recv()


def test_media_is_isolated_by_task_and_lease_and_always_cleaned(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands),
        probe_duration=lambda _path: 10_000,
    )
    identity = _identity(lease_version=7)

    with service.prepare_chunk(
        identity,
        source_video=source,
        start_seconds=10_000,
        end_seconds=20_000,
        visual_mode="ordered_frames",
        frame_fps=0.2,
        max_frames=3,
        max_height=720,
        ocr_enabled=False,
    ) as media:
        expected = (
            service.base_directory
            / identity.video_parse_task_id
            / f"{identity.execution_kind}-{identity.execution_task_id}"
            / "lease-00000007"
        )
        assert media.video_path.parent == expected
        assert media.audio_path.parent == expected
        assert len(media.frames) == 2
        assert all(10_000 <= timestamp < 20_000 for timestamp, _ in media.frames)
        assert json.loads((expected / ".lease.json").read_text())["lease_version"] == 7

    assert not expected.exists()
    assert len(commands) == 4  # clip, audio and two uniformly sampled frames
    assert commands[0][commands[0].index("-ss") + 1] == "10.000"
    assert commands[0][commands[0].index("-t") + 1] == "10.000"
    assert [command[command.index("-ss") + 1] for command in commands[2:]] == [
        "2.500000", "7.500000",
    ]


def test_execution_kind_separates_chunk_and_prefix_lease_namespaces(tmp_path):
    service = VideoParseMediaService(tmp_path / "work")
    video_id, execution_id = str(uuid4()), str(uuid4())
    chunk = MediaLeaseIdentity(
        video_id, execution_id, 1, execution_kind="chunk"
    )
    prefix = MediaLeaseIdentity(
        video_id, execution_id, 1, execution_kind="prefix"
    )

    chunk_directory = service._create_workspace(chunk)
    prefix_directory = service._create_workspace(prefix)

    assert chunk_directory != prefix_directory
    assert service._read_identity(chunk_directory / ".lease.json") == chunk
    assert service._read_identity(prefix_directory / ".lease.json") == prefix


def test_video_mode_only_extracts_frames_when_ocr_needs_them(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands),
        probe_duration=lambda _path: 5000,
    )

    with service.prepare_chunk(
        _identity(), source_video=source,
        start_seconds=0, end_seconds=5000, visual_mode="video",
        frame_fps=1, max_frames=2, max_height=360, ocr_enabled=False,
    ) as media:
        assert media.frames == ()
    assert len(commands) == 2

    with service.prepare_chunk(
        _identity(), source_video=source,
        start_seconds=0, end_seconds=5000, visual_mode="video",
        frame_fps=1, max_frames=2, max_height=360, ocr_enabled=True,
    ) as media:
        assert len(media.frames) == 2


def test_indexing_segment_uses_the_same_lease_isolation_without_extra_media(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands)
    )
    identity = _identity(lease_version=4)

    with service.prepare_video_segment(
        identity,
        source_video=source,
        start_seconds=5000,
        end_seconds=9000,
        max_height=480,
    ) as video:
        assert video.read_bytes() == b"media"
        assert "lease-00000004" in video.parts
    assert len(commands) == 1
    assert not video.exists()


def test_cancellation_check_cleans_partially_materialized_workspace(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner([])
    )
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        with service.prepare_chunk(
            _identity(), source_video=source,
            start_seconds=0, end_seconds=5, visual_mode="video",
            frame_fps=1, max_frames=2, max_height=360, ocr_enabled=False,
            check_active=check,
        ):
            raise AssertionError("unreachable")
    assert list(service.base_directory.rglob(".lease.json")) == []


def test_reaper_deletes_only_old_inactive_lease_directories(tmp_path):
    now = 10_000.0
    service = VideoParseMediaService(tmp_path / "work", clock=lambda: now)
    old_inactive = _identity(lease_version=1)
    old_active = _identity(lease_version=2)
    fresh = _identity(lease_version=3)
    directories = {
        identity: service._create_workspace(identity)
        for identity in (old_inactive, old_active, fresh)
    }
    for identity in (old_inactive, old_active):
        marker = directories[identity] / ".lease.json"
        os.utime(marker, (now - 100, now - 100))

    assert service.reap_stale(
        older_than_seconds=60,
        is_active=lambda identity: identity == old_active,
    ) == 1
    assert not directories[old_inactive].exists()
    assert directories[old_active].exists()
    assert directories[fresh].exists()


def test_reaper_uses_tenant_identity_when_marker_is_missing_or_corrupt(tmp_path):
    now = 10_000.0
    service = VideoParseMediaService(tmp_path / "work", clock=lambda: now)
    markerless = _identity(lease_version=4)
    corrupt = _identity(lease_version=5)
    markerless_directory = service._create_workspace(markerless)
    corrupt_directory = service._create_workspace(corrupt)
    (markerless_directory / ".lease.json").unlink()
    (corrupt_directory / ".lease.json").write_text("{not-json")
    os.utime(markerless_directory, (now - 100, now - 100))
    os.utime(corrupt_directory / ".lease.json", (now - 100, now - 100))

    seen = []
    assert service.reap_stale(
        older_than_seconds=60,
        is_active=lambda identity: seen.append(identity) or False,
    ) == 2
    assert set(seen) == {markerless, corrupt}
    assert not markerless_directory.exists()
    assert not corrupt_directory.exists()


def test_reaper_finishes_old_renamed_tombstone(tmp_path):
    now = 10_000.0
    service = VideoParseMediaService(tmp_path / "work", clock=lambda: now)
    directory = service._create_workspace(_identity())
    tombstone = directory.with_name(f".reaping-{directory.name}-interrupted")
    directory.replace(tombstone)
    os.utime(tombstone, (now - 100, now - 100))

    assert service.reap_stale(
        older_than_seconds=60,
        is_active=lambda _identity: pytest.fail("tombstones are already fenced"),
    ) == 1
    assert not tombstone.exists()


def test_reaper_keeps_another_process_media_until_that_process_really_exits(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = VideoParseMediaService(tmp_path / "work")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_hold_workspace_in_another_process,
        args=(service.base_directory, source, _identity(), child),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(10), "media process did not prepare its workspace"
        directory = Path(parent.recv())
        os.utime(directory / ".lease.json", (0, 0))
        # The durable lease has been revoked, but a live provider still uses
        # these files. Even a different Consumer must retain them.
        assert service.reap_stale(older_than_seconds=1, is_active=lambda _identity: False) == 0
        assert (directory / "visual.mp4").is_file()
        process.terminate()
        process.join(5)
        assert not process.is_alive()
        # Process exit releases the lock without running context cleanup.
        assert service.reap_stale(older_than_seconds=1, is_active=lambda _identity: False) == 1
        assert not directory.exists()
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(5)
        process.close()


def test_workspace_lock_is_not_inherited_by_an_unrelated_subprocess(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = VideoParseMediaService(tmp_path / "work", command_runner=_fake_runner([]))
    descriptors = []
    original = os.set_inheritable

    def capture(descriptor, inheritable):
        descriptors.append(descriptor)
        original(descriptor, inheritable)

    monkeypatch.setattr(os, "set_inheritable", capture)
    with service.prepare_video_segment(
        _identity(), source_video=source, start_seconds=0,
        end_seconds=1000, max_height=360,
    ):
        descriptor = descriptors[-1]
        assert not os.get_inheritable(descriptor)
        stat = os.fstat(descriptor)
        result = subprocess.run([
            sys.executable, "-c",
            "import os,sys\n"
            "try: stat=os.fstat(int(sys.argv[1]))\n"
            "except OSError: pass\n"
            "else: assert (stat.st_dev,stat.st_ino) != (int(sys.argv[2]),int(sys.argv[3]))\n",
            str(descriptor), str(stat.st_dev), str(stat.st_ino),
        ], close_fds=False, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr


def test_workspace_marker_is_published_atomically(tmp_path):
    service = VideoParseMediaService(tmp_path / "work")
    identity = _identity()

    directory = service._create_workspace(identity)

    assert json.loads((directory / ".lease.json").read_text()) == {
        "video_parse_task_id": identity.video_parse_task_id,
        "execution_task_id": identity.execution_task_id,
        "execution_kind": identity.execution_kind,
        "lease_version": identity.lease_version,
        "created_at": pytest.approx(service._clock()),
    }
    assert list(directory.glob("*.tmp")) == []


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real media regression requires FFmpeg and FFprobe",
)
def test_fractional_video_duration_limits_frames_even_when_audio_is_longer(tmp_path):
    source = tmp_path / "fractional.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=64x32:r=125:d=8.008",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=9",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-y", str(source),
    ], check=True, capture_output=True)
    assert probe_video_duration_seconds(source) == 8008
    service = VideoParseMediaService(tmp_path / "real-work")

    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=0, end_seconds=9000,
        visual_mode="ordered_frames", frame_fps=1, max_frames=9,
        max_height=64, ocr_enabled=False,
    ) as media:
        assert len(media.frames) == 9
        assert all(isinstance(timestamp, int) and 0 <= timestamp < 8008
                   for timestamp, _path in media.frames)
        assert all(path.is_file() and path.stat().st_size > 0
                   for _timestamp, path in media.frames)
    assert not media.video_path.exists()
