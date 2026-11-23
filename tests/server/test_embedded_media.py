"""Compare the two media implementations with exactly the same local files."""

import json
import os
import shutil
import subprocess
import wave
from array import array
from uuid import uuid4

import pytest
from PIL import Image, ImageChops, ImageStat

av = pytest.importorskip("av")
from engine.common.ffmpeg_media import FFmpegMedia
from engine.common.pyav_media import PyAVMedia
from server.service.video_parse_media import MediaLeaseIdentity, VideoParseMediaService


def _ffmpeg(*arguments):
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("ffmpeg fixture generator is unavailable")
    subprocess.run([binary, "-nostdin", "-v", "error", *map(str, arguments)], check=True, capture_output=True)


def _active():
    pass


@pytest.fixture
def source(tmp_path):
    # The default audio stream is stereo 880 Hz. Speaker extraction and clip
    # muxing must instead select the first track (mono 440 Hz).
    path = tmp_path / "two-tracks.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24:duration=2.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=3",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-ac:a:1", "2",
            "-disposition:a:0", "0", "-disposition:a:1", "default",
            "-c:v", "libx264", "-g", "200", "-c:a", "aac", path)
    return path


def _frequency(path):
    with wave.open(str(path)) as stream:
        assert (stream.getsampwidth(), stream.getnchannels(), stream.getframerate()) == (2, 1, 16000)
        samples = array("h", stream.readframes(stream.getnframes()))
    crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:]))
    return len(samples), crossings * 16000 / len(samples)


def _no_children(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("desktop media attempted to launch a subprocess")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(subprocess, "Popen", fail)


@pytest.fixture(params=[
    "mpeg4",
    pytest.param("videotoolbox", marks=pytest.mark.skipif(
        os.environ.get("SEEKINO_TEST_VIDEOTOOLBOX") != "1",
        reason="run with SEEKINO_TEST_VIDEOTOOLBOX=1 on a Mac with hardware encoding access",
    )),
])
def media_encoder(request):
    # Generic CI exercises the same timestamps and stream selection with
    # FFmpeg's built-in MPEG-4 encoder. Mac acceptance covers the shipping default.
    if request.param == "videotoolbox":
        return PyAVMedia(), "h264"
    return PyAVMedia(video_encoder="mpeg4"), "mpeg4"


def test_episode_and_replay_use_original_clock_and_no_children(tmp_path, source, monkeypatch, media_encoder):
    # The software encoder makes this contract executable on Linux CI. The
    # shipping macOS encoder is tested separately on a host with VideoToolbox.
    backend, expected_codec = media_encoder
    cli = FFmpegMedia()
    assert cli.probe_duration(source, check_active=_active) == backend.probe_duration(source) == 2500
    for first_track, expected_hz in [(True, 440), (False, 880)]:
        arguments = dict(start_ms=1001 if first_track else 0, end_ms=2001 if first_track else None,
                         first_track=first_track, check_active=_active)
        cli.audio(source, tmp_path / "cli.wav", **arguments)
        backend.audio(source, tmp_path / "embedded.wav", **arguments)
        nsamples, frequency = _frequency(tmp_path / "embedded.wav")
        cli_count, cli_frequency = _frequency(tmp_path / "cli.wav")
        assert nsamples == cli_count
        assert frequency == pytest.approx(cli_frequency, abs=2)
        assert frequency == pytest.approx(expected_hz, abs=3)

    _no_children(monkeypatch)
    service = VideoParseMediaService(tmp_path / "leases", media=backend)
    task = str(uuid4())
    owner = MediaLeaseIdentity(task, task, 1, execution_kind="video")
    with service.prepare_episode(owner, source_video=source, duration_ms=2500, chunk_duration=1,
                                 visual_mode="ordered_frames", frame_fps=3, max_height=64,
                                 ocr_enabled=True, overlap_ms=500, chunk_video_required=True,
                                 check_active=_active) as manifest:
        first, _, last = json.loads(manifest.read_text())["chunks"]
        assert [t for t, _ in first["ocr_frames"]] == [166, 500, 833]
        assert [t for t, _ in last["ocr_frames"]] == [2125, 2375]
        assert backend.probe_duration(manifest.parent / first["visual_video_path"]) == 1500
        with av.open(str(manifest.parent / first["video_path"])) as clip:
            assert [(s.type, s.codec_context.name) for s in clip.streams] == [("video", expected_codec), ("audio", "aac")]
            assert clip.streams.audio[0].codec_context.channels == 1
        _, frequency = _frequency(manifest.parent / first["audio_path"])
        assert frequency == pytest.approx(440, abs=3)
    with service.prepare_event_frames(owner, source_video=source, timestamps_ms=[1001], max_height=64) as frames:
        assert frames[0][0] == 1001
        with Image.open(frames[0][1]) as image:
            assert image.height == 64
    with service.prepare_speaker_audio(owner, source_video=source, start_ms=1001, end_ms=2001,
                                       check_active=_active) as wav:
        assert _frequency(wav)[0] == 16000
    with service.prepare_video_segment(owner, source_video=source, start_seconds=1001, end_seconds=2001,
                                       max_height=64) as clip_path:
        with av.open(str(clip_path)) as clip:
            stream = clip.streams.video[0]
            pictures = list(clip.decode(stream))
            assert 0 < pictures[0].time < 1 / 24
            assert 0.99 < pictures[-1].time < 1
            assert pictures[-1].time + float(pictures[-1].duration * stream.time_base) == pytest.approx(1, abs=0.001)
    assert not list(service.base_directory.iterdir())


def test_batch_frames_vfr_nonzero_origin_and_rotation_match_cli(tmp_path, source, monkeypatch):
    vfr = tmp_path / "vfr.mp4"
    rotated = tmp_path / "rotated.mp4"
    _ffmpeg("-i", source, "-an", "-vf", "select='not(mod(n,3))+eq(mod(n,7),2)'",
            "-fps_mode", "vfr", "-c:v", "libx264", "-g", "200", vfr)
    _ffmpeg("-display_rotation", "90", "-i", vfr, "-map", "0:v:0", "-c", "copy",
            "-output_ts_offset", "3", rotated)
    times = [450, 2, 3, 2499, 450]
    cli = FFmpegMedia()
    expected = cli.frames(rotated, times, output_dir=tmp_path / "cli", max_height=80, check_active=_active)
    cli.frame_at(rotated, tmp_path / "seek-cli.jpg", at_ms=450, max_height=80, check_active=_active)
    _no_children(monkeypatch)
    media = PyAVMedia()
    media.frame_at(rotated, tmp_path / "seek-embedded.jpg", at_ms=450, max_height=80, check_active=_active)
    with Image.open(tmp_path / "seek-cli.jpg") as left, Image.open(tmp_path / "seek-embedded.jpg") as right:
        assert max(ImageStat.Stat(ImageChops.difference(left, right)).mean) < 12
    actual = media.frames(rotated, times, output_dir=tmp_path / "embedded", max_height=80, check_active=_active)
    assert [(t, p.name) for t, p in actual] == [(t, p.name) for t, p in expected]
    assert actual[1][1] == actual[2][1]
    for (_, a), (_, b) in zip(actual, expected, strict=True):
        with Image.open(a) as left, Image.open(b) as right:
            assert left.size == right.size and left.height > left.width
            assert max(ImageStat.Stat(ImageChops.difference(left, right)).mean) < 12
    with pytest.raises(ValueError, match="no audio"):
        media.audio(rotated, tmp_path / "missing.wav", check_active=_active)


def test_missing_stream_duration_and_cancelled_decode(tmp_path, monkeypatch):
    path = tmp_path / "uneven.webm"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=25:duration=3.24",
            "-f", "lavfi", "-i", "sine=duration=4",
            "-c:v", "libvpx-vp9", "-c:a", "libopus", path)
    assert FFmpegMedia().probe_duration(path, check_active=_active) == 3240
    _no_children(monkeypatch)
    media = PyAVMedia()
    with av.open(str(path)) as container:
        assert container.streams.video[0].duration is None
    assert media.probe_duration(path) == 3240
    task = str(uuid4())
    owner = MediaLeaseIdentity(task, task, 1, execution_kind="video")
    service = VideoParseMediaService(tmp_path / "leases", media=media)
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        with service.prepare_episode(owner, source_video=path, duration_ms=3240, chunk_duration=1,
                                     visual_mode="ordered_frames", frame_fps=3, max_height=64,
                                     ocr_enabled=False, overlap_ms=0, check_active=cancel):
            pytest.fail("cancelled media was published")
    assert not list(service.base_directory.iterdir())


def test_silent_hevc_and_corrupt_input(tmp_path, monkeypatch, media_encoder):
    source = tmp_path / "silent-hevc.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=1",
            "-c:v", "libx265", "-tag:v", "hvc1", source)
    cli_duration = FFmpegMedia().probe_duration(source, check_active=_active)
    _no_children(monkeypatch)
    backend, expected_codec = media_encoder
    assert backend.probe_duration(source) == cli_duration == 1000
    target = tmp_path / "proxy.mp4"
    backend.clip(source, target, start_ms=200, end_ms=800, max_height=80, check_active=_active)
    with av.open(str(target)) as clip:
        assert [(s.type, s.codec_context.name) for s in clip.streams] == [("video", expected_codec)]
    assert backend.probe_duration(target) == 600

    broken = tmp_path / "incomplete.mp4"
    broken.write_bytes(source.read_bytes()[:64])
    with pytest.raises(av.FFmpegError):
        backend.probe_duration(broken)
