from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Condition, Event
from typing import Protocol

from .schemas import AnalysisWindow, TranscriptAnalysis


class ASRAgent(Protocol):
    def transcribe_chunk(
        self,
        video_chunk: AnalysisWindow,
    ) -> TranscriptAnalysis: ...


class TranscribeStage:
    """Run bounded ASR concurrently and return results in window order."""

    def __init__(
        self,
        agent: ASRAgent | None,
        *,
        concurrency: int,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be greater than zero")
        self._agent = agent
        self._concurrency = concurrency

    def run(
        self,
        windows: Sequence[AnalysisWindow],
        *,
        ensure_active: Callable[[], None],
    ) -> dict[int, TranscriptAnalysis]:
        if self._agent is None:
            transcripts = {}
            for window in windows:
                ensure_active()
                transcripts[window.sequence_no] = TranscriptAnalysis(transcript=[])
            return transcripts
        if not windows:
            return {}

        root_context = copy_context()
        window_iterator = iter(windows)
        ready = Condition()
        stopped = Event()
        results: dict[int, TranscriptAnalysis | BaseException] = {}
        outstanding = 0
        maximum_outstanding = self._concurrency + 1

        def transcribe_pending_windows() -> None:
            nonlocal outstanding
            while not stopped.is_set():
                with ready:
                    ready.wait_for(
                        lambda: stopped.is_set()
                        or outstanding < maximum_outstanding
                    )
                    if stopped.is_set():
                        return
                    try:
                        window = next(window_iterator)
                    except StopIteration:
                        return
                    outstanding += 1
                try:
                    def transcribe_one() -> TranscriptAnalysis:
                        assert self._agent is not None
                        return self._agent.transcribe_chunk(window)

                    result: TranscriptAnalysis | BaseException = (
                        root_context.copy().run(transcribe_one)
                    )
                except BaseException as exc:
                    result = exc
                    stopped.set()
                with ready:
                    results[window.sequence_no] = result
                    ready.notify_all()

        executor = ThreadPoolExecutor(
            max_workers=self._concurrency,
            thread_name_prefix="parse-asr",
        )
        workers = []
        transcripts: dict[int, TranscriptAnalysis] = {}
        try:
            workers = [
                executor.submit(transcribe_pending_windows)
                for _ in range(min(self._concurrency, len(windows)))
            ]
            for window in windows:
                with ready:
                    ready.wait_for(lambda: window.sequence_no in results)
                    transcript = results.pop(window.sequence_no)
                    outstanding -= 1
                    ready.notify_all()
                if isinstance(transcript, BaseException):
                    raise transcript
                ensure_active()
                transcripts[window.sequence_no] = transcript
        finally:
            with ready:
                stopped.set()
                ready.notify_all()
            for worker in workers:
                worker.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
        return transcripts
