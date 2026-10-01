from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

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


def test_media_is_isolated_by_task_and_lease_and_always_cleaned(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands)
    )
    identity = _identity(lease_version=7)

    with service.prepare_chunk(
        identity,
        source_video=source,
        start_seconds=10,
        end_seconds=20,
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
        assert all(10 <= timestamp < 20 for timestamp, _ in media.frames)
        assert json.loads((expected / ".lease.json").read_text())["lease_version"] == 7

    assert not expected.exists()
    assert len(commands) == 4  # clip, audio and two uniformly sampled frames


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
        tmp_path / "work", command_runner=_fake_runner(commands)
    )

    with service.prepare_chunk(
        _identity(), source_video=source,
        start_seconds=0, end_seconds=5, visual_mode="video",
        frame_fps=1, max_frames=2, max_height=360, ocr_enabled=False,
    ) as media:
        assert media.frames == ()
    assert len(commands) == 2

    with service.prepare_chunk(
        _identity(), source_video=source,
        start_seconds=0, end_seconds=5, visual_mode="video",
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
        start_seconds=5,
        end_seconds=9,
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


def test_reaper_uses_workspace_identity_when_marker_is_missing_or_corrupt(tmp_path):
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
