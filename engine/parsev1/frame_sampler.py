from __future__ import annotations

import math
import subprocess
from collections.abc import Sequence
from pathlib import Path


def plan_frame_timestamps(
    duration_seconds: int,
    *,
    frame_fps: float,
    max_frames: int,
) -> list[int]:
    """Plan integer-millisecond positions; FPS remains frames per second."""
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds <= 0
    ):
        raise ValueError("duration_seconds must be positive integer milliseconds")
    if frame_fps <= 0:
        raise ValueError("frame_fps must be greater than zero")
    if max_frames <= 0:
        raise ValueError("max_frames must be greater than zero")

    frame_count = min(
        max_frames,
        duration_seconds,
        max(1, math.ceil(duration_seconds / 1000 * frame_fps)),
    )
    return [
        (2 * index + 1) * duration_seconds // (2 * frame_count)
        for index in range(frame_count)
    ]


def extract_video_frames(
    video_path: str | Path,
    timestamps_seconds: Sequence[int],
    *,
    output_dir: str | Path,
    max_height: int,
) -> list[tuple[int, Path]]:
    path = Path(video_path).expanduser().resolve()
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[int, Path]] = []
    for index, timestamp_seconds in enumerate(timestamps_seconds):
        output = directory / f"frame-{index:03d}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp_seconds / 1000:.6f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale=-2:min({max_height}\\,ih)",
                    "-q:v",
                    "3",
                    "-y",
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError("ffmpeg is required for local frame fallback") from error
        except subprocess.CalledProcessError as error:
            message = error.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg could not extract frame at {timestamp_seconds} milliseconds: {message}"
            ) from error
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg produced no frame at {timestamp_seconds} milliseconds from {path}"
            )
        frames.append((timestamp_seconds, output))
    return frames
