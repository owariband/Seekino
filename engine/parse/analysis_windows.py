"""Plan and materialize analysis windows and canonical VideoChunk media.

The CLI and durable Worker share these deterministic helpers. Analysis windows
may overlap for model context; canonical chunks never overlap and are the media
units persisted for playback and vision embeddings.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .schemas import AnalysisWindow

_MIN_TAIL_SECONDS = 1


def probe_video_duration_seconds(video_path: str | Path) -> int:
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
    return math.ceil(duration_seconds)


def plan_analysis_windows(
    video_path: str | Path,
    *,
    duration_seconds: int,
    output_dir: str | Path,
    chunk_duration_seconds: int = 60,
    context_overlap_seconds: int = 3,
) -> list[AnalysisWindow]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    if chunk_duration_seconds <= 0:
        raise ValueError("chunk_duration_seconds must be greater than zero")
    if context_overlap_seconds < 0:
        raise ValueError("context_overlap_seconds cannot be negative")

    source_video = Path(video_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    windows: list[AnalysisWindow] = []
    chunk_start_seconds = 0

    while chunk_start_seconds < duration_seconds:
        chunk_end_seconds = min(
            chunk_start_seconds + chunk_duration_seconds,
            duration_seconds,
        )
        min_tail_seconds = min(
            float(_MIN_TAIL_SECONDS),
            chunk_duration_seconds / 4,
        )
        if 0 < duration_seconds - chunk_end_seconds <= min_tail_seconds:
            chunk_end_seconds = duration_seconds

        analysis_start_seconds = max(
            0,
            chunk_start_seconds - context_overlap_seconds,
        )
        analysis_end_seconds = min(
            duration_seconds,
            chunk_end_seconds + context_overlap_seconds,
        )
        sequence_no = len(windows)
        filename = (
            f"chunk-{sequence_no:04d}.analysis-"
            f"{analysis_start_seconds:010d}-{analysis_end_seconds:010d}.mp4"
        )
        windows.append(
            AnalysisWindow(
                sequence_no=sequence_no,
                source_video=source_video,
                media_path=target_dir / filename,
                chunk_start_seconds=chunk_start_seconds,
                chunk_end_seconds=chunk_end_seconds,
                analysis_start_seconds=analysis_start_seconds,
                analysis_end_seconds=analysis_end_seconds,
            )
        )
        chunk_start_seconds = chunk_end_seconds

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
        materialize_analysis_window(
            window,
            ffmpeg_bin=ffmpeg_bin,
            max_height=max_height,
        )


def materialize_analysis_window(
    window: AnalysisWindow,
    *,
    ffmpeg_bin: str = "ffmpeg",
    max_height: int | None = 720,
) -> None:
    """Materialize one planned analysis window without plan-level validation."""

    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be greater than zero")
    if not window.source_video.is_file():
        raise FileNotFoundError(f"video file does not exist: {window.source_video}")
    window.media_path.parent.mkdir(parents=True, exist_ok=True)
    if window.media_path.exists():
        if window.media_path.is_file() and window.media_path.stat().st_size > 0:
            return
        raise RuntimeError(
            f"analysis window path exists but is not a usable file: {window.media_path}"
        )

    start_seconds = f"{window.analysis_start_seconds:.3f}"
    duration_seconds = f"{window.analysis_duration_seconds:.3f}"
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
            f"ffmpeg could not create analysis window {window.sequence_no}{detail}"
        ) from exc


def materialize_canonical_chunks(
    windows: list[AnalysisWindow],
    *,
    output_dir: str | Path,
    ffmpeg_bin: str = "ffmpeg",
    max_height: int | None = 720,
) -> list[Path]:
    """Create the non-overlapping media units used by the vision index."""

    if not windows:
        raise ValueError("at least one analysis window is required")
    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be greater than zero")
    target_directory = Path(output_dir).expanduser().resolve()
    outputs: list[Path] = []
    for expected_sequence_no, window in enumerate(windows):
        if window.sequence_no != expected_sequence_no:
            raise ValueError("analysis windows must have contiguous sequence numbers")
        outputs.append(
            materialize_canonical_chunk(
                window,
                output_dir=target_directory,
                ffmpeg_bin=ffmpeg_bin,
                max_height=max_height,
            )
        )
    return outputs


def materialize_canonical_chunk(
    window: AnalysisWindow,
    *,
    output_dir: str | Path,
    ffmpeg_bin: str = "ffmpeg",
    max_height: int | None = 720,
) -> Path:
    """Materialize one canonical chunk; callers owning a plan validate it."""

    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be greater than zero")
    if not window.source_video.is_file():
        raise FileNotFoundError(f"video file does not exist: {window.source_video}")
    target = (
        Path(output_dir).expanduser().resolve()
        / f"chunk-{window.sequence_no:04d}.mp4"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_file() and target.stat().st_size > 0:
            return target
        raise RuntimeError(
            f"canonical VideoChunk path is not a usable file: {target}"
        )
    command = [
        ffmpeg_bin,
        "-nostdin",
        "-v",
        "error",
        "-ss",
        f"{window.chunk_start_seconds:.3f}",
        "-i",
        str(window.source_video),
        "-t",
        f"{window.chunk_end_seconds - window.chunk_start_seconds:.3f}",
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
            str(target),
        ]
    )
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is required to create canonical VideoChunks"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        if detail:
            detail = f": {detail[-1000:]}"
        raise RuntimeError(
            f"ffmpeg could not create canonical VideoChunk {window.sequence_no}{detail}"
        ) from exc
    return target
