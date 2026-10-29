from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import wave
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


@pytest.mark.parametrize("video_required", [False, True])
def test_episode_prepares_before_consumers_and_links_survive_owner_cleanup(tmp_path, video_required):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=128x72:rate=24:duration=2.5", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2.5", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(source)], check=True, capture_output=True)
    commands = []

    def run(command):
        commands.append(command)
        subprocess.run(command, check=True, capture_output=True)

    service = VideoParseMediaService(tmp_path / "media", command_runner=run)
    task = str(uuid4())
    owner = MediaLeaseIdentity(task, task, 1, execution_kind="video")
    context = service.prepare_episode(owner, source_video=source, duration_ms=2500,
        chunk_duration=1, visual_mode="ordered_frames", frame_fps=3, max_height=72,
        ocr_enabled=True, overlap_ms=500, chunk_video_required=video_required, check_active=lambda: None)
    with context as manifest:
        rows = json.loads(manifest.read_text())["chunks"]
        assert len(rows) == 3
        assert [t for t, _ in rows[0]["ocr_frames"]] == [166, 500, 833]
        assert [t for t, _ in rows[2]["ocr_frames"]] == [2125, 2375]
        assert sum(any(flag in command for flag in ("-filter_script:v", "-/filter:v"))
                   for command in commands) == 1
        if not video_required:
            assert len(commands) == 2  # one whole audio decode and one whole video decode
            assert not list(manifest.parent.glob("*.mp4"))
        before = len(commands)
        consumer = service.use_episode_chunk(MediaLeaseIdentity(task, str(uuid4()), 2),
            video_lease_version=1, sequence_no=0, start_ms=0, end_ms=1000, check_active=lambda: None)
        media = consumer.__enter__()
        assert (media.video_path is not None) is video_required
        assert len(commands) == before
        assert (media.visual_start_ms, media.visual_end_ms) == (0, 1500)
        assert all(os.stat(path).st_nlink >= 2 for _, path in media.frames)
    assert not manifest.exists()
    assert media.audio_path.is_file() and all(path.is_file() for _, path in media.frames)
    consumer.__exit__(None, None, None)
    assert not media.audio_path.exists()


def test_episode_decode_failure_never_publishes_partial_assets(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = VideoParseMediaService(tmp_path / "media", command_runner=_fake_runner([]),
                                    probe_duration=lambda _path: 1000)
    task = str(uuid4())
    def fail(*_args, **_kwargs):
        raise RuntimeError("decode failed")
    monkeypatch.setattr("server.service.video_parse_media.extract_video_frames_batch", fail)
    with pytest.raises(RuntimeError, match="decode failed"):
        with service.prepare_episode(MediaLeaseIdentity(task, task, 1, execution_kind="video"),
            source_video=source, duration_ms=1000, chunk_duration=1,
            visual_mode="ordered_frames", frame_fps=3, max_height=72,
            ocr_enabled=True, overlap_ms=0, check_active=lambda: None):
            pytest.fail("Incomplete preparation must not admit chunk consumers")
    assert list(service.base_directory.rglob("*")) == []


def test_event_capture_seeks_original_absolute_time_and_cleans_on_consumer_failure(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    commands = []
    service = VideoParseMediaService(tmp_path / "work", command_runner=_fake_runner(commands))
    paths = ()
    with pytest.raises(RuntimeError, match="encoder failed"):
        with service.prepare_event_frames(_identity(), source_video=source,
                                          timestamps_ms=(120_000, 121_500), max_height=720) as frames:
            assert [at for at, _ in frames] == [120_000, 121_500]
            paths = tuple(path for _, path in frames)
            assert all(path.is_file() for path in paths)
            raise RuntimeError("encoder failed")
    assert all(not path.exists() for path in paths)
    assert [command[command.index("-ss") + 1] for command in commands] == ["120.000000", "121.500000"]
    assert all(str(source) in command for command in commands)


@pytest.mark.parametrize("timestamps", [(), (1, 1), (2, 1), (-1,), (True,), (1.5,)])
def test_event_capture_rejects_invalid_plan_before_media_io(tmp_path, timestamps):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    commands = []
    service = VideoParseMediaService(tmp_path / "work", command_runner=_fake_runner(commands))
    with pytest.raises(ValueError, match="timestamps"):
        with service.prepare_event_frames(_identity(), source_video=source,
                                          timestamps_ms=timestamps, max_height=720):
            pytest.fail("invalid plan accepted")
    assert commands == []


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
        assert media.visual_video_path == media.video_path
        assert (media.visual_start_ms, media.visual_end_ms) == (10_000, 20_000)
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


def test_visual_context_keeps_core_asr_and_samples_at_configured_fps(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    probed = []

    def probe(path):
        probed.append(path)
        return 16_000

    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands), probe_duration=probe,
    )
    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=10_000, end_seconds=20_000,
        visual_start_ms=7000, visual_end_ms=23_000, visual_mode="ordered_frames",
        frame_fps=1, max_height=360, ocr_enabled=False,
    ) as media:
        assert media.visual_video_path != media.video_path
        assert media.visual_video_path.parent == media.video_path.parent
        assert (media.visual_start_ms, media.visual_end_ms) == (7000, 23_000)
        assert [timestamp for timestamp, _ in media.frames] == list(range(7500, 23_000, 1000))
        assert probed == [media.visual_video_path]
        assert commands[0][commands[0].index("-ss") + 1] == "10.000"
        assert commands[0][commands[0].index("-t") + 1] == "10.000"
        assert commands[1][commands[1].index("-i") + 1] == str(media.video_path)
        assert commands[2][commands[2].index("-ss") + 1] == "7.000"
        assert commands[2][commands[2].index("-t") + 1] == "16.000"
        assert all(command[command.index("-i") + 1] == str(media.visual_video_path)
                   for command in commands[3:])
        assert len(commands) == 19  # two clips, core audio and sixteen frames
    assert not media.video_path.parent.exists()


@pytest.mark.parametrize("overlap_ms,expected_frames", [(0, 180), (3000, 198)])
@pytest.mark.parametrize("ocr_enabled", [False, True])
def test_three_fps_preserves_all_chunk_frames(
    tmp_path, overlap_ms, expected_frames, ocr_enabled,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner([]),
        probe_duration=lambda path: 66_000 if path.name == "understanding.mp4" else 60_000,
    )
    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=10_000, end_seconds=70_000,
        visual_start_ms=10_000 - overlap_ms, visual_end_ms=70_000 + overlap_ms,
        visual_mode="ordered_frames", frame_fps=3,
        max_height=720, ocr_enabled=ocr_enabled,
    ) as media:
        times = [timestamp for timestamp, _ in media.frames]
        assert len(times) == expected_frames
        assert all(right - left in (333, 334) for left, right in zip(times, times[1:]))
        assert 10_000 - overlap_ms <= times[0] < times[-1] < 70_000 + overlap_ms
        assert all(path.is_file() for _, path in media.frames)
        if ocr_enabled:
            assert len(media.ocr_frames) == 180
            assert all(10_000 <= timestamp < 70_000 for timestamp, _ in media.ocr_frames)
    assert not media.video_path.parent.exists()


def test_explicit_core_visual_bounds_reuse_existing_clip(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(tmp_path / "work", command_runner=_fake_runner(commands))
    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=1000, end_seconds=2000,
        visual_start_ms=1000, visual_end_ms=2000, visual_mode="video",
        frame_fps=1, max_height=360, ocr_enabled=False,
    ) as media:
        assert media.visual_video_path == media.video_path
        assert media.frames == ()
        assert len(commands) == 2


@pytest.mark.parametrize("bounds", [
    (-1, 3000), (1500, 3000), (0, 1999), (0, 0), (True, 3000), (0, 3000.0),
])
def test_invalid_visual_bounds_do_not_create_media(tmp_path, bounds):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(tmp_path / "work", command_runner=_fake_runner(commands))
    with pytest.raises(ValueError, match="Visual bounds"):
        with service.prepare_chunk(
            _identity(), source_video=source, start_seconds=1000, end_seconds=2000,
            visual_start_ms=bounds[0], visual_end_ms=bounds[1], visual_mode="video",
            frame_fps=1, max_height=360, ocr_enabled=False,
        ):
            pytest.fail("invalid visual bounds must be rejected before preparing media")
    assert commands == []
    assert list(service.base_directory.iterdir()) == []


@pytest.mark.parametrize("failure", ["cancel", "ffmpeg"])
def test_failed_visual_context_cleans_core_and_extended_media(tmp_path, failure):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    checks = 0

    def run(command):
        _fake_runner(commands)(command)
        if failure == "ffmpeg" and len(commands) == 3:
            raise subprocess.CalledProcessError(1, command, stderr=b"visual decode failed")

    def check():
        nonlocal checks
        checks += 1
        if failure == "cancel" and checks == 4:
            raise RuntimeError("lease lost")

    service = VideoParseMediaService(tmp_path / "work", command_runner=run)
    with pytest.raises(RuntimeError, match="lease lost|visual decode failed"):
        with service.prepare_chunk(
            _identity(), source_video=source, start_seconds=1000, end_seconds=2000,
            visual_start_ms=0, visual_end_ms=3000, visual_mode="video",
            frame_fps=1, max_height=360, ocr_enabled=False, check_active=check,
        ):
            pytest.fail("failed visual preparation must not yield media")
    assert len(commands) == 3
    assert not list(service.base_directory.rglob(".lease.json"))
    assert not list(service.base_directory.rglob("*.mp4"))
    assert not list(service.base_directory.rglob("*.wav"))


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


@pytest.mark.parametrize("ocr_enabled,visual_fallback_enabled,frame_count", [
    (False, False, 0), (True, False, 5), (False, True, 5), (True, True, 5),
])
def test_video_mode_extracts_frames_only_for_ocr_or_fallback(
    tmp_path, ocr_enabled, visual_fallback_enabled, frame_count,
):
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
        frame_fps=1, max_height=360, ocr_enabled=ocr_enabled,
        visual_fallback_enabled=visual_fallback_enabled,
    ) as media:
        assert len(media.frames) == frame_count
        if ocr_enabled:
            assert media.ocr_frames is media.frames
        else:
            assert media.ocr_frames is None
    assert len(commands) == 2 + frame_count


@pytest.mark.parametrize("visual_mode,visual_fallback_enabled", [
    ("ordered_frames", False), ("video", True), ("video", False),
])
@pytest.mark.parametrize("ocr_enabled", [False, True])
def test_short_tail_keeps_a_core_frame_without_thinning_visual_context(
    tmp_path, visual_mode, visual_fallback_enabled, ocr_enabled,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    service = VideoParseMediaService(
        tmp_path / "work", command_runner=_fake_runner(commands),
        probe_duration=lambda path: 3100 if path.name == "understanding.mp4" else 100,
    )
    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=60_000, end_seconds=60_100,
        visual_start_ms=57_000, visual_end_ms=60_100, visual_mode=visual_mode,
        frame_fps=1, max_height=360, ocr_enabled=ocr_enabled,
        visual_fallback_enabled=visual_fallback_enabled,
    ) as media:
        visual_frames_needed = visual_mode == "ordered_frames" or visual_fallback_enabled
        expected = [57_387, 58_162, 58_937, 60_050] if visual_frames_needed else ([60_050] if ocr_enabled else [])
        assert [timestamp for timestamp, _ in media.frames] == expected
        if ocr_enabled:
            assert [timestamp for timestamp, _ in media.ocr_frames] == [60_050]
            core_frame_command = commands[-1]
            assert core_frame_command[core_frame_command.index("-i") + 1] == str(media.video_path)
        else:
            assert media.ocr_frames is None
        if not visual_frames_needed and ocr_enabled:
            assert media.ocr_frames is media.frames
        assert len(commands) == 3 + 4 * int(visual_frames_needed) + int(ocr_enabled)


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
            frame_fps=1, max_height=360, ocr_enabled=False,
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
        visual_mode="ordered_frames", frame_fps=1,
        max_height=64, ocr_enabled=False,
    ) as media:
        assert len(media.frames) == 9
        assert all(isinstance(timestamp, int) and 0 <= timestamp < 8008
                   for timestamp, _path in media.frames)
        assert all(path.is_file() and path.stat().st_size > 0
                   for _timestamp, path in media.frames)
    assert not media.video_path.exists()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real media regression requires FFmpeg and FFprobe",
)
def test_real_visual_overlap_does_not_extend_asr_audio_or_video(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=64x32:r=20:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source),
    ], check=True, capture_output=True)
    service = VideoParseMediaService(tmp_path / "work")
    with service.prepare_chunk(
        _identity(), source_video=source, start_seconds=1000, end_seconds=2000,
        visual_start_ms=0, visual_end_ms=3000, visual_mode="ordered_frames",
        frame_fps=3, max_height=64, ocr_enabled=False,
    ) as media:
        assert probe_video_duration_seconds(media.video_path) == 1000
        assert probe_video_duration_seconds(media.visual_video_path) == 3000
        with wave.open(str(media.audio_path), "rb") as audio:
            assert audio.getnframes() / audio.getframerate() == pytest.approx(1, abs=0.1)
        assert [timestamp for timestamp, _ in media.frames] == [166, 500, 833, 1166, 1500, 1833, 2166, 2500, 2833]
        assert all(path.is_file() for _timestamp, path in media.frames)
    assert not media.video_path.parent.exists()
