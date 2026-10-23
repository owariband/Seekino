from array import array
import builtins
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest
from pydantic import ValidationError
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.config import GatewayConfig, ModelBinding, TracingConfig, load_config, required_api_key_env_names
from gateway.local.pyannote import PyannoteSpeakerDiarizer
from gateway.model_factory import GatewayClient
from gateway.speaker_diarization import SpeakerDiarizationResult, SpeakerDiarizationTurn
from gateway.tracing import LangfuseTracing
from gateway.usage import (
    ModelCallCancelledError, capture_model_usage, control_model_calls,
    model_call_context,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = "pyannote/speaker-diarization-3.1"


def _payload(tmp_path):
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    data["model_sources"]["diarization_cpu"] = {
        "adapter": "pyannote", "provider": "local",
        "pipeline_path": str(tmp_path / "config.yaml"),
    }
    data["models"]["diarization"] = {
        "source": "diarization_cpu", "model": MODEL,
        "kind": "speaker_diarization", "request_timeout_seconds": 10,
    }
    return data


def _wav(tmp_path, *, rate=16000, channels=1, width=2, seconds=1, silent=False):
    path = tmp_path / "prepared.wav"
    samples = int(rate * seconds) * channels
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        payload = bytes(samples * width) if silent or width != 2 else (
            array("h", [1000, -1000] * ((samples + 1) // 2)).tobytes()[:samples * width]
        )
        audio.writeframes(payload)
    return path


class _Annotation:
    def __init__(self, turns):
        self.turns = turns

    def itertracks(self, *, yield_label):
        assert yield_label
        for index, (start, end, speaker) in enumerate(self.turns):
            yield SimpleNamespace(start=start, end=end), index, speaker


def test_legacy_config_and_diarization_binding_keep_non_llm_defaults(tmp_path):
    legacy = load_config(ROOT / "tests/gateway/fixtures/legacy-config.yaml")
    assert "diarization" not in legacy.normalized_models
    data = _payload(tmp_path)
    data["models"]["diarization"].pop("request_timeout_seconds")
    config = GatewayConfig.model_validate(data)
    binding = config.resolve_model_binding("diarization", environ={})
    assert binding.kind == "speaker_diarization"
    assert binding.request_timeout_seconds == 600
    assert binding.max_output_tokens is binding.reasoning_effort is None
    assert binding.context_window_tokens is binding.context_safety_margin_tokens is None
    assert binding.auto_compact_tokens is binding.compact_target_tokens is binding.max_input_chars is None
    assert config.model_sources["diarization_cpu"].cpu_threads == 6
    assert config.model_sources["diarization_cpu"].max_audio_seconds == 300
    assert required_api_key_env_names(config) == required_api_key_env_names(
        GatewayConfig.model_validate(yaml.safe_load((ROOT / "config.yaml").read_text()))
    )
    assert GatewayConfig.model_validate(config.model_dump()) == config
    frozen = ModelBinding(source="diarization_cpu", model=MODEL, kind="speaker_diarization")
    assert frozen.request_timeout_seconds is None
    assert config.resolve_model_binding(frozen, environ={}) == binding
    assert config.resolve_model_binding(ModelBinding.model_validate(binding.model_dump()), environ={}) == binding


@pytest.mark.parametrize("threads", [6, 4])
def test_cpu_torch_adapters_share_one_process_thread_count(tmp_path, threads):
    data = _payload(tmp_path)
    data["model_sources"]["speaker_cpu"] = {
        "adapter": "campplus", "provider": "local",
        "checkpoint_path": str(tmp_path / "campplus.bin"), "cpu_threads": threads,
    }
    if threads == 6:
        assert GatewayConfig.model_validate(data).model_sources["speaker_cpu"].cpu_threads == 6
    else:
        with pytest.raises(ValidationError, match="cpu_threads"):
            GatewayConfig.model_validate(data)


@pytest.mark.parametrize("changes", [
    {"source": "ark_default"}, {"kind": "generative"}, {"kind": "speaker_embedding"},
    {"max_output_tokens": 1024}, {"reasoning_effort": "low"},
    {"supports_vision_input": True}, {"context_window_tokens": 64000},
    {"fallback": {"source": "diarization_cpu", "model": MODEL, "kind": "speaker_diarization"}},
])
def test_diarization_binding_rejects_wrong_capability_and_generation_controls(tmp_path, changes):
    data = _payload(tmp_path)
    data["models"]["diarization"].update(changes)
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(data)


def test_factory_creation_does_not_import_optional_inference_dependencies(tmp_path):
    script = """import builtins, json, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'torchaudio', 'pyannote', 'lightning', 'numpy'}:
        raise AssertionError('eager optional import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from gateway.config import GatewayConfig
from gateway.model_factory import GatewayClient
client = GatewayClient(GatewayConfig.model_validate(json.loads(sys.argv[1])), environ={})
model = client.create('speaker_diarization', 'diarization')
assert model.model_name == 'pyannote/speaker-diarization-3.1'
client.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_payload(tmp_path))],
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("turn", [
    {"start_ms": -1, "end_ms": 100, "speaker": "A"},
    {"start_ms": 100, "end_ms": 100, "speaker": "A"},
    {"start_ms": 101, "end_ms": 100, "speaker": "A"},
    {"start_ms": 0.5, "end_ms": 100, "speaker": "A"},
    {"start_ms": False, "end_ms": 100, "speaker": "A"},
    {"start_ms": 0, "end_ms": True, "speaker": "A"},
    {"start_ms": 0, "end_ms": 100, "speaker": ""},
])
def test_turn_result_requires_ordered_integer_milliseconds_and_anonymous_speaker(turn):
    with pytest.raises(ValidationError):
        SpeakerDiarizationTurn.model_validate(turn)


@pytest.mark.parametrize("duration,turns", [
    (0, []), (1.5, []), (True, []),
    (100, [{"start_ms": 0, "end_ms": 101, "speaker": "A"}]),
    (100, [{"start_ms": 50, "end_ms": 100, "speaker": "A"},
           {"start_ms": 0, "end_ms": 50, "speaker": "B"}]),
])
def test_result_rejects_invalid_duration_outside_audio_and_unsorted_turns(duration, turns):
    with pytest.raises(ValidationError):
        SpeakerDiarizationResult(model=MODEL, model_revision="fixture", duration_ms=duration, turns=turns)


def test_factory_reuses_pipeline_preserves_sequential_speakers_and_overlap_and_traces_span(monkeypatch, tmp_path):
    loads, forwards, calls = [], [], []

    def load(model):
        loads.append(model)
        model.model_revision = "fixture-revision"

        def forward(pcm, *, hook):
            forwards.append(pcm)
            hook("segmentation", None)
            return _Annotation([(0.7, 1.0, "A"), (0.3, 0.7, "B"), (0.0, 0.4, "A")])

        return forward

    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline", load)
    client = GatewayClient(GatewayConfig.model_validate(_payload(tmp_path)), environ={})
    model = client.create_speaker_diarization_model("diarization")
    audio = _wav(tmp_path)
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    try:
        with capture_model_usage(calls.append), model_call_context({"owner": "test"}), tracing.scope({}):
            first = model.diarize_audio(audio)
            second = model.diarize_audio(audio)
        assert first == second
        assert first.model == MODEL and first.model_revision == "fixture-revision"
        assert first.duration_ms == 1000
        assert [(turn.start_ms, turn.end_ms, turn.speaker) for turn in first.turns] == [
            (0, 400, "A"), (300, 700, "B"), (700, 1000, "A"),
        ]
        assert len(loads) == 1 and len(forwards) == 2 and len(forwards[0]) == 32000
        assert len(calls) == 2
        assert all(call.context == {"owner": "test", "src": "diarization_cpu"} for call in calls)
        assert all(call.operation == "speaker_diarization" and call.status == "completed"
                   and call.usage is None for call in calls)
        span = next(span for span in exporter.get_finished_spans() if span.name == "speaker_diarization")
        assert span.attributes["langfuse.observation.type"] == "span"
        assert span.attributes["langfuse.observation.metadata.usage_status"] == "unknown"
        assert "langfuse.observation.usage_details" not in span.attributes
        with pytest.raises(ValueError, match="not generative"):
            client.create_generative_model("diarization")
    finally:
        tracing.close()
        client.close()
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.diarize_audio(audio)
    with pytest.raises(RuntimeError, match="closed"):
        client.create("speaker_diarization", "diarization")


@pytest.mark.parametrize("seconds,silent", [(0.1, False), (1, True), (13.3, False)])
def test_short_silent_and_more_than_one_window_audio_are_valid_inputs(monkeypatch, tmp_path, seconds, silent):
    lengths = []

    def forward(pcm, *, hook):
        lengths.append(len(pcm))
        return _Annotation([])

    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline", lambda _model: forward)
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=tmp_path / "unused.yaml", request_timeout_seconds=10)
    try:
        result = model.diarize_audio(_wav(tmp_path, seconds=seconds, silent=silent))
        assert result.duration_ms == round(seconds * 1000) and result.turns == []
        assert lengths == [round(seconds * 16000) * 2]
    finally:
        model.close()


@pytest.mark.parametrize("options", [
    {"rate": 8000}, {"channels": 2}, {"width": 1}, {"seconds": 0},
    {"seconds": 1.1}, {"truncate": True},
])
def test_invalid_audio_fails_before_loading_or_consuming_model_call(monkeypatch, tmp_path, options):
    def unexpected(_model):
        raise AssertionError("invalid audio reached pipeline loading")

    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline", unexpected)
    audio = _wav(tmp_path, **{key: value for key, value in options.items() if key != "truncate"})
    if options.get("truncate"):
        audio.write_bytes(audio.read_bytes()[:-10])
    model = PyannoteSpeakerDiarizer(
        MODEL, pipeline_path=tmp_path / "unused.yaml", request_timeout_seconds=10, max_audio_seconds=1,
    )
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(ValueError):
            model.diarize_audio(audio)
        assert calls == []
    finally:
        model.close()


@pytest.mark.parametrize("start,end", [(float("nan"), 1.0), (0.0, float("inf")), (0.5, 0.4), (2.0, 3.0)])
def test_invalid_model_timestamps_fail_and_are_recorded(monkeypatch, tmp_path, start, end):
    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline",
                        lambda _model: lambda _pcm, **_kwargs: _Annotation([(start, end, "A")]))
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=tmp_path / "unused.yaml", request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises((ValueError, RuntimeError)):
            model.diarize_audio(_wav(tmp_path))
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


@pytest.mark.parametrize("mode,error", [("cancel", ModelCallCancelledError), ("timeout", TimeoutError)])
def test_pipeline_late_results_are_rejected_and_release_host_control(monkeypatch, tmp_path, mode, error):
    clock, events, calls = [0.0], [], []

    class Control:
        def acquire(self, call):
            self.call = call
            events.append("acquire")

        def release(self, call):
            assert call is self.call
            events.append("release")

    control = Control()

    def forward(_pcm, *, hook):
        if mode == "cancel":
            control.call.cancel()
        else:
            clock[0] = 11.0
        return _Annotation([])

    monkeypatch.setattr("gateway.local.pyannote.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline", lambda _model: forward)
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=tmp_path / "unused.yaml", request_timeout_seconds=10)
    try:
        with control_model_calls(control), capture_model_usage(calls.append), pytest.raises(error):
            model.diarize_audio(_wav(tmp_path))
        assert events == ["acquire", "release"]
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


def test_missing_local_pipeline_fails_before_optional_imports(monkeypatch, tmp_path):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "torchaudio", "pyannote", "numpy"}:
            raise AssertionError("missing local model must fail without inference imports or downloads")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=tmp_path / "missing.yaml", request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(FileNotFoundError):
            model.diarize_audio(_wav(tmp_path))
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


@pytest.mark.parametrize("change", [
    "remote_segmentation", "remote_embedding", "missing_segmentation", "preprocessor", "other_pipeline",
])
def test_pipeline_config_cannot_load_remote_models_or_external_preprocessors(monkeypatch, tmp_path, change):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "torchaudio", "pyannote", "numpy"}:
            raise AssertionError("unsafe model references must fail before inference imports or downloads")
        return original(name, *args, **kwargs)

    checkpoint = tmp_path / "checkpoint.bin"
    checkpoint.write_bytes(b"local checkpoint fixture")
    config = {
        "version": "3.1.0",
        "pipeline": {
            "name": "pyannote.audio.pipelines.SpeakerDiarization",
            "params": {"segmentation": str(checkpoint), "embedding": str(checkpoint)},
        },
        "params": {"segmentation": {"min_duration_off": 0.0}},
    }
    if change.startswith("remote_"):
        config["pipeline"]["params"][change.removeprefix("remote_")] = "pyannote/gated-model"
    elif change == "missing_segmentation":
        config["pipeline"]["params"].pop("segmentation")
    elif change == "preprocessor":
        config["preprocessors"] = {"audio": "external audio loader"}
    else:
        config["pipeline"]["name"] = "external.module.Pipeline"
    pipeline_path = tmp_path / "config.yaml"
    pipeline_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(builtins, "__import__", guarded)
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=pipeline_path, request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(ValueError):
            model.diarize_audio(_wav(tmp_path))
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


@pytest.mark.parametrize("during_load", [True, False])
def test_pipeline_failures_propagate_and_are_recorded(monkeypatch, tmp_path, during_load):
    failure = RuntimeError("pipeline failed")

    def forward(_pcm, **_kwargs):
        raise failure

    def load(_model):
        if during_load:
            raise failure
        return forward

    monkeypatch.setattr(PyannoteSpeakerDiarizer, "_load_pipeline", load)
    model = PyannoteSpeakerDiarizer(MODEL, pipeline_path=tmp_path / "unused.yaml", request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(RuntimeError) as caught:
            model.diarize_audio(_wav(tmp_path))
        assert caught.value is failure
        assert len(calls) == 1 and calls[0].status == "failed" and calls[0].usage is None
    finally:
        model.close()
