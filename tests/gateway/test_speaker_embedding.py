from array import array
from pathlib import Path
import subprocess
import sys
import wave

import pytest
from pydantic import ValidationError
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.config import GatewayConfig, ModelBinding, TracingConfig, load_config, required_api_key_env_names
from gateway.local.campplus import CampPlusSpeakerEmbedder
from gateway.model_factory import GatewayClient
from gateway.speaker_embedding import SpeakerEmbeddingResult
from gateway.tracing import LangfuseTracing
from gateway.usage import (
    ModelCallCancelledError, capture_model_usage, control_model_calls,
    model_call_context,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = "iic/speech_campplus_sv_zh-cn_16k-common"


def _payload(tmp_path):
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    data["model_sources"]["voice_cpu"] = {
        "adapter": "campplus", "provider": "local",
        "checkpoint_path": str(tmp_path / "campplus.bin"),
    }
    data["models"]["voice_probe"] = {
        "source": "voice_cpu", "model": MODEL, "kind": "speaker_embedding",
        "request_timeout_seconds": 10,
    }
    return data


def _wav(tmp_path, *, rate=16000, channels=1, width=2, seconds=1, silent=False, constant=False):
    path = tmp_path / "prepared.wav"
    samples = int(rate * seconds) * channels
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        payload = bytes(samples * width) if silent else (
            array("h", [1000, 1000 if constant else -1000] * ((samples + 1) // 2)).tobytes()[:samples * width]
        )
        audio.writeframes(payload)
    return path


def test_legacy_config_still_loads_and_voice_binding_has_no_llm_defaults(tmp_path):
    legacy = load_config(ROOT / "tests/gateway/fixtures/legacy-config.yaml")
    assert "voice_probe" not in legacy.normalized_models
    data = _payload(tmp_path)
    data["models"]["voice_probe"].pop("request_timeout_seconds")
    config = GatewayConfig.model_validate(data)
    binding = config.resolve_model_binding("voice_probe", environ={})
    assert binding.kind == "speaker_embedding"
    assert binding.request_timeout_seconds == 600
    assert binding.max_output_tokens is binding.context_window_tokens is None
    assert binding.context_safety_margin_tokens is binding.auto_compact_tokens is None
    assert binding.compact_target_tokens is binding.max_input_chars is None
    assert config.model_sources["voice_cpu"].cpu_threads == 6
    assert config.model_sources["voice_cpu"].max_audio_seconds == 30
    assert required_api_key_env_names(config) == required_api_key_env_names(
        GatewayConfig.model_validate(yaml.safe_load((ROOT / "config.yaml").read_text()))
    )
    assert GatewayConfig.model_validate(config.model_dump()) == config
    frozen = ModelBinding(source="voice_cpu", model=MODEL, kind="speaker_embedding")
    resolved = config.resolve_model_binding(frozen, environ={})
    assert frozen.request_timeout_seconds is None and resolved == binding
    assert config.resolve_model_binding(ModelBinding.model_validate(resolved.model_dump()), environ={}) == resolved


@pytest.mark.parametrize("threads", [6, 4])
def test_campplus_sources_share_one_process_wide_thread_count(tmp_path, threads):
    data = _payload(tmp_path)
    data["model_sources"]["other_voice"] = {**data["model_sources"]["voice_cpu"], "cpu_threads": threads}
    if threads == 6:
        assert GatewayConfig.model_validate(data).model_sources["other_voice"].cpu_threads == 6
    else:
        with pytest.raises(ValidationError, match="cpu_threads"):
            GatewayConfig.model_validate(data)


@pytest.mark.parametrize("changes", [
    {"source": "ark_default"},
    {"kind": "generative"}, {"max_output_tokens": 1024},
    {"reasoning_effort": "low"}, {"supports_vision_input": True},
    {"context_window_tokens": 64000},
    {"fallback": {"source": "voice_cpu", "model": MODEL, "kind": "speaker_embedding"}},
])
def test_voice_config_rejects_wrong_capability_and_generation_controls(tmp_path, changes):
    data = _payload(tmp_path)
    data["models"]["voice_probe"].update(changes)
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(data)


def test_import_config_and_factory_do_not_load_optional_inference_dependencies(tmp_path):
    import json
    script = """import builtins, json, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'torchaudio', 'speakerlab', 'numpy'}:
        raise AssertionError('eager optional import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from gateway.config import GatewayConfig
from gateway.model_factory import GatewayClient
config = GatewayConfig.model_validate(json.loads(sys.argv[1]))
client = GatewayClient(config, environ={})
model = client.create('speaker_embedding', 'voice_probe')
assert model.model_name == 'iic/speech_campplus_sv_zh-cn_16k-common'
client.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_payload(tmp_path))],
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("vector,duration", [([float("nan")], 1000), ([float("inf")], 1000),
    ([], 1000), ([1.0], 1.5), ([1.0], -1), ([1.0], True)])
def test_result_rejects_invalid_vectors_and_non_integer_millisecond_duration(vector, duration):
    with pytest.raises(ValidationError):
        SpeakerEmbeddingResult(model=MODEL, encoder_revision="revision", vector=vector,
                               duration_ms=duration)


def test_factory_tracks_source_reuses_encoder_and_closes_resources(monkeypatch, tmp_path):
    loads, forwards, calls = [], [], []

    def load(model):
        loads.append(model)
        return lambda pcm: forwards.append(pcm) or [3.0, 4.0] + [0.0] * 190

    monkeypatch.setattr(CampPlusSpeakerEmbedder, "_load_encoder", load)
    client = GatewayClient(GatewayConfig.model_validate(_payload(tmp_path)), environ={})
    model = client.create_speaker_embedding_model("voice_probe")
    audio = _wav(tmp_path)
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    try:
        with capture_model_usage(calls.append), model_call_context({"owner": "test"}), tracing.scope({}):
            first = model.embed_audio(audio)
            second = model.embed_audio(audio)
        assert first == second
        assert first.model == MODEL and first.dimension == 192 and first.duration_ms == 1000
        assert first.vector[:2] == pytest.approx([0.6, 0.8])
        assert first.encoder_revision
        assert len(loads) == 1 and len(forwards) == 2 and len(forwards[0]) == 32000
        assert len(calls) == 2
        assert all(call.context == {"owner": "test", "src": "voice_cpu"} for call in calls)
        assert all(call.operation == "speaker_embedding" and call.status == "completed"
                   and call.usage is None for call in calls)
        embedding = exporter.get_finished_spans()[0]
        assert embedding.attributes["langfuse.observation.type"] == "embedding"
        assert embedding.attributes["langfuse.observation.metadata.usage_status"] == "unknown"
        assert "langfuse.observation.usage_details" not in embedding.attributes
        with pytest.raises(ValueError, match="not generative"):
            client.create_generative_model("voice_probe")
    finally:
        tracing.close()
        client.close()
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.embed_audio(audio)
    with pytest.raises(RuntimeError, match="closed"):
        client.create("speaker_embedding", "voice_probe")


def test_unsupported_checkpoint_identity_fails_before_loading(tmp_path):
    data = _payload(tmp_path)
    data["models"]["voice_probe"]["model"] = "unsupported-model"
    client = GatewayClient(GatewayConfig.model_validate(data), environ={})
    try:
        with pytest.raises(ValueError, match="only supports"):
            client.create_speaker_embedding_model("voice_probe")
    finally:
        client.close()


@pytest.mark.parametrize("options", [
    {"rate": 8000}, {"channels": 2}, {"width": 1}, {"seconds": 0.2},
    {"seconds": 31}, {"silent": True}, {"constant": True}, {"truncate": True},
])
def test_invalid_audio_does_not_load_encoder_or_consume_model_call(monkeypatch, tmp_path, options):
    def unexpected(_model):
        raise AssertionError("invalid audio reached model loading")

    monkeypatch.setattr(CampPlusSpeakerEmbedder, "_load_encoder", unexpected)
    audio = _wav(tmp_path, **{key: value for key, value in options.items() if key != "truncate"})
    if options.get("truncate"):
        audio.write_bytes(audio.read_bytes()[:-10])
    model = CampPlusSpeakerEmbedder(MODEL, checkpoint_path=tmp_path / "unused.bin",
                                   request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(ValueError):
            model.embed_audio(audio)
        assert calls == []
    finally:
        model.close()


@pytest.mark.parametrize("mode,error", [("cancel", ModelCallCancelledError), ("timeout", TimeoutError)])
def test_late_results_are_rejected_and_release_host_control(monkeypatch, tmp_path, mode, error):
    clock, events, calls = [0.0], [], []

    class Control:
        def acquire(self, call):
            self.call = call
            events.append("acquire")

        def release(self, call):
            assert call is self.call
            events.append("release")

    control = Control()

    def forward(_pcm):
        if mode == "cancel":
            control.call.cancel()
        else:
            clock[0] = 11.0
        return [1.0] + [0.0] * 191

    monkeypatch.setattr("gateway.local.campplus.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(CampPlusSpeakerEmbedder, "_load_encoder", lambda _model: forward)
    model = CampPlusSpeakerEmbedder(MODEL, checkpoint_path=tmp_path / "unused.bin",
                                   request_timeout_seconds=10)
    try:
        with control_model_calls(control), capture_model_usage(calls.append), pytest.raises(error):
            model.embed_audio(_wav(tmp_path))
        assert events == ["acquire", "release"]
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


def test_corrupt_checkpoint_is_rejected_without_loading_optional_dependencies(monkeypatch, tmp_path):
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"numpy", "torch", "torchaudio", "speakerlab"}:
            raise AssertionError("checkpoint must be validated before inference imports")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    checkpoint = tmp_path / "corrupt.bin"
    checkpoint.write_bytes(b"not the official checkpoint")
    model = CampPlusSpeakerEmbedder(MODEL, checkpoint_path=checkpoint, request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(ValueError, match="SHA|sha|checksum"):
            model.embed_audio(_wav(tmp_path))
        assert len(calls) == 1 and calls[0].status == "failed"
    finally:
        model.close()


@pytest.mark.parametrize("changed", ["speakerlab.models.campplus.DTDNN",
    "speakerlab.models.campplus.layers", "speakerlab.process.processor"])
def test_modified_official_implementation_is_rejected_and_recorded(monkeypatch, tmp_path, changed):
    import hashlib
    from types import ModuleType
    from gateway.local import campplus

    content = b"# pinned implementation fixture\n"
    expected = hashlib.sha256(content).hexdigest()
    pinned = []
    for name, _ in campplus.CAMPPLUS_IMPLEMENTATION_SHA256:
        source = tmp_path / (name + ".py")
        source.write_bytes(b"# replaced implementation\n" if name == changed else content)
        module = ModuleType(name)
        module.__file__ = str(source)
        module.CAMPPlus = module.FBank = object
        monkeypatch.setitem(sys.modules, name, module)
        pinned.append((name, expected))
    for name in ("numpy", "torch"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setattr(campplus, "CAMPPLUS_IMPLEMENTATION_SHA256", tuple(pinned))
    checkpoint = tmp_path / "checkpoint.bin"
    checkpoint.write_bytes(b"checkpoint fixture")
    monkeypatch.setattr(campplus, "CAMPPLUS_CHECKPOINT_SHA256", hashlib.sha256(checkpoint.read_bytes()).hexdigest())
    model = CampPlusSpeakerEmbedder(MODEL, checkpoint_path=checkpoint, request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(ValueError, match=changed):
            model.embed_audio(_wav(tmp_path))
        assert len(calls) == 1 and calls[0].status == "failed" and model._encode is None
    finally:
        model.close()


@pytest.mark.parametrize("during_load", [True, False])
def test_inference_failures_propagate_and_are_recorded(monkeypatch, tmp_path, during_load):
    failure = RuntimeError("encoder failed")

    def forward(_pcm):
        raise failure

    def load(_model):
        if during_load:
            raise failure
        return forward

    monkeypatch.setattr(CampPlusSpeakerEmbedder, "_load_encoder", load)
    model = CampPlusSpeakerEmbedder(MODEL, checkpoint_path=tmp_path / "unused.bin",
                                   request_timeout_seconds=10)
    calls = []
    try:
        with capture_model_usage(calls.append), pytest.raises(RuntimeError) as caught:
            model.embed_audio(_wav(tmp_path))
        assert caught.value is failure
        assert len(calls) == 1 and calls[0].status == "failed" and calls[0].usage is None
    finally:
        model.close()
