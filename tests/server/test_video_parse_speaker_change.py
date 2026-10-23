"""Host integration: original PCM, frozen bindings and advisory evidence."""

import asyncio
from array import array
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from agent.utils.tool import as_langchain_tools
from agent.video_parse_v2.input import ParseInput, SpeakerEmbeddingModelConfig
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from engine.parsev2.schemas import VideoParseResult
from gateway.speaker_embedding import SpeakerEmbeddingResult
from server.config.parse_profile import default_video_parse_v2_config
from server.constants.parse import SPEAKER_CONSISTENCY_PARAMETERS
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_media import PreparedVideoParseMedia
from server.utils.execution import BlockingCallCancelled, ExecutionCalls, execution_calls


@pytest.fixture
def host(tmp_path, monkeypatch):
    calls = SimpleNamespace(crops=[], embeddings=[], encoders=[], transports=[], checks=0, fail=False, pause=False)
    config = default_video_parse_v2_config()
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "asr": config.models.asr.model_copy(update={"mode": "none"}),
        "speaker_embedding": SpeakerEmbeddingModelConfig(
            source="frozen-camplus", model="campplus", request_timeout_seconds=60,
        ),
    })})
    inputs = ParseInput(series_id="series", video_id="video", parse_config=config)
    execution = ChunkExecution(
        "member", "video-task", 0, 35000, 49000, 35000, 49000, inputs,
        LeaseToken("worker", 1, datetime.now(UTC) + timedelta(minutes=1)),
    )
    original = {"transcript": [{
        "start_seconds": 35300, "end_seconds": 48500, "text": "original mixed cue",
    }]}
    canvas = ParseContextCanvas(
        scope="chunk:member", stage="understanding", evidence={"member": {"asr": deepcopy(original), "ocr": None}},
        members={"member": {"sequence_no": 0, "version": 0, "bounds": (35000, 49000),
                            "content": VideoParseResult(persons=[], timeline=[])}},
    )
    media = PreparedVideoParseMedia(tmp_path / "core.mp4", tmp_path / "aac.wav", ())
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")

    @contextmanager
    def crop(prepared, *, source_video, start_ms, end_ms, input_mode, check_active, **_kwargs):
        assert prepared is media and source_video == source
        check_active()
        path = tmp_path / f"crop-{len(calls.crops)}.{ 'wav' if input_mode == 'audio' else 'mp4'}"
        calls.crops.append((start_ms, end_ms, input_mode, path))
        if input_mode == "audio":
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(array("h", (
                    0 if calls.pause and 37000 <= start_ms + i // 16 < 39000 else (start_ms + i // 16) % 30000
                    for i in range((end_ms - start_ms) * 16)
                )).tobytes())
        else:
            path.write_bytes(b"cropped video")
        try:
            yield path
        finally:
            path.unlink()
            path.with_name("consistency-window.wav").unlink(missing_ok=True)

    class Encoder:
        model_name = "campplus"
        encoder_revision = "frozen-192"
        closed = False

        def embed_audio(self, path):
            with wave.open(str(path), "rb") as audio:
                assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
                pcm = audio.readframes(audio.getnframes())
            calls.embeddings.append(pcm)
            if calls.fail:
                raise RuntimeError("encoder failed")
            # A deliberately simple changing signal tests transport, not model accuracy.
            values = array("h", pcm)
            vector = [0.0] * 192
            vector[int(values[0] >= 16400)] = 1.0
            return SpeakerEmbeddingResult(
                model=self.model_name, encoder_revision=self.encoder_revision,
                vector=vector, duration_ms=len(pcm) // 32,
            )

        def close(self):
            self.closed = True

    def encoder(binding):
        assert binding is config.models.speaker_embedding
        result = Encoder()
        calls.encoders.append(result)
        return result

    def active(*_args):
        calls.checks += 1

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._model_manager = ContextVar("speaker-test-manager", default=None)
    runtime._media_service = SimpleNamespace(prepare_chunk_tool_media=crop)
    runtime._artifact_store = SimpleNamespace(resolve=lambda ref: source if ref == "original-ref" else pytest.fail(ref))
    monkeypatch.setattr(runtime, "_require_chunk_execution", lambda _execution: SimpleNamespace(
        state=SimpleNamespace(
            source_media_ref="proxy-must-not-be-used",
            task_input=SimpleNamespace(original_source_uri="original-ref", content_hash="original-content-hash"),
        ), lease=object(),
    ))
    monkeypatch.setattr(runtime, "_require_chunk_active", active)
    monkeypatch.setattr(runtime, "_speaker_encoder", encoder)

    async def prepared(_execution):
        return media

    monkeypatch.setattr(runtime, "_ensure_chunk_media", prepared)
    return SimpleNamespace(runtime=runtime, execution=execution, media=media, canvas=canvas,
                           original=original, calls=calls, config=config)


def _set_models(host, **updates):
    host.config = host.config.model_copy(update={"models": host.config.models.model_copy(update=updates)})
    host.execution = replace(host.execution, inputs=host.execution.inputs.model_copy(update={"parse_config": host.config}))


def _tools(host):
    return host.runtime._understanding_audio_tools(
        host.execution, host.media, host.canvas, host.original,
    )


def _invoke(tools, name, start=35300, end=48500):
    return json.loads(next(tool for tool in as_langchain_tools(tools) if tool.name == name).invoke({
        "start_ms": start, "end_ms": end,
    }))


def test_prescan_reuses_original_pcm_encoder_and_frozen_grid_without_filter(host):
    before = deepcopy(host.original)
    with _tools(host) as (tools, summary):
        assert "speaker_consistency:" in summary
        result = _invoke(tools, "analyze_speaker_consistency")
        assert result["window_count"] == 31
        assert result["parameters"].items() >= SPEAKER_CONSISTENCY_PARAMETERS.items()
        assert result["parameters"]["vad_filter"] is False
        assert result["parameters"]["window_ms"] == 1000
        assert result["parameters"]["hop_ms"] == 400
        assert result["single_speaker_verified"] is False
        assert result["source_content_hash"] == "original-content-hash"
        assert result["score_is_probability"] is False
        assert "matrix" not in result and "vectors" not in result
        assert len(host.calls.embeddings) == 31
        assert len(host.calls.encoders) == 1
        assert host.calls.crops[0][:3] == (35000, 49000, "audio")
        assert _invoke(tools, "analyze_speaker_consistency") == result
        _invoke(tools, "analyze_speaker_consistency", 35700, 48100)
        assert len(host.calls.embeddings) == 31  # overlapping requests reuse identical PCM
        cached = _invoke(tools, "analyze_speaker_consistency")
        assert cached["evidence_ref"] == result["evidence_ref"]
        assert cached["revision"] == host.canvas.revision > result["revision"]
        assert len(host.calls.crops) == 1
        assert host.calls.encoders[0].closed is False
    assert host.calls.encoders[0].closed
    assert not host.calls.crops[0][3].exists()
    assert host.original == before
    persisted = host.canvas.understanding_evidence("member")
    assert persisted["asr"] == before
    assert result["evidence_ref"] in persisted["tool_evidence"]


def test_short_and_invalid_ranges_do_not_invent_single_speaker_evidence(host):
    host.original["transcript"] = []
    with _tools(host) as (tools, _summary):
        result = _invoke(tools, "analyze_speaker_consistency", 48000, 48500)
        assert result["window_count"] == 0 and result["candidates"] == []
        assert result["uncovered"] == [{"start_ms": 48000, "end_ms": 48500}]
        assert result["single_speaker_verified"] is False
        assert not host.calls.encoders
        rejected = _invoke(tools, "analyze_speaker_consistency", 34000, 36000)
        assert rejected["error"]["code"] == "INVALID_AUDIO_RANGE"


def test_encoder_failure_is_visible_and_closes_resources(host):
    host.calls.fail = True
    with pytest.raises(RuntimeError, match="encoder failed"):
        with _tools(host):
            pytest.fail("failed analysis cannot be represented as no candidates")
    assert host.calls.encoders[0].closed
    assert not host.calls.crops[0][3].exists()
    assert "tool_evidence" not in host.canvas.understanding_evidence("member")


def test_constant_pause_reports_unavailable_without_filtering_or_failing_chunk(host):
    host.calls.pause = True
    with _tools(host) as (tools, summary):
        result = _invoke(tools, "analyze_speaker_consistency")
        assert result["analysis_status"] == "unavailable"
        assert result["window_count"] == 31  # the grid is retained, not speech-filtered
        assert result["unencodable_windows"]
        assert all(window["reason"] == "constant_pcm" for window in result["unencodable_windows"])
        assert result["uncovered"] == [{"start_ms": 35300, "end_ms": 48500}]
        assert not result["single_speaker_verified"] and not result["parameters"]["vad_filter"]
        assert '"analysis_status": "unavailable"' in summary
        assert not host.calls.embeddings
    assert not host.calls.crops[0][3].exists()


@pytest.mark.parametrize("input_mode", ["audio", "video"])
def test_local_asr_uses_frozen_binding_correct_media_and_one_time_offset(host, input_mode):
    _set_models(host, asr=host.config.models.asr.model_copy(update={"mode": "lite"}), speaker_embedding=None)
    calls = host.calls

    class Transport:
        model_name = "asr-model"
        closed = False

        def transcribe(self, path):
            assert input_mode == "audio" and Path(path).suffix == ".wav"
            return {"segments": [{"start": 0.2, "end": 1.1, "text": "new", "speaker_id": "speaker1"}]}

        def generate(self, inputs, **kwargs):
            assert input_mode == "video" and inputs[0].path.suffix == ".mp4"
            return kwargs["result_type"].model_validate({"transcript": [{
                "start_seconds": 0.2, "end_seconds": 1.1, "text": "new", "speaker_id": "speaker1",
            }]})

        def close(self):
            self.closed = True

    def create(binding):
        assert binding.kind == "transcription" and binding.model == host.config.models.asr.model
        transport = Transport()
        transport.input_mode = input_mode
        calls.transports.append(transport)
        return transport

    host.runtime._gateway = SimpleNamespace(create_transcription_model=create)
    before = deepcopy(host.original)
    with _tools(host) as (tools, _summary):
        assert {tool.name for tool in tools} == {"get_context_canvas", "write_context_canvas", "transcribe_segment"}
        first = _invoke(tools, "transcribe_segment", 46000, 48000)
        assert first["asr"]["transcript"] == [{
            "start_seconds": 46200, "end_seconds": 47100, "text": "new", "speaker_id": "speaker1",
        }]
        assert _invoke(tools, "transcribe_segment", 46000, 48000)["evidence_ref"] == first["evidence_ref"]
        assert host.canvas.result("member").timeline == []  # evidence does not write events
    assert host.original == before
    assert all(transport.closed for transport in calls.transports)
    assert all(not crop[3].exists() for crop in calls.crops)
    persisted = host.canvas.understanding_evidence("member")
    assert persisted["asr"] == before
    assert persisted["tool_evidence"][first["evidence_ref"]]["kind"] == "local_asr"


def test_unavailable_bindings_do_not_use_global_model_or_media(host):
    _set_models(host, speaker_embedding=None)
    with _tools(host) as (tools, summary):
        assert {tool.name for tool in tools} == {"get_context_canvas", "write_context_canvas"}
        assert "unavailable" in summary
    assert not host.calls.crops and not host.calls.encoders


def test_understanding_sees_prescan_before_generation_and_closes_encoder(host, monkeypatch):
    async def generate(_binding, inputs, **kwargs):
        assert host.calls.embeddings
        assert host.canvas.understanding_evidence("member")["tool_evidence"]
        assert "Host audio review" in inputs[-1].text
        assert {tool.name for tool in kwargs["tools"]} == {
            "get_context_canvas", "write_context_canvas", "analyze_speaker_consistency",
        }
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_multimodal", generate)
    asyncio.run(host.runtime.understand_chunk(
        host.execution, asr_result=host.original, ocr_result=None, prompt="understand", canvas=host.canvas,
    ))
    assert host.calls.encoders[0].closed


def test_cancellation_retains_audio_until_blocking_encoder_exits(host, monkeypatch):
    started, release, closed = Event(), Event(), Event()
    cancelled = Event()

    class Manager:
        def check_active(self):
            if cancelled.is_set():
                raise RuntimeError("cancelled")

        def cancel(self):
            cancelled.set()

    class SlowEncoder:
        model_name = "campplus"
        encoder_revision = "frozen-192"

        def embed_audio(self, path):
            started.set()
            assert release.wait(5), "test did not release inference"
            assert Path(path).exists()  # media must survive cancellation of its waiter
            return SpeakerEmbeddingResult(
                model=self.model_name, encoder_revision=self.encoder_revision,
                vector=[1.0] + [0.0] * 191, duration_ms=1000,
            )

        def close(self):
            closed.set()

    monkeypatch.setattr(host.runtime, "_speaker_encoder", lambda _binding: SlowEncoder())

    async def run():
        context = ExecutionCalls()
        calls_token = execution_calls.set(context)
        manager_token = host.runtime._model_manager.set(Manager())
        try:
            task = asyncio.create_task(host.runtime.understand_chunk(
                host.execution, asr_result=host.original, ocr_result=None,
                prompt="understand", canvas=host.canvas,
            ))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            task.cancel()
            with pytest.raises(BlockingCallCancelled) as error:
                await task
            assert not closed.is_set() and host.calls.crops[0][3].exists()
            release.set()
            await asyncio.wait_for(asyncio.wrap_future(error.value.completion), timeout=5)
            assert closed.is_set() and not host.calls.crops[0][3].exists()
            assert "tool_evidence" not in host.canvas.understanding_evidence("member")
        finally:
            release.set()
            execution_calls.reset(calls_token)
            host.runtime._model_manager.reset(manager_token)

    asyncio.run(run())
