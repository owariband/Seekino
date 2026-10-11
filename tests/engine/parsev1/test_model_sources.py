from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from engine.common.config import VideoParseRuntimeConfig
from engine.common.transcription import AudioTranscriptionAgent
from engine.parsev1.model_factory import build_fallback_model
from engine.parsev1.runtime import EngineVideoParseRuntime
from engine.parsev1.video_chunk_parse import OrderedFrameVideoUnderstandingAgent
from gateway.config import load_config


@pytest.fixture
def third_party_config(tmp_path):
    data = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "config.yaml").read_text()
    )
    data["model_sources"]["asr"] = {
        "adapter": "openai_audio",
        "provider": "speech",
        "base_url": "https://speech.example/v1",
        "api_key_env": "SPEECH_KEY",
    }
    data["model_sources"]["vision"] = {
        "adapter": "openai_vision",
        "provider": "vision_vendor",
        "base_url": "https://vision.example/v1",
        "api_key_env": "VISION_KEY",
        "response_format": "json_object",
        "token_parameter": "max_completion_tokens",
    }
    data["models"]["transcription"] = {
        "source": "asr",
        "model": "speech-model",
        "request_timeout_seconds": 45,
    }
    data["models"]["vision"] = {
        "source": "vision",
        "model": "vision-model",
        "request_timeout_seconds": 90,
    }
    data["models"]["vision_fallback"] = {"source": "vision", "model": "fallback-model"}
    data["video_parser"]["fallback"]["enabled"] = False
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_runtime_routes_asr_and_vision_without_creating_ark_clients(
    third_party_config, monkeypatch
):
    monkeypatch.setenv("SPEECH_KEY", "fake-speech-key")
    monkeypatch.setenv("VISION_KEY", "fake-vision-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(
        "gateway.ark.multimodal.ArkMultimodalModel",
        lambda *a, **k: pytest.fail("unexpected Ark client"),
    )
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    asr = runtime._get_asr_agent()
    vision = runtime._get_understanding_agent()
    assert isinstance(asr, AudioTranscriptionAgent)
    assert asr.model_name == "speech-model" and asr._timeout == 45
    assert isinstance(vision, OrderedFrameVideoUnderstandingAgent)
    assert callable(vision._model.generate)
    assert vision._model.provider_name == "vision_vendor"
    assert vision._model.binding.request_timeout_seconds == 90
    assert vision._model.visual_input_mode == "ordered_frames"
    runtime.close()
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime._get_asr_agent()


def test_runtime_preserves_environment_and_explicit_model_overrides(
    third_party_config, monkeypatch
):
    monkeypatch.setenv("ANIVAG_TRANSCRIPT_MODEL", "env-speech")
    monkeypatch.setenv("ANIVAG_VIDEO_MODEL", "env-vision")
    runtime = EngineVideoParseRuntime(
        model_config_path=third_party_config,
        config=VideoParseRuntimeConfig(video_model="explicit-vision"),
    )
    models = runtime.describe_models(asr_enabled=True)
    assert models.transcript == "env-speech"
    assert models.video_parser == "explicit-vision"


def test_third_party_fallback_keeps_source_dialect(third_party_config, monkeypatch):
    monkeypatch.setenv("VISION_KEY", "fake-vision-key")
    config = load_config(third_party_config)
    model = build_fallback_model(config, config.video_parser.fallback.deepseek)
    assert callable(model.generate)
    assert model.model_name == "fallback-model"
    assert model.provider_name == "vision_vendor"
    assert model.binding.source == "vision" and model.visual_input_mode == "ordered_frames"
    assert config.source(model.binding.source).response_format == "json_object"
    assert config.source(model.binding.source).token_parameter == "max_completion_tokens"
    model.close()


def test_missing_selected_asr_key_names_the_correct_variable(
    third_party_config, monkeypatch
):
    monkeypatch.delenv("SPEECH_KEY", raising=False)
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    with pytest.raises(RuntimeError, match="SPEECH_KEY"):
        runtime._get_asr_agent()


def test_disabled_fallback_without_a_model_has_truthful_metadata(third_party_config):
    data = yaml.safe_load(third_party_config.read_text())
    del data["models"]["vision_fallback"]
    data["models"]["vision"]["fallback"] = None
    third_party_config.write_text(yaml.safe_dump(data))
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    metadata = runtime.describe_models(asr_enabled=False).fallback
    assert not metadata.enabled
    assert metadata.provider is None and metadata.visual_model is None and metadata.refiner_model is None
    runtime.close()


def test_explicit_null_fallback_is_not_reenabled_by_a_retained_default_reference(third_party_config):
    data = yaml.safe_load(third_party_config.read_text())
    data["video_parser"]["fallback"]["enabled"] = True
    data["models"]["vision"]["fallback"] = None
    third_party_config.write_text(yaml.safe_dump(data))
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    assert not runtime.describe_models(asr_enabled=False).fallback.enabled
    runtime.close()


def test_fallback_construction_failure_closes_the_primary_model(third_party_config, monkeypatch):
    closed = []
    primary = SimpleNamespace(
        model_name="primary", provider_name="vision_vendor", visual_input_mode="ordered_frames",
        close=lambda: closed.append("primary"),
    )
    monkeypatch.setattr("engine.parsev1.runtime.build_vision_model", lambda *args, **kwargs: primary)
    monkeypatch.delenv("VISION_KEY", raising=False)
    runtime = EngineVideoParseRuntime(
        model_config_path=third_party_config, config=VideoParseRuntimeConfig(fallback_enabled=True),
    )
    with pytest.raises(RuntimeError, match="VISION_KEY"):
        runtime._get_understanding_agent()
    assert closed == ["primary"]
    runtime.close()
    assert closed == ["primary"]
