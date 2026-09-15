import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from engine.parse.checkpoints import prepare_candidate_transcripts
from engine.parse.schemas import AnalysisWindow, TranscriptAnalysis, VideoEvidence
from engine.parse.usage import capture_parse_usage, parse_chunk_scope
from engine.parse.video_parse import ParsePipeline
from gateway.models import ArkMultimodalModel, TextInput
from gateway.usage import capture_model_usage, track_model_call


def _windows():
    return [
        AnalysisWindow(sequence_no=sequence, source_video=Path("video.mp4"),
                       media_path=Path(f"window-{sequence}.mp4"), chunk_start_ms=sequence * 1000,
                       chunk_end_ms=(sequence + 1) * 1000, analysis_start_ms=sequence * 1000,
                       analysis_end_ms=(sequence + 1) * 1000)
        for sequence in range(2)
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

    def transcribe(self, path, *, duration_ms):
        sequence = int(path.stem.rsplit("-", 1)[1])
        return self.model.generate([TextInput(str(sequence))], schema_name="candidate_transcript", result_type=TranscriptAnalysis)


def _prepare(parser, output_dir):
    return prepare_candidate_transcripts(parser, _windows(), output_dir=output_dir,
                                         transcript_model="lite", transcript_source="lite", concurrency=2,
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


def test_pipeline_counts_each_fallback_call_and_does_not_recount_completed_chunks():
    ledger = []
    class Parser:
        def analyze(self, path, *, context, duration_ms, candidate_transcript, background_images=()):
            with pytest.raises(RuntimeError), track_model_call(provider="ark", model="pro", operation="video_timeline"):
                raise RuntimeError("content filter")
            for operation in ["neutral_visual_evidence", "neutral_visual_evidence", "fallback_video_timeline"]:
                with track_model_call(provider="deepseek", model="fallback", operation=operation) as call:
                    call.capture_response(SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7)))
            return VideoEvidence(duration_ms=duration_ms, candidate_transcript=[], summary="summary", events=[])
    pipeline = ParsePipeline(Parser())
    request = dict(source_video="video.mp4", duration_ms=2000, chunk_duration_ms=1000,
                   context_overlap_ms=0, windows=_windows())
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
