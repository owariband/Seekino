import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from engine.common.config import VideoParseRuntimeConfig
from engine.common.usage import capture_parse_usage, parse_chunk_scope
from engine.parsev1.checkpoints import (
    prepare_candidate_transcripts,
    stream_candidate_transcripts,
)
from engine.parsev1.runtime import EngineVideoParseRuntime
from engine.parsev1.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    TranscriptAnalysis,
    VideoEvidence,
    VideoTimeline,
)
from engine.parsev1.understand import UnderstandStage
from gateway.ark.multimodal import ArkMultimodalModel
from gateway.multimodal import TextInput
from gateway.usage import capture_model_usage, track_model_call


def _windows(count=2):
    return [
        AnalysisWindow(sequence_no=sequence, source_video=Path("video.mp4"),
                       media_path=Path(f"window-{sequence}.mp4"), chunk_start_seconds=sequence,
                       chunk_end_seconds=sequence + 1, analysis_start_seconds=sequence,
                       analysis_end_seconds=sequence + 1)
        for sequence in range(count)
    ]


class Responses:
    def __init__(self, model, *, barrier=None, fail_sequence=None):
        self.model = model
        self.barrier = barrier
        self.fail_sequence = fail_sequence
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        sequence = int(request["input"][0]["content"][0]["text"])
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if sequence == self.fail_sequence:
            raise RuntimeError("provider failure")
        text = json.dumps({"transcript": []})
        response = SimpleNamespace(
            model=self.model, request_id=f"{self.model}-{sequence}",
            usage=SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12),
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])],
        )
        return iter([SimpleNamespace(type="response.completed", response=response)])


class Transcriber:
    def __init__(self, responses):
        self.model = ArkMultimodalModel(responses.model, client=SimpleNamespace(responses=responses))

    def transcribe(self, path, *, duration_seconds):
        sequence = int(path.stem.rsplit("-", 1)[1])
        return self.model.generate([TextInput(str(sequence))], schema_name="candidate_transcript", result_type=TranscriptAnalysis)


def _prepare(parser, output_dir, *, count=2, concurrency=2):
    return prepare_candidate_transcripts(parser, _windows(count), output_dir=output_dir,
                                         transcript_model="lite", transcript_source="lite", concurrency=concurrency,
                                         on_log=lambda message: None)


def test_parallel_asr_preserves_job_observer_and_each_chunk_scope(tmp_path):
    barrier = Barrier(4)
    ledgers = {"job-a": [], "job-b": []}
    providers = {job: Responses(job, barrier=barrier) for job in ledgers}
    def run(job):
        with capture_parse_usage(ledgers[job].append), parse_chunk_scope(99, category="embedding"):
            return _prepare(Transcriber(providers[job]), tmp_path / job)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, job) for job in ledgers]
        assert all(len(future.result()) == 2 for future in futures)
    for job, ledger in ledgers.items():
        assert len(ledger) == len(providers[job].requests) == 2
        assert {item.sequence_no for item in ledger} == {0, 1}
        assert {item.call.model for item in ledger} == {job}
        assert all(item.category == "parse" and item.call.operation == "candidate_transcript" for item in ledger)
        assert all(item.call.usage.total_tokens == 12 for item in ledger)


def test_asr_failed_request_is_recorded_and_checkpoint_resume_only_counts_new_request(tmp_path):
    ledger = []
    provider = Responses("lite", fail_sequence=1)
    parser = Transcriber(provider)
    with capture_parse_usage(ledger.append), pytest.raises(RuntimeError, match="candidate ASR failed"):
        _prepare(parser, tmp_path)
    assert sorted((item.sequence_no, item.call.status) for item in ledger) == [(0, "completed"), (1, "failed")]
    assert next(item for item in ledger if item.sequence_no == 1).call.usage is None
    provider.fail_sequence = None
    ledger.clear()
    with capture_parse_usage(ledger.append):
        assert len(_prepare(parser, tmp_path)) == 2
        assert len(_prepare(parser, tmp_path)) == 2
    assert len(ledger) == 1
    assert ledger[0].sequence_no == 1
    assert len(provider.requests) == 3


def test_candidate_asr_continues_without_waiting_for_consumption(tmp_path):
    started: list[int] = []
    started_lock = Lock()
    started_events = [Event() for _ in range(4)]
    release_second = Event()

    class Parser:
        def transcribe(self, path, *, duration_seconds):
            sequence_no = int(path.stem.rsplit("-", 1)[1])
            with started_lock:
                started.append(sequence_no)
            started_events[sequence_no].set()
            if sequence_no == 1:
                assert release_second.wait(timeout=5)
            return TranscriptAnalysis(transcript=[])

    with stream_candidate_transcripts(
        Parser(),
        _windows(4),
        output_dir=tmp_path,
        transcript_model="lite",
        transcript_source="lite",
        concurrency=1,
        on_log=lambda _message: None,
    ) as transcripts:
        assert started_events[0].wait(timeout=5)
        assert started_events[1].wait(timeout=5)
        assert not started_events[2].is_set()

        release_second.set()
        assert started_events[2].wait(timeout=5)
        assert started_events[3].wait(timeout=5)
        assert [sequence_no for sequence_no, _ in transcripts] == [0, 1, 2, 3]
        assert started == [0, 1, 2, 3]

    assert sorted(path.name for path in (tmp_path / "transcripts").iterdir()) == [
        "chunk-0000.json",
        "chunk-0001.json",
        "chunk-0002.json",
        "chunk-0003.json",
    ]


def test_continuous_asr_worker_preserves_usage_scope_across_chunks(tmp_path):
    ledger = []
    provider = Responses("lite")
    with capture_parse_usage(ledger.append):
        transcripts = _prepare(
            Transcriber(provider),
            tmp_path,
            count=3,
            concurrency=1,
        )

    assert len(transcripts) == 3
    assert [item.sequence_no for item in ledger] == [0, 1, 2]
    assert all(item.call.status == "completed" for item in ledger)


def test_runtime_single_chunk_calls_preserve_the_chunk_usage_scope(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video fixture")
    window = AnalysisWindow(
        sequence_no=0,
        source_video=source,
        media_path=tmp_path / "window-0.mp4",
        chunk_start_seconds=0,
        chunk_end_seconds=1,
        analysis_start_seconds=0,
        analysis_end_seconds=1,
    )
    window.media_path.write_bytes(b"analysis fixture")

    class Model:
        provider_name = "ark"

        def __init__(self, model, **_kwargs):
            self.model_name = model

        def generate(self, _inputs, *, schema_name, result_type):
            with track_model_call(
                provider="ark",
                model=self.model_name,
                operation=schema_name,
            ):
                if result_type is TranscriptAnalysis:
                    return TranscriptAnalysis(transcript=[])
                return VideoTimeline(summary="summary", events=[])

    monkeypatch.setattr("engine.parsev1.runtime.ArkMultimodalModel", Model)
    runtime = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            fallback_enabled=False,
            transcript_model="lite",
            video_model="pro",
        )
    )
    compact = ContextSnapshot(
        snapshot_id="context-initial",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    ledger = []

    with capture_parse_usage(ledger.append):
        transcript = runtime.transcribe_chunk(window)
        runtime.understand_chunk(
            compact,
            window,
            transcript=transcript,
        )

    assert [(item.sequence_no, item.call.operation) for item in ledger] == [
        (0, "candidate_transcript"),
        (0, "video_timeline"),
    ]


def test_pipeline_counts_each_fallback_call_and_does_not_recount_completed_chunks():
    ledger = []
    class Parser:
        def understand(
            self,
            path,
            *,
            transcript,
            context,
            duration_seconds,
            background_images=(),
        ):
            with pytest.raises(RuntimeError), track_model_call(provider="ark", model="pro", operation="video_timeline"):
                raise RuntimeError("content filter")
            for operation in ["neutral_visual_evidence", "neutral_visual_evidence", "fallback_video_timeline"]:
                with track_model_call(provider="deepseek", model="fallback", operation=operation) as call:
                    call.capture_response(SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7)))
            return VideoEvidence(duration_seconds=duration_seconds, candidate_transcript=[], summary="summary", events=[])
    pipeline = UnderstandStage(Parser())
    request = dict(source_video="video.mp4", duration_seconds=2, chunk_duration_seconds=1,
                   context_overlap_seconds=0, windows=_windows())
    with capture_parse_usage(ledger.append):
        result = pipeline.run(**request)
        assert pipeline.run(**request, completed_chunks=result.chunks) == result
    assert len(ledger) == 8
    assert [item.sequence_no for item in ledger] == [0] * 4 + [1] * 4
    assert [item.call.status for item in ledger] == ["failed", "completed", "completed", "completed"] * 2
    assert all(item.category == "parse" for item in ledger)


def test_scope_reset_keeps_embedding_and_parse_separate_and_excludes_outside_calls():
    ledger, raw_calls = [], []
    with capture_model_usage(raw_calls.append):
        with capture_parse_usage(ledger.append):
            with parse_chunk_scope(3, category="embedding"):
                with track_model_call(provider="ark", model="embedding", operation="text_embedding"):
                    pass
            with track_model_call(provider="ark", model="lite", operation="candidate_transcript"):
                pass
        with track_model_call(provider="ark", model="embedding", operation="text_embedding"):
            pass
    with track_model_call(provider="ark", model="chat", operation="text_generation"):
        pass
    assert [(item.sequence_no, item.category) for item in ledger] == [(3, "embedding"), (None, "parse")]
    assert len(raw_calls) == 1
    assert raw_calls[0].operation == "text_embedding"
