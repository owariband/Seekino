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
