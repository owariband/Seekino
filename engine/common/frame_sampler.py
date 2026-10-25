from __future__ import annotations

import math
import subprocess
from bisect import bisect_left
from collections.abc import Sequence
from pathlib import Path
from time import monotonic


def extract_video_frames_batch(
    video_path: str | Path, timestamps_ms: Sequence[int], *, output_dir: str | Path,
    max_height: int, ffmpeg_bin: str = "ffmpeg", command_runner=None,
) -> list[tuple[int, Path]]:
    """Decode once; retain requested timestamps and share coincident source frames."""
    times = sorted(set(timestamps_ms))
    if not times or any(type(t) is not int or t < 0 for t in times):
        raise ValueError("Frame timestamps must be nonempty nonnegative milliseconds")
    if type(max_height) is not int or max_height <= 0:
        raise ValueError("Frame max_height must be positive")
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    def select(low, high):
        if low == high:
            return f"if(isnan(prev_t),1,lt(prev_t,{times[low] / 1000:.3f}))"
        middle = (low + high + 1) // 2
        return f"if(lt(t,{times[middle] / 1000:.3f}),{select(low, middle - 1)},{select(middle, high)})"

    # A balanced expression visits O(log N) thresholds per decoded frame.
    # Keep the final second too, so requests within the last frame's duration
    # can use that frame even when there is no subsequent presentation timestamp.
    expression = (f"if(gte(t,{max(0, times[-1] / 1000 - 1):.3f}),1,"
                  f"if(lt(t,{times[0] / 1000:.3f}),0,{select(0, len(times) - 1)}))")
    script = directory / "sampling.filter"
    script.write_text(f"setpts=PTS-STARTPTS,select='{expression}',scale=-2:min({max_height}\\,ih)")
    command = [ffmpeg_bin, "-v", "error", "-i", str(Path(video_path).resolve()),
               "-map", "0:v:0", "-filter_script:v", str(script), "-fps_mode", "passthrough",
               "-enc_time_base", "1:1000", "-q:v", "3", "-frame_pts", "1",
               "-y", str(directory / "frame-%012d.jpg")]
    if command_runner is None:
        subprocess.run(command, check=True, capture_output=True)
    else:
        command_runner(command)
    frames = sorted((int(path.stem.removeprefix("frame-")), path)
                    for path in directory.glob("frame-*.jpg") if path.stat().st_size)
    if not frames:
        raise RuntimeError("FFmpeg produced no batch frames")
    actual_times = [stamp for stamp, _ in frames]
    return [(stamp, frames[min(bisect_left(actual_times, stamp), len(frames) - 1)][1])
            for stamp in timestamps_ms]


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
    timeout_seconds: float | None = None,
) -> list[tuple[int, Path]]:
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("frame extraction timeout must be finite and positive")
    deadline = None if timeout_seconds is None else monotonic() + timeout_seconds
    path = Path(video_path).expanduser().resolve()
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[int, Path]] = []
    for index, timestamp_seconds in enumerate(timestamps_seconds):
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("video frame extraction exceeded its time budget")
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
                timeout=remaining,
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
