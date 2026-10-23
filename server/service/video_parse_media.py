"""Lease-scoped temporary media for Parse V2 workers."""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from engine.common.frame_sampler import plan_frame_timestamps
from engine.common.media_probe import probe_video_duration_seconds

from ..utils.validation import canonical_uuid


@dataclass(frozen=True, slots=True)
class MediaLeaseIdentity:
    video_parse_task_id: str
    execution_task_id: str
    lease_version: int
    execution_kind: str = "chunk"

    def __post_init__(self) -> None:
        canonical_uuid(self.video_parse_task_id, "video_parse_task_id")
        canonical_uuid(self.execution_task_id, "execution_task_id")
        if (
            isinstance(self.lease_version, bool)
            or not isinstance(self.lease_version, int)
            or self.lease_version <= 0
        ):
            raise ValueError("lease_version must be a positive integer")
        if self.execution_kind not in {"video", "chunk", "interval", "prefix"}:
            raise ValueError(
                "execution_kind must be video, chunk, interval, or prefix"
            )


@dataclass(frozen=True, slots=True)
class PreparedVideoParseMedia:
    """Paths valid only inside ``VideoParseMediaService.prepare_chunk``."""

    video_path: Path
    audio_path: Path
    frames: tuple[tuple[int, Path], ...]
    visual_video_path: Path | None = None
    visual_start_ms: int | None = None
    visual_end_ms: int | None = None
    ocr_frames: tuple[tuple[int, Path], ...] | None = None


class VideoParseMediaService:
    """Materialize and reap media without sharing paths between task leases."""

    _MARKER = ".lease.json"

    def __init__(
        self,
        base_directory: str | Path,
        *,
        ffmpeg_bin: str = "ffmpeg",
        command_runner: Callable[[Sequence[str]], None] | None = None,
        probe_duration: Callable[[Path], int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        base = Path(base_directory).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        if not ffmpeg_bin.strip():
            raise ValueError("ffmpeg_bin must not be empty")
        self.base_directory = base
        self._ffmpeg_bin = ffmpeg_bin
        self._command_runner = command_runner or self._run_subprocess
        self._probe_duration = probe_duration or probe_video_duration_seconds
        self._clock = clock

    @contextmanager
    def prepare_speaker_audio(
        self,
        identity: MediaLeaseIdentity,
        *,
        source_video: str | Path,
        start_ms: int,
        end_ms: int,
        check_active: Callable[[], None],
    ) -> Iterator[Path]:
        """Decode original audio directly; the caller owns the execution lifetime."""
        source = Path(source_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
            raise ValueError("Speaker audio bounds must be increasing integer milliseconds")
        with self._workspace(identity) as directory:
            check_active()
            audio = directory / "speaker.wav"
            self._materialize_source_audio(source, audio, start_ms=start_ms, end_ms=end_ms)
            check_active()
            yield audio

    @contextmanager
    def prepare_chunk_tool_media(
        self,
        media: PreparedVideoParseMedia,
        *,
        source_video: Path,
        start_ms: int,
        end_ms: int,
        input_mode: Literal["audio", "video"],
        max_height: int,
        check_active: Callable[[], None],
    ) -> Iterator[Path]:
        """Prepare one tool input inside an already active Chunk media context."""
        source = Path(source_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if input_mode not in {"audio", "video"}:
            raise ValueError("input_mode must be audio or video")
        self._validate_options(
            start_seconds=start_ms, end_seconds=end_ms, visual_mode="video",
            frame_fps=1.0, max_frames=1, max_height=max_height,
        )
        check_active()
        # The enclosing Chunk owns its lease lock; each tool call owns only its child.
        with TemporaryDirectory(prefix="tool-", dir=media.video_path.parent) as temporary:
            directory = Path(temporary)
            if input_mode == "audio":
                output = directory / "audio.wav"
                self._materialize_source_audio(source, output, start_ms=start_ms, end_ms=end_ms)
            else:
                output = directory / "video.mp4"
                self._materialize_video(
                    source, output, start_seconds=start_ms,
                    end_seconds=end_ms, max_height=max_height,
                )
            check_active()
            yield output

    @contextmanager
    def prepare_video_segment(
        self,
        identity: MediaLeaseIdentity,
        *,
        source_video: str | Path,
        start_seconds: int,
        end_seconds: int,
        max_height: int,
        check_active: Callable[[], None] | None = None,
    ) -> Iterator[Path]:
        """Create one lease-scoped video segment, for example for indexing."""

        source = Path(source_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        self._validate_options(
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            visual_mode="video",
            frame_fps=1.0,
            max_frames=1,
            max_height=max_height,
        )
        ensure_active = check_active or (lambda: None)
        with self._workspace(identity) as directory:
            ensure_active()
            video = directory / "visual.mp4"
            self._materialize_video(
                source,
                video,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                max_height=max_height,
            )
            ensure_active()
            yield video

    @contextmanager
    def prepare_event_frames(
        self,
        identity: MediaLeaseIdentity,
        *,
        source_video: str | Path,
        timestamps_ms: Sequence[int],
        max_height: int,
        check_active: Callable[[], None] | None = None,
    ) -> Iterator[tuple[tuple[int, Path], ...]]:
        """Prepare planned source-video frames in the current Index lease workspace."""

        source = Path(source_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        times = tuple(timestamps_ms)
        if (not times or any(type(value) is not int or value < 0 for value in times)
                or tuple(sorted(set(times))) != times):
            raise ValueError("Event frame timestamps must be nonempty, increasing integer milliseconds")
        if type(max_height) is not int or max_height <= 0:
            raise ValueError("Event frame max_height must be a positive integer")
        ensure_active = check_active or (lambda: None)
        with self._workspace(identity) as directory:
            frames = []
            for ordinal, timestamp in enumerate(times):
                ensure_active()
                path = directory / f"event-frame-{ordinal:04d}.jpg"
                self._materialize_frame(source, path, at_seconds=timestamp, max_height=max_height)
                ensure_active()
                frames.append((timestamp, path))
            yield tuple(frames)

    @contextmanager
    def prepare_chunk(
        self,
        identity: MediaLeaseIdentity,
        *,
        source_video: str | Path,
        start_seconds: int,
        end_seconds: int,
        visual_mode: str,
        frame_fps: float,
        max_frames: int,
        max_height: int,
        ocr_enabled: bool,
        visual_fallback_enabled: bool = False,
        visual_start_ms: int | None = None,
        visual_end_ms: int | None = None,
        check_active: Callable[[], None] | None = None,
    ) -> Iterator[PreparedVideoParseMedia]:
        """Keep ASR inside the core clip while visual context may extend it."""

        source = Path(source_video).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        self._validate_options(
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            visual_mode=visual_mode,
            frame_fps=frame_fps,
            max_frames=max_frames,
            max_height=max_height,
        )
        visual_start_ms = start_seconds if visual_start_ms is None else visual_start_ms
        visual_end_ms = end_seconds if visual_end_ms is None else visual_end_ms
        if (
            type(visual_start_ms) is not int or type(visual_end_ms) is not int
            or not 0 <= visual_start_ms <= start_seconds < end_seconds <= visual_end_ms
        ):
            raise ValueError("Visual bounds must be integer milliseconds containing the core clip")
        ensure_active = check_active or (lambda: None)
        with self._workspace(identity) as directory:
            ensure_active()
            video = directory / "visual.mp4"
            self._materialize_video(
                source,
                video,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                max_height=max_height,
            )
            ensure_active()
            audio = directory / "audio.wav"
            self._materialize_audio(video, audio)
            ensure_active()
            visual_video = video
            if (visual_start_ms, visual_end_ms) != (start_seconds, end_seconds):
                visual_video = directory / "understanding.mp4"
                self._materialize_video(
                    source, visual_video, start_seconds=visual_start_ms,
                    end_seconds=visual_end_ms, max_height=max_height,
                )
                ensure_active()

            frame_sources = []
            visual_frames_needed = visual_mode == "ordered_frames" or visual_fallback_enabled
            if visual_frames_needed:
                frame_sources.append(("frames", visual_video, visual_start_ms, visual_end_ms))
            if ocr_enabled and (not visual_frames_needed or visual_video != video):
                # OCR keeps its original core sampling, including very short tail chunks.
                frame_sources.append(("ocr-frames", video, start_seconds, end_seconds))
            frame_batches: dict[str, tuple[tuple[int, Path], ...]] = {}
            for name, frame_video, frame_start, frame_end in frame_sources:
                frame_directory = directory / name
                frame_directory.mkdir()
                # Media bounds are milliseconds. Audio can extend beyond the last
                # video frame, so sample only inside the prepared video stream.
                duration = min(frame_end - frame_start, self._probe_duration(frame_video))
                timestamps = plan_frame_timestamps(
                    duration,
                    frame_fps=frame_fps,
                    max_frames=max_frames,
                )
                core_start, core_end = start_seconds, min(end_seconds, frame_start + duration)
                if (name == "frames" and core_start < core_end
                        and not any(core_start <= frame_start + value < core_end for value in timestamps)):
                    # Context must not displace every frame of a very short core chunk.
                    timestamps[-1] = (core_start + core_end) // 2 - frame_start
                    timestamps.sort()
                sampled_frames: list[tuple[int, Path]] = []
                for index, relative_seconds in enumerate(timestamps):
                    ensure_active()
                    output = frame_directory / f"frame-{index:03d}.jpg"
                    self._materialize_frame(
                        frame_video,
                        output,
                        at_seconds=relative_seconds,
                        max_height=max_height,
                    )
                    sampled_frames.append((frame_start + relative_seconds, output))
                frame_batches[name] = tuple(sampled_frames)
            frames = frame_batches.get("frames", frame_batches.get("ocr-frames", ()))
            ocr_frames = frame_batches.get("ocr-frames", frames) if ocr_enabled else None
            ensure_active()
            yield PreparedVideoParseMedia(
                video, audio, frames, visual_video,
                visual_start_ms, visual_end_ms, ocr_frames,
            )

    def reap_stale(
        self,
        *,
        older_than_seconds: float,
        is_active: Callable[[MediaLeaseIdentity], bool],
    ) -> int:
        """Delete old workspaces only after the host confirms their lease is dead."""

        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(older_than_seconds)
            or older_than_seconds < 0
        ):
            raise ValueError("older_than_seconds must be finite and nonnegative")
        cutoff = self._clock() - older_than_seconds
        reaped = 0
        for tombstone in tuple(
            self.base_directory.glob("*/*/.reaping-lease-*")
        ):
            try:
                if tombstone.is_symlink() or tombstone.stat().st_mtime > cutoff:
                    continue
                with self._workspace_lock(tombstone, blocking=False) as acquired:
                    if acquired:
                        self._delete_tree(tombstone)
                        self._remove_empty_parents(tombstone.parent)
                        reaped += 1
            except (FileNotFoundError, OSError):
                continue

        for directory in tuple(self.base_directory.glob("*/*/lease-*")):
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                marker = directory / self._MARKER
                age_source = marker if marker.is_file() else directory
                if age_source.stat().st_mtime > cutoff:
                    continue
                try:
                    identity = (
                        self._read_identity(marker)
                        if marker.is_file()
                        else self._identity_from_workspace(directory)
                    )
                except RuntimeError:
                    identity = self._identity_from_workspace(directory)
                with self._workspace_lock(directory, blocking=False) as acquired:
                    if not acquired or is_active(identity):
                        continue
                    tombstone = directory.with_name(
                        f".reaping-{directory.name}-{uuid4().hex}"
                    )
                    directory.replace(tombstone)
                    self._delete_tree(tombstone)
                    self._remove_empty_parents(directory.parent)
                    reaped += 1
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
        return reaped

    @contextmanager
    def _workspace(self, identity: MediaLeaseIdentity) -> Iterator[Path]:
        directory = self._create_workspace(identity)
        with self._workspace_lock(directory, blocking=True):
            try:
                yield directory
            finally:
                self._delete_tree(directory)
                self._remove_empty_parents(directory.parent)

    @staticmethod
    @contextmanager
    def _workspace_lock(directory: Path, *, blocking: bool) -> Iterator[bool]:
        # Keep the lock for the actual media context lifetime, including a
        # revoked lease whose provider thread has not exited. A crashed process
        # releases it automatically; another Consumer can then reap the files.
        with (directory / ".execution.lock").open("a+b") as lock:
            os.set_inheritable(lock.fileno(), False)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _create_workspace(self, identity: MediaLeaseIdentity) -> Path:
        video_task = canonical_uuid(
            identity.video_parse_task_id, "video_parse_task_id"
        )
        execution_task = canonical_uuid(
            identity.execution_task_id, "execution_task_id"
        )
        directory = (
            self.base_directory
            / video_task
            / f"{identity.execution_kind}-{execution_task}"
            / f"lease-{identity.lease_version:08d}"
        )
        directory.mkdir(parents=True, exist_ok=False)
        marker_payload = {
            "video_parse_task_id": video_task,
            "execution_task_id": execution_task,
            "execution_kind": identity.execution_kind,
            "lease_version": identity.lease_version,
            "created_at": self._clock(),
        }
        marker_path = directory / self._MARKER
        temporary_marker = directory / f".{self._MARKER}.{uuid4().hex}.tmp"
        temporary_marker.write_text(
            json.dumps(marker_payload, separators=(",", ":")), encoding="utf-8"
        )
        temporary_marker.replace(marker_path)
        return directory

    def _materialize_video(
        self,
        source: Path,
        output: Path,
        *,
        start_seconds: int,
        end_seconds: int,
        max_height: int,
    ) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                "-ss", f"{start_seconds / 1000:.3f}",
                "-i", str(source),
                "-t", f"{(end_seconds - start_seconds) / 1000:.3f}",
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

    def _materialize_audio(self, video: Path, output: Path) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                "-i", str(video),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                "-y", str(output),
            ],
            output,
        )

    def _materialize_source_audio(
        self, source: Path, output: Path, *, start_ms: int, end_ms: int,
    ) -> None:
        self._execute([
            self._ffmpeg_bin, "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
            "-i", str(source), "-t", f"{(end_ms - start_ms) / 1000:.3f}",
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-y", str(output),
        ], output)

    def _materialize_frame(
        self,
        video: Path,
        output: Path,
        *,
        at_seconds: int,
        max_height: int,
    ) -> None:
        self._execute(
            [
                self._ffmpeg_bin,
                "-v", "error",
                "-ss", f"{at_seconds / 1000:.6f}",
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

    @staticmethod
    def _validate_options(
        *,
        start_seconds: int,
        end_seconds: int,
        visual_mode: str,
        frame_fps: float,
        max_frames: int,
        max_height: int,
    ) -> None:
        if (
            isinstance(start_seconds, bool)
            or not isinstance(start_seconds, int)
            or isinstance(end_seconds, bool)
            or not isinstance(end_seconds, int)
            or not 0 <= start_seconds < end_seconds
        ):
            raise ValueError("Chunk bounds must be increasing integer milliseconds")
        if visual_mode not in {"ordered_frames", "video"}:
            raise ValueError("visual_mode must be ordered_frames or video")
        if (
            isinstance(frame_fps, bool)
            or not isinstance(frame_fps, (int, float))
            or not math.isfinite(frame_fps)
            or frame_fps <= 0
        ):
            raise ValueError("frame_fps must be greater than zero")
        for name, value in (("max_frames", max_frames), ("max_height", max_height)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def _read_identity(cls, marker: Path) -> MediaLeaseIdentity:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            return MediaLeaseIdentity(
                video_parse_task_id=payload["video_parse_task_id"],
                execution_task_id=payload["execution_task_id"],
                lease_version=payload["lease_version"],
                execution_kind=payload["execution_kind"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid Parse V2 media lease marker: {marker}") from error

    @classmethod
    def _identity_from_workspace(cls, directory: Path) -> MediaLeaseIdentity:
        name = directory.name
        if not name.startswith("lease-"):
            raise ValueError("not a Parse V2 lease workspace")
        execution_kind, separator, execution_task_id = (
            directory.parent.name.partition("-")
        )
        if not separator:
            raise ValueError("Parse V2 lease workspace has no execution kind")
        return MediaLeaseIdentity(
            video_parse_task_id=directory.parent.parent.name,
            execution_task_id=execution_task_id,
            lease_version=int(name.removeprefix("lease-")),
            execution_kind=execution_kind,
        )

    @staticmethod
    def _delete_tree(directory: Path) -> None:
        def ignore_missing(_function, _path, error: OSError) -> None:
            if not isinstance(error, FileNotFoundError):
                raise error

        shutil.rmtree(directory, onexc=ignore_missing)

    def _remove_empty_parents(self, directory: Path) -> None:
        while directory != self.base_directory:
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                return
            directory = directory.parent


__all__ = [
    "MediaLeaseIdentity",
    "PreparedVideoParseMedia",
    "VideoParseMediaService",
]
