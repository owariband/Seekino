from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from gateway.config import (
    ArkModelSourceConfig,
    ModelBinding,
    OpenAIAudioSourceConfig,
    OpenAIModelSourceConfig,
)
from gateway.multimodal import TextInput, VideoInput
from gateway.transcription import build_transcription_model


def test_audio_transport_uses_explicit_binding_and_preserves_provider_seconds(monkeypatch, tmp_path):
    calls = []
    payload = {"segments": [{"start": 1.2346, "end": 2.8, "text": " spoken "}]}
    monkeypatch.setattr(
        "gateway.transcription.transcribe_audio",
        lambda path, **kwargs: calls.append((path, kwargs)) or payload,
    )
    binding = ModelBinding(
        source="speech_connection", model="selected-speech", request_timeout_seconds=17,
    )
    source = OpenAIAudioSourceConfig(
        provider="speech-provider", base_url="https://speech.example/v1",
        api_key_env="EXPLICIT_KEY", response_format="json",
    )
    model = build_transcription_model(binding, source, api_key="selected-key")
    audio = tmp_path / "audio.wav"
    assert model.input_mode == "audio"
    assert model.transcribe(audio) is payload
    assert calls == [(audio, {
        "model": "selected-speech", "provider": "speech-provider",
        "base_url": "https://speech.example/v1", "api_key": "selected-key",
        "timeout": 17, "response_format": "json",
    })]
    model.close()
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.transcribe(audio)
    assert len(calls) == 1


def test_video_transport_retains_caller_prompt_schema_and_selected_model(monkeypatch, tmp_path):
    constructed = []
    requests = []

    class Transcript(BaseModel):
        text: str

    result = Transcript(text="heard speech")

    def create(model, **options):
        constructed.append((model, options))
        return SimpleNamespace(generate=lambda inputs, **kwargs: requests.append((inputs, kwargs)) or result)

    monkeypatch.setattr("gateway.transcription.ArkMultimodalModel", create)
    binding = ModelBinding(
        source="video_connection", model="selected-video", request_timeout_seconds=23,
        max_output_tokens=1024, reasoning_effort="none",
    )
    model = build_transcription_model(binding, ArkModelSourceConfig(), api_key="explicit-key")
    inputs = [VideoInput(tmp_path / "video.mp4", fps=0.5), TextInput("caller ASR prompt")]
    assert model.input_mode == "video"
    assert model.generate(inputs, schema_name="candidate_transcript", result_type=Transcript) is result
    assert constructed == [("selected-video", {
        "request_timeout_seconds": 23, "max_output_tokens": 1024,
        "reasoning_effort": "none", "api_key": "explicit-key",
    })]
    assert requests == [(inputs, {"schema_name": "candidate_transcript", "result_type": Transcript})]
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        model.generate(inputs, schema_name="candidate_transcript", result_type=Transcript)


def test_audio_transport_requires_configured_timeout():
    binding = ModelBinding(source="speech", model="selected-speech")
    source = OpenAIAudioSourceConfig(
        provider="speech", base_url="https://speech.example/v1", api_key_env="KEY",
    )
    with pytest.raises(ValueError, match="configured request timeout"):
        build_transcription_model(binding, source, api_key="key")


def test_transcription_rejects_unsupported_adapter_without_provider_fallback():
    source = OpenAIModelSourceConfig(
        provider="chat", base_url="https://chat.example/v1", api_key_env="KEY",
    )
    with pytest.raises(TypeError, match="unsupported transcription adapter 'openai'"):
        build_transcription_model(ModelBinding(source="chat", model="chat-model"), source, api_key="key")
