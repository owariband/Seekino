from pathlib import Path
import subprocess
import sys

import pytest

from engine.common import frame_sampler


def test_frame_extraction_terminates_a_process_that_exceeds_the_budget(
    tmp_path, monkeypatch,
):
    run = subprocess.run

    def stalled_ffmpeg(_command, **kwargs):
        return run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(frame_sampler.subprocess, "run", stalled_ffmpeg)
    with pytest.raises(subprocess.TimeoutExpired):
        frame_sampler.extract_video_frames(
            tmp_path / "video.mp4", [1000], output_dir=tmp_path / "frames",
            max_height=720, timeout_seconds=0.05,
        )


def test_all_frames_share_one_extraction_budget(tmp_path, monkeypatch):
    clock = iter([0, 0, 2])
    monkeypatch.setattr(frame_sampler, "monotonic", lambda: next(clock))
    extracted = []

    def finish_first_frame(command, **_kwargs):
        extracted.append(command)
        Path(command[-1]).write_bytes(b"frame")

    monkeypatch.setattr(frame_sampler.subprocess, "run", finish_first_frame)
    with pytest.raises(TimeoutError, match="time budget"):
        frame_sampler.extract_video_frames(
            tmp_path / "video.mp4", [1000, 2000], output_dir=tmp_path / "frames",
            max_height=720, timeout_seconds=1,
        )
    assert len(extracted) == 1


def test_batch_decode_preserves_requested_times_and_reuses_frames(tmp_path):
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=128x72:rate=24:duration=2", "-c:v", "libx264", str(source)],
                   check=True, capture_output=True)
    calls = []

    def run(command):
        calls.append(command)
        subprocess.run(command, check=True, capture_output=True)

    stamps = [166, 167, 500, 833, 1166, 1500, 1833, 1999]
    rows = frame_sampler.extract_video_frames_batch(
        source, stamps, output_dir=tmp_path / "frames", max_height=72, command_runner=run,
    )
    assert len(calls) == 1
    assert [stamp for stamp, _ in rows] == stamps
    assert rows[0][1] == rows[1][1]
    actual = [int(path.stem.removeprefix("frame-")) for _, path in rows]
    assert all(0 <= decoded - target <= 42 for decoded, target in zip(actual[:-1], stamps[:-1]))
    assert actual[-1] == 1958  # final requested instant lies within the last frame
    assert all(path.is_file() for _, path in rows)
