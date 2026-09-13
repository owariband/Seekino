from __future__ import annotations

import math
import subprocess
from collections.abc import Sequence
from pathlib import Path


def plan_frame_timestamps(
    duration_ms: int,
    *,
    frame_fps: float,
    max_frames: int,
) -> list[int]:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    if frame_fps <= 0:
        raise ValueError("frame_fps must be greater than zero")
    if max_frames <= 0:
        raise ValueError("max_frames must be greater than zero")

    frame_count = min(
        max_frames,
        max(1, math.ceil(duration_ms * frame_fps / 1000)),
    )
    return [
        min(duration_ms - 1, ((2 * index + 1) * duration_ms) // (2 * frame_count))
        for index in range(frame_count)
    ]


def extract_video_frames(
    video_path: str | Path,
    timestamps_ms: Sequence[int],
    *,
    output_dir: str | Path,
    max_height: int,
) -> list[tuple[int, Path]]:
    path = Path(video_path).expanduser().resolve()
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[int, Path]] = []
    for index, timestamp_ms in enumerate(timestamps_ms):
        output = directory / f"frame-{index:03d}-{timestamp_ms:08d}ms.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp_ms / 1000:.3f}",
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
                f"ffmpeg could not extract frame at {timestamp_ms} ms: {message}"
            ) from error
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg produced no frame at {timestamp_ms} ms from {path}"
            )
        frames.append((timestamp_ms, output))
    return frames
