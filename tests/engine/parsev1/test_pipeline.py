from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from time import sleep
from typing import Literal

import pytest

from engine.parsev1.embed import ChunkEmbedding, EmbedStage
from engine.parsev1.pipeline import (
    ParseChunkInput,
    ParsePipeline,
    ParsePipelineInput,
    ParsePipelineResult,
)
from engine.parsev1.schemas import (
    AnalysisWindow,
    TimelineEvent,
    TranscriptAnalysis,
    TranscriptCue,
    VideoChunkParseResult,
    VideoEvidence,
    VideoFallbackModels,
    VideoParseModels,
    VideoParseResult,
)
from engine.parsev1.transcribe import TranscribeStage
from engine.parsev1.understand import UnderstandStage
from gateway.embedding import EmbeddingResult


class _Trace:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[str] = []

    def add(self, event: str) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[str]:
        with self._lock:
            return list(self._events)


class _ASRAgent:
    def __init__(self, trace: _Trace) -> None:
        self._trace = trace
        self._started = Barrier(2)
        self._second_completed = Event()
        self._completed_lock = Lock()
        self._completed: set[int] = set()
        self.completion_order: list[int] = []

    def transcribe_chunk(self, video_chunk: AnalysisWindow) -> TranscriptAnalysis:
        sequence_no = video_chunk.sequence_no
        self._trace.add(f"asr:start:{sequence_no}")
        self._started.wait(timeout=5)
        if sequence_no == 0:
            assert self._second_completed.wait(timeout=5)
        else:
            self._complete(sequence_no)
            self._second_completed.set()
            return self._transcript(sequence_no)

        self._complete(sequence_no)
        return self._transcript(sequence_no)

    def all_completed(self) -> bool:
        with self._completed_lock:
            return self._completed == {0, 1}

    def _complete(self, sequence_no: int) -> None:
        with self._completed_lock:
            self._completed.add(sequence_no)
            self.completion_order.append(sequence_no)
        self._trace.add(f"asr:complete:{sequence_no}")

    @staticmethod
    def _transcript(sequence_no: int) -> TranscriptAnalysis:
        return TranscriptAnalysis(
            transcript=[
                TranscriptCue(
                    start_seconds=0,
                    end_seconds=1,
                    text=f"asr-{sequence_no}",
                )
            ]
        )


class _UnderstandingAgent:
    def __init__(self, trace: _Trace, asr: _ASRAgent) -> None:
        self._trace = trace
        self._asr = asr
        self.sequences: list[int] = []
        self.transcripts: list[str] = []
        self.contexts: list[str] = []

    def understand(
        self,
        video_path: str,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images=(),
    ) -> VideoEvidence:
        del background_images
        assert self._asr.all_completed(), "understand started before the ASR stage ended"
        sequence_no = int(Path(video_path).stem.rsplit("-", 1)[-1])
        assert duration_seconds == 1
        self._trace.add(f"understand:{sequence_no}")
        self.sequences.append(sequence_no)
        self.transcripts.append(transcript.transcript[0].text)
        self.contexts.append(context or "")
        return VideoEvidence(
            duration_seconds=duration_seconds,
            candidate_transcript=transcript.transcript,
            summary=f"summary-{sequence_no}",
            events=[
                TimelineEvent(
                    start_seconds=0,
                    end_seconds=1,
                    event_type="ACTION",
                    description=f"event-{sequence_no}",
                    text=None,
                )
            ],
        )


class _EmbeddingAgent:
    def __init__(self, trace: _Trace) -> None:
        self._trace = trace
        self.text_inputs: list[str] = []
        self.video_inputs: list[Path] = []

    def embed_text(self, text: str) -> EmbeddingResult:
        sequence_no = 0 if "event-0" in text else 1
        self._trace.add(f"embed:text:{sequence_no}")
        self.text_inputs.append(text)
        return EmbeddingResult(
            model="embedding-test",
            modality="text",
            vector=[float(sequence_no), 1.0, 2.0],
        )

    def embed_video(self, video_path: str | Path) -> EmbeddingResult:
        path = Path(video_path)
        sequence_no = int(path.stem.rsplit("-", 1)[-1])
        self._trace.add(f"embed:video:{sequence_no}")
        self.video_inputs.append(path)
        return EmbeddingResult(
            model="embedding-test",
            modality="video",
            vector=[float(sequence_no), 3.0, 4.0],
        )


class _Persistence:
    def __init__(self, trace: _Trace, models: VideoParseModels) -> None:
        self._trace = trace
        self._models = models
        self.stages: list[str] = []
        self.active_checks = 0
        self.saved_chunks: list[VideoChunkParseResult] = []
        self.saved_parse_result: VideoParseResult | None = None
        self.embedding_loads: list[tuple[int, str, str]] = []
        self.saved_embeddings: list[ChunkEmbedding] = []
        self.final_result: ParsePipelineResult | None = None

    def start_stage(self, stage: str) -> None:
        self.stages.append(stage)
        self._trace.add(f"stage:{stage}")

    def ensure_active(self) -> None:
        self.active_checks += 1

    def load_completed_chunks(
        self,
        windows: Sequence[AnalysisWindow],
        *,
        models: VideoParseModels,
    ) -> Sequence[VideoChunkParseResult]:
        assert [window.sequence_no for window in windows] == [0, 1]
        assert models == self._models
        return ()

    def save_chunk(
        self,
        chunk: VideoChunkParseResult,
        *,
        models: VideoParseModels,
    ) -> None:
        assert models == self._models
        self.saved_chunks.append(chunk)
        self._trace.add(f"persistence:chunk:{chunk.window.sequence_no}")

    def save_parse_result(self, result: VideoParseResult) -> None:
        self.saved_parse_result = result
        self._trace.add("persistence:parse-result")

    def load_embedding(
        self,
        *,
        sequence_no: int,
        modality: Literal["text", "video"],
        input_sha256: str,
    ) -> ChunkEmbedding | None:
        self.embedding_loads.append((sequence_no, modality, input_sha256))
        return None

    def save_embedding(self, embedding: ChunkEmbedding) -> None:
        self.saved_embeddings.append(embedding)
        self._trace.add(
            f"persistence:embedding:{embedding.modality}:{embedding.sequence_no}"
        )

    def persist(self, result: ParsePipelineResult) -> Sequence[str]:
        self.final_result = result
        self._trace.add("persistence:final")
        return [f"video-chunk-{sequence_no}" for sequence_no in result.documents]


def _models() -> VideoParseModels:
    return VideoParseModels(
        transcript="asr-test",
        transcript_mode="lite",
        video_parser="understanding-test",
        fallback=VideoFallbackModels(
            enabled=False,
            transcript_model="asr-test",
            provider="deepseek",
            visual_model="fallback-vision-test",
            refiner_model="fallback-refiner-test",
            input_mode="ordered_frames",
            frame_fps=0.5,
            group_duration_seconds=30,
            max_frames_per_group=15,
            max_height=720,
        ),
    )


def _request(tmp_path: Path) -> ParsePipelineInput:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"source")
    chunks = []
    for sequence_no in range(2):
        analysis_media = tmp_path / f"window-{sequence_no}.mp4"
        canonical_media = tmp_path / f"canonical-{sequence_no}.mp4"
        analysis_media.write_bytes(f"analysis-{sequence_no}".encode())
        canonical_media.write_bytes(f"canonical-{sequence_no}".encode())
        window = AnalysisWindow(
            sequence_no=sequence_no,
            source_video=source,
            media_path=analysis_media,
            chunk_start_seconds=sequence_no * 1,
            chunk_end_seconds=(sequence_no + 1) * 1,
            analysis_start_seconds=sequence_no * 1,
            analysis_end_seconds=(sequence_no + 1) * 1,
        )
        chunks.append(
            ParseChunkInput(
                window=window,
                canonical_media_path=canonical_media,
                canonical_media_sha256=f"{sequence_no + 1:064x}",
            )
        )
    return ParsePipelineInput(
        source_video=source,
        duration_seconds=2,
        chunk_duration_seconds=1,
        context_overlap_seconds=0,
        chunks=tuple(chunks),
        series_bible="series context",
    )


def test_pipeline_owns_algorithm_order_and_hands_results_to_persistence(
    tmp_path: Path,
) -> None:
    trace = _Trace()
    models = _models()
    asr = _ASRAgent(trace)
    understanding = _UnderstandingAgent(trace, asr)
    embedding = _EmbeddingAgent(trace)
    persistence = _Persistence(trace, models)
    pipeline = ParsePipeline(
        models=models,
        transcribe=TranscribeStage(asr, concurrency=2),
        understand=UnderstandStage(understanding),
        embed=EmbedStage(embedding, dimensions=3),
    )

    video_chunk_ids = pipeline.run(
        _request(tmp_path),
        persistence=persistence,
    )

    # Embed has separate durable milestones for its text and vision halves, but
    # remains one algorithm stage between understand and persist.
    assert persistence.stages == [
        "transcribe",
        "understand",
        "text_embedding",
        "vision_embedding",
        "persist",
    ]
    assert asr.completion_order == [1, 0]
    assert understanding.sequences == [0, 1]
    assert understanding.transcripts == ["asr-0", "asr-1"]
    events = trace.events
    assert events.index("stage:understand") > max(
        events.index("asr:complete:0"),
        events.index("asr:complete:1"),
    )

    assert "event-0" not in understanding.contexts[0]
    assert "event-0" in understanding.contexts[1]
    assert [chunk.window.sequence_no for chunk in persistence.saved_chunks] == [
        0,
        1,
    ]
    assert persistence.saved_chunks[1].context_before == (
        persistence.saved_chunks[0].context_after
    )
    assert [
        cue.text
        for chunk in persistence.saved_chunks
        for cue in chunk.candidate_transcript
    ] == ["asr-0", "asr-1"]

    assert len(embedding.text_inputs) == 2
    assert "event-0" in embedding.text_inputs[0]
    assert "event-1" in embedding.text_inputs[1]
    assert embedding.video_inputs == [
        tmp_path / "canonical-0.mp4",
        tmp_path / "canonical-1.mp4",
    ]
    assert [
        (item.modality, item.sequence_no)
        for item in persistence.saved_embeddings
    ] == [
        ("text", 0),
        ("text", 1),
        ("video", 0),
        ("video", 1),
    ]
    assert [
        (sequence_no, modality)
        for sequence_no, modality, _input_hash in persistence.embedding_loads
    ] == [
        (0, "text"),
        (1, "text"),
        (0, "video"),
        (1, "video"),
    ]
    assert persistence.embedding_loads[2][2] == f"{1:064x}"
    assert persistence.embedding_loads[3][2] == f"{2:064x}"

    assert persistence.final_result is not None
    assert persistence.saved_parse_result is persistence.final_result.parse_result
    assert persistence.final_result.parse_result.models == models
    assert list(persistence.final_result.documents) == [0, 1]
    assert list(persistence.final_result.text_embeddings) == [0, 1]
    assert list(persistence.final_result.vision_embeddings) == [0, 1]
    assert persistence.active_checks > 0
    assert events.index("persistence:final") > events.index(
        "persistence:embedding:video:1"
    )
    assert video_chunk_ids == ["video-chunk-0", "video-chunk-1"]


def test_transcribe_stage_propagates_context_and_refills_with_bounded_lookahead(
    tmp_path: Path,
) -> None:
    asr_context = ContextVar("asr_context", default="missing")
    release_first = Event()
    third_started = Event()
    fourth_started = Event()
    lock = Lock()
    context_values: dict[int, str] = {}

    class Agent:
        def transcribe_chunk(
            self,
            video_chunk: AnalysisWindow,
        ) -> TranscriptAnalysis:
            sequence_no = video_chunk.sequence_no
            with lock:
                context_values[sequence_no] = asr_context.get()
            if sequence_no == 0:
                assert release_first.wait(timeout=5)
            elif sequence_no == 2:
                third_started.set()
            elif sequence_no == 3:
                fourth_started.set()
            return TranscriptAnalysis(transcript=[])

    base_window = _request(tmp_path).chunks[0].window
    windows = [
        base_window.model_copy(update={"sequence_no": sequence_no})
        for sequence_no in range(4)
    ]
    results: dict[int, TranscriptAnalysis] = {}
    errors: list[BaseException] = []

    def consume() -> None:
        token = asr_context.set("parse-usage-capture")
        try:
            results.update(
                TranscribeStage(Agent(), concurrency=2).run(
                    windows,
                    ensure_active=lambda: None,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            asr_context.reset(token)

    consumer = Thread(target=consume)
    consumer.start()
    refilled = third_started.wait(timeout=5)
    overfilled = fourth_started.wait(timeout=0.1)
    release_first.set()
    consumer.join(timeout=5)

    assert refilled
    assert not overfilled
    assert not consumer.is_alive()
    assert errors == []
    assert list(results) == [0, 1, 2, 3]
    assert context_values == {
        sequence_no: "parse-usage-capture" for sequence_no in range(4)
    }


@pytest.mark.parametrize("failure", ["model", "ensure_active"])
def test_transcribe_stage_stops_workers_before_propagating_failures(
    failure: str,
    tmp_path: Path,
) -> None:
    class Agent:
        def __init__(self) -> None:
            self.lock = Lock()
            self.calls: list[int] = []
            self.active = 0

        def transcribe_chunk(
            self,
            video_chunk: AnalysisWindow,
        ) -> TranscriptAnalysis:
            with self.lock:
                self.calls.append(video_chunk.sequence_no)
                self.active += 1
            try:
                if failure == "model" and video_chunk.sequence_no == 0:
                    raise RuntimeError("model failed")
                if video_chunk.sequence_no != 0:
                    sleep(0.02)
                return TranscriptAnalysis(transcript=[])
            finally:
                with self.lock:
                    self.active -= 1

    base_window = _request(tmp_path).chunks[0].window
    windows = [
        base_window.model_copy(update={"sequence_no": sequence_no})
        for sequence_no in range(10)
    ]
    agent = Agent()

    def ensure_active() -> None:
        if failure == "ensure_active":
            raise RuntimeError("lease lost")

    expected_message = "model failed" if failure == "model" else "lease lost"
    with pytest.raises(RuntimeError, match=expected_message):
        TranscribeStage(agent, concurrency=2).run(
            windows,
            ensure_active=ensure_active,
        )

    calls_after_exit = list(agent.calls)
    assert agent.active == 0
    sleep(0.05)
    assert agent.active == 0
    assert agent.calls == calls_after_exit
