"""Local CLI helpers for materializing temporary analysis media.

Production parsing consumes segments persisted by the upload pipeline. These
helpers only keep the current whole-video experiment and resume workflow usable.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .schemas import AnalysisWindow

_MIN_TAIL_MS = 1000


def probe_video_duration_ms(video_path: str | Path) -> int:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
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
        duration_seconds = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned an invalid duration: {path}") from exc
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise RuntimeError(f"ffprobe returned an invalid duration: {path}")
    return round(duration_seconds * 1000)


def plan_analysis_windows(
    video_path: str | Path,
    *,
    duration_ms: int,
    output_dir: str | Path,
    segment_duration_ms: int = 60_000,
    context_overlap_ms: int = 3_000,
) -> list[AnalysisWindow]:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    if segment_duration_ms <= 0:
        raise ValueError("segment_duration_ms must be greater than zero")
    if context_overlap_ms < 0:
        raise ValueError("context_overlap_ms cannot be negative")

    source_video = Path(video_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    windows: list[AnalysisWindow] = []
    segment_start_ms = 0

    while segment_start_ms < duration_ms:
        segment_end_ms = min(segment_start_ms + segment_duration_ms, duration_ms)
        min_tail_ms = min(_MIN_TAIL_MS, max(1, segment_duration_ms // 4))
        if 0 < duration_ms - segment_end_ms < min_tail_ms:
            segment_end_ms = duration_ms

        analysis_start_ms = max(0, segment_start_ms - context_overlap_ms)
        analysis_end_ms = min(duration_ms, segment_end_ms + context_overlap_ms)
        sequence_no = len(windows)
        filename = (
            f"segment-{sequence_no:04d}.analysis-"
            f"{analysis_start_ms:010d}-{analysis_end_ms:010d}.mp4"
        )
        windows.append(
            AnalysisWindow(
                sequence_no=sequence_no,
                source_video=source_video,
                media_path=target_dir / filename,
                segment_start_ms=segment_start_ms,
                segment_end_ms=segment_end_ms,
                analysis_start_ms=analysis_start_ms,
                analysis_end_ms=analysis_end_ms,
            )
        )
        segment_start_ms = segment_end_ms

    return windows


def materialize_analysis_windows(
    windows: list[AnalysisWindow],
    *,
    ffmpeg_bin: str = "ffmpeg",
    max_height: int | None = 720,
) -> None:
    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be greater than zero")

    for window in windows:
        if not window.source_video.is_file():
            raise FileNotFoundError(
                f"video file does not exist: {window.source_video}"
            )
        window.media_path.parent.mkdir(parents=True, exist_ok=True)
        if window.media_path.exists():
            if window.media_path.is_file() and window.media_path.stat().st_size > 0:
                continue
            raise RuntimeError(
                f"analysis window path exists but is not a usable file: "
                f"{window.media_path}"
            )

        start_seconds = f"{window.analysis_start_ms / 1000:.3f}"
        duration_seconds = f"{window.analysis_duration_ms / 1000:.3f}"
        command = [
            ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            start_seconds,
            "-i",
            str(window.source_video),
            "-t",
            duration_seconds,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-sn",
        ]
        if max_height is not None:
            command.extend(["-vf", f"scale=-2:min({max_height}\\,ih)"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-n",
                str(window.media_path),
            ]
        )
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required to create local analysis windows"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            if detail:
                detail = f": {detail[-1000:]}"
            raise RuntimeError(
                f"ffmpeg could not create analysis window {window.sequence_no}"
                f"{detail}"
            ) from exc
