from pathlib import Path

import pytest
import yaml

from engine.parsev1.audio_transcription import AudioTranscriptionAgent
from engine.parsev1.model_factory import build_fallback_model
from engine.parsev1.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from engine.parsev1.video_chunk_parse import OrderedFrameVideoUnderstandingAgent
from gateway.config import load_config
from gateway.openai.vision import OpenAIVisionModel


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
        "engine.parsev1.runtime.ArkMultimodalModel",
        lambda *a, **k: pytest.fail("unexpected Ark client"),
    )
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    asr = runtime._get_asr_agent()
    vision = runtime._get_understanding_agent()
    assert isinstance(asr, AudioTranscriptionAgent)
    assert asr.model_name == "speech-model" and asr._timeout == 45
    assert isinstance(vision, OrderedFrameVideoUnderstandingAgent)
    assert isinstance(vision._model, OpenAIVisionModel)
    assert vision._model.provider_name == "vision_vendor"
    assert vision._model._timeout == 90


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
    assert isinstance(model, OpenAIVisionModel)
    assert model.model_name == "fallback-model"
    assert model._format == "json_object"
    assert model._token_parameter == "max_completion_tokens"


def test_missing_selected_asr_key_names_the_correct_variable(
    third_party_config, monkeypatch
):
    monkeypatch.delenv("SPEECH_KEY", raising=False)
    runtime = EngineVideoParseRuntime(model_config_path=third_party_config)
    with pytest.raises(RuntimeError, match="SPEECH_KEY"):
        runtime._get_asr_agent()
