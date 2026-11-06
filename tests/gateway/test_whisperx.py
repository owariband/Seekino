from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
import yaml

from gateway.config import ModelBinding, WhisperXSourceConfig, load_config
from gateway.local.whisperx import WhisperXRuntime, WhisperXTranscriptionModel
from gateway.model_factory import GatewayClient
from gateway.transcription import build_transcription_model


@pytest.fixture
def local_asr(tmp_path, monkeypatch):
    model_dir = tmp_path / "asr"
    align_dir = tmp_path / "align-en"
    model_dir.mkdir()
    (model_dir / "tokenizer.json").write_text("{}")
    align_dir.mkdir()
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    source = WhisperXSourceConfig(alignment_models={"en": str(align_dir)})
    binding = ModelBinding(source="local_speech", model=str(model_dir),
                           kind="transcription", request_timeout_seconds=30)
    calls = []
    raw = {"language": "en", "segments": [{"start": 0, "end": 1, "text": "Hello 2026"}]}
    aligned = {"segments": [{"start": 0.1, "end": 0.8, "text": "Hello 2026", "words": [
        {"word": "Hello", "start": 0.1, "end": 0.6, "score": 0.9},
        {"word": "2026", "start": 0.1, "end": 0.6},
    ]}]}

    def transcribe(samples, **kwargs):
        assert samples.shape == (16000,)
        assert str(samples.dtype) == "float32"
        calls.append(("transcribe", kwargs))
        for percent in (1, 2, 6, 100):
            kwargs["progress_callback"](percent)
        return raw

    def load_model(*args, **kwargs):
        calls.append(("load", args, kwargs))
        return SimpleNamespace(transcribe=transcribe)

    def load_align_model(**kwargs):
        calls.append(("load_align", kwargs))
        return "aligner", {"language": "en"}

    def align(segments, model, metadata, samples, device, **kwargs):
        assert segments is raw["segments"]
        assert (model, metadata, device) == ("aligner", {"language": "en"}, "cpu")
        calls.append(("align", kwargs))
        for percent in (10, 50, 100):
            kwargs["progress_callback"](percent)
        return aligned

    module = SimpleNamespace(load_model=load_model, load_align_model=load_align_model, align=align)
    nltk = SimpleNamespace(data=SimpleNamespace(find=lambda path: "installed", load=lambda path: "installed"))
    # NumPy is optional too: the ordinary test environment has no ML extras.
    class Samples:
        shape = (16000,)
        dtype = "float32"

        def astype(self, dtype):
            assert dtype == "float32"
            return self

        def __truediv__(self, divisor):
            assert divisor == 32768
            return self

    def frombuffer(pcm, dtype):
        assert pcm == b"\x00\x00" * 16000 and dtype == "<i2"
        return Samples()

    numpy = SimpleNamespace(frombuffer=frombuffer, float32="float32")
    from gateway.local import whisperx as adapter
    original_import = adapter.importlib.import_module

    def import_module(name):
        if name == "whisperx":
            return module
        if name == "nltk":
            return nltk
        if name == "whisperx.utils":
            return SimpleNamespace(PUNKT_LANGUAGES={"en": "english"})
        if name == "numpy":
            return numpy
        return original_import(name)

    monkeypatch.setattr(adapter.importlib, "import_module", import_module)
    return SimpleNamespace(source=source, binding=binding, audio=audio, raw=raw,
                           aligned=aligned, calls=calls, module=module, nltk=nltk)


def test_gateway_reuses_local_model_and_alignment_across_chunk_handles(local_asr):
    env = local_asr
    config = load_config(Path(__file__).parent / "fixtures" / "legacy-config.yaml")
    config.model_sources["local_speech"] = env.source
    client = GatewayClient(config, environ={})
    first = client.create_transcription_model(env.binding)
    assert first.input_mode == "audio"
    assert not env.calls  # Construction is lazy and needs no API credentials.
    result = first.transcribe(env.audio)
    first.close()
    with pytest.raises(RuntimeError, match="closed"):
        first.transcribe(env.audio)
    second = client.create("transcription", env.binding)
    assert second.transcribe(env.audio) == result
    second.close()
    runtime = next(iter(client._whisperx_runtimes.values()))
    client.close()
    client.close()
    assert runtime._model is None and not runtime._aligners
    assert [call[0] for call in env.calls] == [
        "load", "transcribe", "load_align", "align", "transcribe", "align",
    ]
    assert env.calls[0] == ("load", (env.binding.model, "cpu"), {
        "compute_type": "int8", "language": None, "local_files_only": True,
        "threads": 6, "vad_method": "pyannote",
    })
    assert env.calls[2] == ("load_align", {
        "language_code": "en", "device": "cpu",
        "model_name": env.source.alignment_models["en"], "model_cache_only": True,
    })
    assert callable(env.calls[3][1]["progress_callback"])
    assert {key: value for key, value in env.calls[3][1].items() if key != "progress_callback"} == {
        "interpolate_method": "nearest", "return_char_alignments": False,
    }
    words = result["segments"][0]["words"]
    assert words[0] == env.aligned["segments"][0]["words"][0]
    assert words[1] == {"word": "2026"}
    assert "start" in env.aligned["segments"][0]["words"][1]  # No upstream mutation.


def test_no_speech_does_not_require_alignment(local_asr):
    local_asr.raw["segments"] = []
    model = build_transcription_model(local_asr.binding, local_asr.source, api_key=None)
    try:
        assert model.transcribe(local_asr.audio) == {"segments": [], "language": "en"}
        assert [call[0] for call in local_asr.calls] == ["load", "transcribe"]
    finally:
        model.close()


@pytest.mark.parametrize("failure", ["language", "weights", "alignment"])
def test_alignment_failures_do_not_silently_return_unaligned_success(local_asr, failure):
    if failure == "language":
        local_asr.raw["language"] = "ja"
        expected, message = ValueError, "no configured local alignment model"
    elif failure == "weights":
        Path(local_asr.source.alignment_models["en"]).rmdir()
        expected, message = FileNotFoundError, "alignment model directory is missing"
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("alignment failed")
        local_asr.module.align = fail
        expected, message = RuntimeError, "alignment failed"
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(expected, match=message):
            model.transcribe(local_asr.audio)
    finally:
        model.close()


def test_missing_optional_dependency_reports_install_command(local_asr, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr("gateway.local.whisperx.importlib.import_module", missing)
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(RuntimeError, match="uv sync --extra whisperx"):
            model.transcribe(local_asr.audio)
    finally:
        model.close()


def test_missing_nltk_data_is_not_automatically_downloaded(local_asr):
    def missing(path):
        raise LookupError(path)
    local_asr.nltk.data.find = missing
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(RuntimeError, match="nltk.downloader punkt_tab"):
            model.transcribe(local_asr.audio)
        assert local_asr.calls == []
    finally:
        model.close()


def test_only_local_asr_directories_are_accepted(local_asr):
    binding = local_asr.binding.model_copy(update={"model": "remote/model-id"})
    model = WhisperXTranscriptionModel(binding, local_asr.source)
    try:
        with pytest.raises(FileNotFoundError, match="local CTranslate2 model directory"):
            model.transcribe(local_asr.audio)
        assert local_asr.calls == []
    finally:
        model.close()


def test_missing_tokenizer_does_not_allow_faster_whisper_download(local_asr):
    (Path(local_asr.binding.model) / "tokenizer.json").unlink()
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(FileNotFoundError, match="requires tokenizer.json"):
            model.transcribe(local_asr.audio)
        assert not local_asr.calls
    finally:
        model.close()


def test_partial_nltk_data_fails_before_upstream_alignment(local_asr):
    def missing(path):
        assert path == "tokenizers/punkt_tab/english.pickle"
        raise FileNotFoundError(path)
    local_asr.nltk.data.load = missing
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(RuntimeError, match="nltk.downloader punkt_tab"):
            model.transcribe(local_asr.audio)
        assert [call[0] for call in local_asr.calls] == ["load", "transcribe"]
    finally:
        model.close()


def test_invalid_media_is_rejected_before_loading_models(local_asr):
    with wave.open(str(local_asr.audio), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00" * 100)
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(ValueError, match="16 kHz mono PCM16"):
            model.transcribe(local_asr.audio)
        assert local_asr.calls == []
    finally:
        model.close()


def test_timeout_rejects_late_native_result_before_alignment(local_asr, monkeypatch):
    now = [0]
    monkeypatch.setattr("gateway.local.whisperx.monotonic", lambda: now[0])
    def load(*args, **kwargs):
        def transcribe(*args, **kwargs):
            now[0] = 31
            return local_asr.raw
        return SimpleNamespace(transcribe=transcribe)
    local_asr.module.load_model = load
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            model.transcribe(local_asr.audio)
        assert local_asr.calls == []
    finally:
        model.close()


def test_timeout_applies_while_waiting_for_shared_runtime(local_asr, caplog):
    caplog.set_level("INFO", logger="gateway.local.whisperx")
    binding = local_asr.binding.model_copy(update={"request_timeout_seconds": 0.01})
    runtime = WhisperXRuntime(binding.model, local_asr.source)
    model = WhisperXTranscriptionModel(binding, local_asr.source, runtime=runtime)
    runtime._lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            model.transcribe(local_asr.audio)
        assert local_asr.calls == []
    finally:
        runtime._lock.release()
        model.close()
        runtime.close()
    records = _progress_records(caplog)
    assert records[-1]["phase"] == "waiting_for_runtime"
    assert records[-1]["status"] == "failed"
    assert records[-1]["error_type"] == "TimeoutError"
    assert records[-1]["phase_percent"] is None


@pytest.mark.parametrize("fields", [
    {"alignment_models": {}}, {"language": "ja"}, {"device": "mps"},
    {"compute_type": "float16"}, {"batch_size": 0}, {"cpu_threads": True},
])
def test_invalid_local_source_settings_fail_early(fields):
    with pytest.raises(ValueError):
        WhisperXSourceConfig.model_validate({"alignment_models": {"en": "/models/en"}, **fields})


def test_yaml_selects_whisperx_through_formal_model_binding_without_ml_install(tmp_path):
    payload = yaml.safe_load(Path("config.yaml").read_text())
    payload["models"]["transcription"] = {
        "kind": "transcription", "source": "whisperx_local",
        "model": "/not-installed/asr", "request_timeout_seconds": 600,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload))
    config = load_config(path)
    assert isinstance(config.source("whisperx_local"), WhisperXSourceConfig)
    client = GatewayClient(config, environ={})
    try:
        model = client.create_transcription_model()
        assert model.model_name == "/not-installed/asr" and model.input_mode == "audio"
        model.close()
        with pytest.raises(ValueError, match="does not support adapter"):
            client.create_generative_model(ModelBinding(
                source="whisperx_local", model="/not-installed/asr", request_timeout_seconds=30,
            ))
    finally:
        client.close()


def _progress_records(caplog):
    import json
    return [json.loads(record.getMessage().split("summary=", 1)[1])
            for record in caplog.records if "event=local_asr_progress summary=" in record.getMessage()]


def test_progress_correlates_calls_and_distinguishes_transcription_from_alignment(local_asr, caplog):
    from gateway.usage import capture_model_usage, model_call_context
    caplog.set_level("INFO", logger="gateway.local.whisperx")
    calls = []
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    with capture_model_usage(calls.append), model_call_context({"run_id": "course-run", "stage": "asr"}):
        result = model.transcribe(local_asr.audio)
    model.close()
    records = _progress_records(caplog)
    assert result["segments"][0]["words"][1] == {"word": "2026"}
    assert {row["call_id"] for row in records} == {calls[0].call_id}
    assert all(row["context"] == {"run_id": "course-run", "stage": "asr"} for row in records)
    assert [row["phase"] for row in records if row["status"] == "phase_started"] == [
        "read_audio", "load_model", "transcribe", "load_aligner", "align", "normalize",
    ]
    transcript = [row for row in records if row["phase"] == "transcribe" and row["status"] == "progress"]
    assert [row["phase_percent"] for row in transcript] == [1, 6, 100]
    assert all(row["progress_basis"] == "upstream_segments" for row in transcript)
    alignment_start = next(row for row in records if row["phase"] == "align" and row["status"] == "phase_started")
    assert alignment_start["phase_percent"] is None
    assert alignment_start["audio_duration_seconds"] == 1
    assert alignment_start["language"] == "en"
    assert records[-1]["status"] == "completed" and records[-1]["phase"] == "normalize"
    assert sum(row["status"] == "completed" for row in records) == 1
    assert "Hello 2026" not in caplog.text and str(local_asr.audio) not in caplog.text


def test_progress_records_alignment_failure_without_completed_call(local_asr, caplog):
    caplog.set_level("INFO", logger="gateway.local.whisperx")
    def fail(*args, **kwargs):
        kwargs["progress_callback"](25)
        raise RuntimeError("private transcript must not appear in progress logs")
    local_asr.module.align = fail
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)
    with pytest.raises(RuntimeError):
        model.transcribe(local_asr.audio)
    model.close()
    records = _progress_records(caplog)
    assert records[-1]["status"] == "failed" and records[-1]["phase"] == "align"
    assert records[-1]["phase_percent"] == 25
    assert records[-1]["error_type"] == "RuntimeError"
    assert not any(row["status"] == "completed" for row in records)
    assert "private transcript" not in caplog.text


def test_progress_heartbeat_continues_without_upstream_callbacks_and_stops(local_asr, monkeypatch, caplog):
    import logging
    from threading import Event, Thread, enumerate as threads
    from gateway.local import whisperx as adapter
    caplog.set_level("INFO", logger="gateway.local.whisperx")
    monkeypatch.setattr(adapter, "_PROGRESS_INTERVAL_SECONDS", 0.01)
    seen, release = Event(), Event()
    errors = []

    class Observe(logging.Handler):
        def emit(self, record):
            import json
            row = json.loads(record.getMessage().split("summary=", 1)[1])
            if row["phase"] == "transcribe" and row["status"] == "heartbeat":
                assert row["phase_percent"] is None
                seen.set()

    def transcribe(*args, **kwargs):
        assert release.wait(2)
        return local_asr.raw

    local_asr.module.load_model = lambda *args, **kwargs: SimpleNamespace(transcribe=transcribe)
    observer = Observe()
    adapter._logger.addHandler(observer)
    model = WhisperXTranscriptionModel(local_asr.binding, local_asr.source)

    def run():
        try:
            model.transcribe(local_asr.audio)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert seen.wait(1)
    finally:
        release.set()
        worker.join(2)
        model.close()
        adapter._logger.removeHandler(observer)
    assert not worker.is_alive() and not errors
    call_id = _progress_records(caplog)[0]["call_id"]
    assert not any(t.name == f"whisperx-progress-{call_id[:8]}" for t in threads())
