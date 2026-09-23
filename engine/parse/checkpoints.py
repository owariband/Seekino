from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from threading import Condition, Event, Lock
from typing import Any

from pydantic import BaseModel

from .schemas import (
    AnalysisWindow,
    TranscriptAnalysis,
    TranscriptCheckpoint,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
)
from .usage import parse_chunk_scope
from .video_chunk_parse import VideoASRAgentProtocol


def write_json(payload: BaseModel | dict[str, Any], output: Path) -> None:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


def analysis_windows_match(
    checkpoint_window: AnalysisWindow,
    current_window: AnalysisWindow,
) -> bool:
    if checkpoint_window == current_window:
        return True
    checkpoint_data = checkpoint_window.model_dump()
    current_data = current_window.model_dump()
    checkpoint_data.pop("media_path")
    current_data.pop("media_path")
    return checkpoint_data == current_data


def load_completed_chunks(
    windows: list[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    video_model: str,
    fallback_provider: str,
    fallback_model: str,
    fallback_transcript_model: str | None,
    fallback_frame_fps: float,
    fallback_group_duration_seconds: int,
    fallback_max_frames_per_group: int,
) -> list[VideoChunkParseResult]:
    completed: list[VideoChunkParseResult] = []
    gap_found = False
    for window in windows:
        path = output_dir / "chunks" / f"chunk-{window.sequence_no:04d}.json"
        if not path.is_file():
            legacy_path = (
                output_dir
                / "segments"
                / f"segment-{window.sequence_no:04d}.json"
            )
            if legacy_path.is_file():
                path = legacy_path
            else:
                gap_found = True
                continue
        if gap_found:
            raise RuntimeError(f"chunk checkpoints contain a gap before {path}")

        checkpoint = VideoChunkCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if checkpoint.transcript_model != transcript_model:
            raise RuntimeError(f"transcript model changed for checkpoint: {path}")
        if checkpoint.transcript_source != transcript_source:
            raise RuntimeError(f"transcript source changed for checkpoint: {path}")
        if checkpoint.video_model != video_model:
            raise RuntimeError(f"video model changed for checkpoint: {path}")
        provenance = checkpoint.chunk.video_parse
        if provenance is not None:
            if provenance.provider == "doubao":
                expected_model = video_model
                expected_input_mode = "video"
            else:
                if provenance.provider != fallback_provider:
                    raise RuntimeError(
                        f"actual video parser provider changed for checkpoint: {path}"
                    )
                expected_model = fallback_model
                expected_input_mode = "ordered_frames"
            if provenance.model != expected_model:
                raise RuntimeError(
                    f"actual video parser changed for checkpoint: {path}"
                )
            if provenance.input_mode != expected_input_mode:
                raise RuntimeError(
                    f"video parser input mode changed for checkpoint: {path}"
                )
            if provenance.provider != "doubao":
                expected_fallback = {
                    "frame_fps": fallback_frame_fps,
                    "group_duration_seconds": fallback_group_duration_seconds,
                    "max_frames_per_group": fallback_max_frames_per_group,
                    "fallback_transcript_model": fallback_transcript_model,
                    "visual_model": fallback_model,
                    "refiner_model": fallback_model,
                }
                actual_fallback = {
                    key: getattr(provenance, key) for key in expected_fallback
                }
                legacy_no_asr_provenance = (
                    transcript_source == "none"
                    and actual_fallback["fallback_transcript_model"]
                    == checkpoint.transcript_model
                    and expected_fallback["fallback_transcript_model"] is None
                )
                if legacy_no_asr_provenance:
                    actual_fallback["fallback_transcript_model"] = None
                if actual_fallback != expected_fallback:
                    raise RuntimeError(
                        f"fallback strategy changed for checkpoint: {path}"
                    )
        if not analysis_windows_match(checkpoint.chunk.window, window):
            raise RuntimeError(f"analysis window changed for checkpoint: {path}")
        completed.append(checkpoint.chunk.model_copy(update={"window": window}))
    return completed


class _CandidateTranscriptStream:
    def __init__(
        self,
        parser: VideoASRAgentProtocol | None,
        windows: Sequence[AnalysisWindow],
        *,
        output_dir: Path,
        transcript_model: str,
        transcript_source: str,
        concurrency: int,
        on_log: Callable[[str], None] | None,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("asr_concurrency must be greater than zero")

        self._parser = parser
        self._windows = tuple(windows)
        self._output_dir = output_dir
        self._transcript_model = transcript_model
        self._transcript_source = transcript_source
        self._log = on_log or (lambda message: print(message, flush=True))
        self._transcripts: dict[int, TranscriptAnalysis] = {}
        self._errors: dict[int, Exception] = {}
        self._pending: deque[AnalysisWindow] = deque()
        self._next_index = 0
        self._ready_count = 0
        self._ready_lock = Lock()
        self._state = Condition()
        self._stop = Event()
        self._executor: ThreadPoolExecutor | None = None

        for window in self._windows:
            path = self._checkpoint_path(window.sequence_no)
            if not path.is_file():
                legacy_path = (
                    output_dir
                    / "transcripts"
                    / f"segment-{window.sequence_no:04d}.json"
                )
                if legacy_path.is_file():
                    path = legacy_path
                else:
                    self._pending.append(window)
                    continue
            checkpoint = TranscriptCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if checkpoint.transcript_model != transcript_model:
                raise RuntimeError(
                    f"transcript model changed for checkpoint: {path}"
                )
            if checkpoint.transcript_source != transcript_source:
                raise RuntimeError(
                    f"transcript source changed for checkpoint: {path}"
                )
            if not analysis_windows_match(checkpoint.window, window):
                raise RuntimeError(
                    f"analysis window changed for checkpoint: {path}"
                )
            self._transcripts[window.sequence_no] = checkpoint.analysis

        self._ready_count = len(self._transcripts)
        if not self._pending:
            return
        if transcript_source == "none":
            self._prepare_empty_transcripts()
            return
        if parser is None:
            raise ValueError("an ASR agent is required when transcript_source is lite")

        self._executor = ThreadPoolExecutor(
            max_workers=min(concurrency, len(self._pending)),
            thread_name_prefix="anivag-asr",
        )
        for _ in range(min(concurrency, len(self._pending))):
            context = copy_context()
            self._executor.submit(context.run, self._run_worker)

    def __iter__(self) -> _CandidateTranscriptStream:
        return self

    def __next__(self) -> tuple[int, TranscriptAnalysis]:
        if self._next_index >= len(self._windows):
            raise StopIteration

        window = self._windows[self._next_index]
        sequence_no = window.sequence_no
        with self._state:
            while (
                sequence_no not in self._transcripts
                and sequence_no not in self._errors
            ):
                self._state.wait()
            error = self._errors.get(sequence_no)
            if error is not None:
                raise RuntimeError(
                    f"candidate ASR failed for chunk {sequence_no}"
                ) from error
            analysis = self._transcripts[sequence_no]

        self._next_index += 1
        return sequence_no, analysis

    def close(self) -> None:
        if self._executor is None:
            return
        self._stop.set()
        with self._state:
            self._pending.clear()
            self._state.notify_all()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None

    def _run_worker(self) -> None:
        while True:
            with self._state:
                if self._stop.is_set() or not self._pending:
                    return
                window = self._pending.popleft()
            try:
                analysis = self._transcribe_and_checkpoint(window)
            except Exception as exc:
                with self._state:
                    self._errors[window.sequence_no] = exc
                    self._state.notify_all()
                continue
            with self._state:
                self._transcripts[window.sequence_no] = analysis
                self._state.notify_all()

    def _transcribe_and_checkpoint(
        self,
        window: AnalysisWindow,
    ) -> TranscriptAnalysis:
        if self._parser is None:
            raise RuntimeError("ASR agent was not configured")
        with parse_chunk_scope(window.sequence_no):
            analysis = self._parser.transcribe(
                window.media_path,
                duration_seconds=window.analysis_duration_seconds,
            )
        write_json(
            TranscriptCheckpoint(
                transcript_model=self._transcript_model,
                transcript_source=self._transcript_source,
                window=window,
                analysis=analysis,
            ),
            self._checkpoint_path(window.sequence_no),
        )
        with self._ready_lock:
            self._ready_count += 1
            ready_count = self._ready_count
            self._log(
                f"ASR ready {ready_count}/{len(self._windows)}; "
                f"chunk={window.sequence_no}; cues={len(analysis.transcript)}"
            )
        return analysis

    def _prepare_empty_transcripts(self) -> None:
        pending_count = len(self._pending)
        while self._pending:
            window = self._pending.popleft()
            analysis = TranscriptAnalysis(transcript=[])
            write_json(
                TranscriptCheckpoint(
                    transcript_model=self._transcript_model,
                    transcript_source=self._transcript_source,
                    window=window,
                    analysis=analysis,
                ),
                self._checkpoint_path(window.sequence_no),
            )
            self._transcripts[window.sequence_no] = analysis
        self._ready_count = len(self._transcripts)
        self._log(
            f"candidate ASR disabled for {pending_count} pending chunks; "
            "video understanding will use video audio and hard subtitles"
        )

    def _checkpoint_path(self, sequence_no: int) -> Path:
        return (
            self._output_dir
            / "transcripts"
            / f"chunk-{sequence_no:04d}.json"
        )


@contextmanager
def stream_candidate_transcripts(
    parser: VideoASRAgentProtocol | None,
    windows: Sequence[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    concurrency: int,
    on_log: Callable[[str], None] | None = None,
) -> Iterator[Iterator[tuple[int, TranscriptAnalysis]]]:
    stream = _CandidateTranscriptStream(
        parser,
        windows,
        output_dir=output_dir,
        transcript_model=transcript_model,
        transcript_source=transcript_source,
        concurrency=concurrency,
        on_log=on_log,
    )
    try:
        yield stream
    finally:
        stream.close()


def prepare_candidate_transcripts(
    parser: VideoASRAgentProtocol,
    windows: Sequence[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    concurrency: int,
    on_log: Callable[[str], None] | None = None,
) -> dict[int, TranscriptAnalysis]:
    with stream_candidate_transcripts(
        parser,
        windows,
        output_dir=output_dir,
        transcript_model=transcript_model,
        transcript_source=transcript_source,
        concurrency=concurrency,
        on_log=on_log,
    ) as transcripts:
        return dict(transcripts)
