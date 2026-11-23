"""Media operations backed by the server's installed FFmpeg programs."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .frame_sampler import extract_video_frames_batch
from .media_contract import FFMPEG_PREPROCESSING, MediaPreprocessing
from .media_probe import probe_video_duration_seconds


class MediaOperations(Protocol):
    preprocessing: MediaPreprocessing

    def probe_duration(self, source: Path, *, check_active: Callable[[], None]) -> int: ...

    def frames(self, source: Path, timestamps_ms: Sequence[int], *, output_dir: Path,
               max_height: int, check_active: Callable[[], None]) -> list[tuple[int, Path]]: ...

    def frame_at(self, video: Path, output: Path, *, at_ms: int, max_height: int,
                 check_active: Callable[[], None]) -> None: ...

    def clip(self, source: Path, output: Path, *, start_ms: int, end_ms: int,
             max_height: int, check_active: Callable[[], None]) -> None: ...

    def audio(self, video: Path, output: Path, *, start_ms: int = 0, end_ms: int | None = None,
              first_track: bool = False, check_active: Callable[[], None]) -> None: ...


class FFmpegMedia:
    preprocessing = FFMPEG_PREPROCESSING

    def __init__(self, ffmpeg_bin: str = "ffmpeg",
                 command_runner: Callable[[Sequence[str]], None] | None = None,
                 probe_duration: Callable[[Path], int] | None = None) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._command_runner = command_runner or self._run_subprocess
        self._probe_duration = probe_duration or probe_video_duration_seconds

    def probe_duration(self, source: Path, *, check_active: Callable[[], None]) -> int:
        return self._probe_duration(source)

    def frames(self, source: Path, timestamps_ms: Sequence[int], *, output_dir: Path,
               max_height: int, check_active: Callable[[], None]) -> list[tuple[int, Path]]:
        return extract_video_frames_batch(source, timestamps_ms, output_dir=output_dir, max_height=max_height,
                                          ffmpeg_bin=self._ffmpeg_bin, command_runner=self._command_runner)

    def clip(
        self,
        source: Path,
        output: Path,
        *,
        start_ms: int,
        end_ms: int,
        max_height: int,
        check_active: Callable[[], None],
    ) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                "-ss", f"{start_ms / 1000:.3f}",
                "-i", str(source),
                "-t", f"{(end_ms - start_ms) / 1000:.3f}",
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-vf", f"scale=-2:min({max_height}\\,ih)",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                "-y", str(output),
            ],
            output,
        )

    def audio(self, video: Path, output: Path, *, start_ms: int = 0,
              end_ms: int | None = None, first_track: bool = False,
              check_active: Callable[[], None]) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                *(["-ss", f"{start_ms / 1000:.3f}"] if start_ms or first_track else []),
                "-i", str(video),
                *(["-t", f"{(end_ms - start_ms) / 1000:.3f}"] if end_ms is not None else []),
                *(["-map", "0:a:0"] if first_track else []),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                "-y", str(output),
            ],
            output,
        )

    def frame_at(
        self,
        video: Path,
        output: Path,
        *,
        at_ms: int,
        max_height: int,
        check_active: Callable[[], None],
    ) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                "-ss", f"{at_ms / 1000:.6f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", f"scale=-2:min({max_height}\\,ih)",
                "-q:v", "3",
                "-y", str(output),
            ],
            output,
        )

    def _execute(self, command: Sequence[str], output: Path) -> None:
        try:
            self._command_runner(command)
        except FileNotFoundError as error:
            raise RuntimeError("ffmpeg is required for Parse V2 media") from error
        except subprocess.CalledProcessError as error:
            stderr = error.stderr
            detail = (
                stderr.decode("utf-8", errors="replace").strip()
                if isinstance(stderr, bytes)
                else str(stderr or "").strip()
            )
            raise RuntimeError(
                "ffmpeg could not prepare Parse V2 media"
                + (f": {detail}" if detail else "")
            ) from error
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no Parse V2 media output")

    @staticmethod
    def _run_subprocess(command: Sequence[str]) -> None:
        subprocess.run(command, check=True, capture_output=True)
