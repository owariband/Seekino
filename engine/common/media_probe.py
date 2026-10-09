from __future__ import annotations

import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path


def probe_video_duration_seconds(video_path: str | Path) -> int:
    """Return actual video-stream duration in integer milliseconds; keep the name."""
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration,duration_ts,time_base,avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe is required to prepare local analysis windows"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffprobe could not read video duration: {path}") from exc

    try:
        metadata = json.loads(result.stdout)
        if not isinstance(metadata, dict):
            raise ValueError("video metadata must be an object")
        streams = metadata.get("streams", [])
        if not streams:
            raise ValueError("video stream is missing")
        stream = streams[0]
        if stream.get("duration") not in (None, "N/A"):
            duration_seconds = float(stream["duration"])
        elif stream.get("duration_ts") not in (None, "N/A"):
            duration_seconds = float(
                int(stream["duration_ts"]) * Fraction(stream["time_base"])
            )
        else:
            # WebM and other containers may omit stream duration metadata.
            # Read video frame times only; a longer audio tail is not video.
            frames_result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_frames", "-show_entries",
                    "frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
                    "-of", "json", str(path),
                ],
                check=True, capture_output=True, text=True,
            )
            frames = json.loads(frames_result.stdout).get("frames", [])
            frame_starts = []
            frame_ends = []
            for frame in frames:
                start = float(frame["best_effort_timestamp_time"])
                frame_duration = frame.get("duration_time", frame.get("pkt_duration_time"))
                if frame_duration in (None, "N/A"):
                    frame_duration = 1 / Fraction(stream["avg_frame_rate"])
                frame_starts.append(start)
                frame_ends.append(start + float(frame_duration))
            duration_seconds = max(frame_ends) - min(frame_starts)
    except (ValueError, TypeError, KeyError, ZeroDivisionError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"ffprobe returned an invalid duration: {path}") from exc
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise RuntimeError(f"ffprobe returned an invalid duration: {path}")
    return max(1, round(duration_seconds * 1000))
