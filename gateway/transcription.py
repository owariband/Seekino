"""Configured transcription transports; media preparation belongs to the caller."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import ArkModelSourceConfig, ModelBinding, OpenAIAudioSourceConfig
from gateway.multimodal import MultimodalInput
from gateway.openai.audio import transcribe_audio

_Result = TypeVar("_Result", bound=BaseModel)


class AudioTranscriptionModel(Protocol):
    provider_name: str
    model_name: str
    input_mode: Literal["audio"]

    def transcribe(self, audio_path: str | Path) -> dict: ...

    def close(self) -> None: ...


class VideoTranscriptionModel(Protocol):
    provider_name: str
    model_name: str
    input_mode: Literal["video"]

    def generate(
        self, inputs: Sequence[MultimodalInput], *,
        schema_name: str, result_type: type[_Result],
    ) -> _Result: ...

    def close(self) -> None: ...


class _AudioTranscriptionModel:
    input_mode = "audio"

    def __init__(self, binding, source, *, api_key: str):
        if binding.request_timeout_seconds is None:
            raise ValueError("audio transcription requires a configured request timeout")
        self.provider_name = source.provider
        self.model_name = binding.model
        self._binding = binding
        self._source = source
        self._api_key = api_key
        self._closed = False

    def transcribe(self, audio_path: str | Path) -> dict:
        if self._closed:
            raise RuntimeError("Transcription model is closed")
        # The transport owns and closes one SDK client per call.
        return transcribe_audio(
            Path(audio_path), model=self.model_name, provider=self.provider_name,
            base_url=self._source.base_url, api_key=self._api_key,
            timeout=self._binding.request_timeout_seconds,
            response_format=self._source.response_format,
        )

    def close(self) -> None:
        self._closed = True


class _VideoTranscriptionModel:
    input_mode = "video"

    def __init__(self, binding, source, *, api_key: str):
        self.provider_name = source.provider
        self.model_name = binding.model
        self._model = ArkMultimodalModel(
            binding.model,
            request_timeout_seconds=binding.request_timeout_seconds,
            max_output_tokens=binding.max_output_tokens,
            reasoning_effort=binding.reasoning_effort,
            api_key=api_key,
        )
        self._closed = False

    def generate(
        self, inputs: Sequence[MultimodalInput], *,
        schema_name: str, result_type: type[_Result],
    ) -> _Result:
        if self._closed:
            raise RuntimeError("Transcription model is closed")
        # Ark's adapter owns and closes one SDK client per call.
        return self._model.generate(
            inputs, schema_name=schema_name, result_type=result_type,
        )

    def close(self) -> None:
        self._closed = True


def build_transcription_model(
    binding: ModelBinding,
    source: ArkModelSourceConfig | OpenAIAudioSourceConfig,
    *,
    api_key: str,
) -> AudioTranscriptionModel | VideoTranscriptionModel:
    """Bind the provider transport without introducing ASR prompt or timing rules."""
    if isinstance(source, OpenAIAudioSourceConfig):
        return _AudioTranscriptionModel(binding, source, api_key=api_key)
    if isinstance(source, ArkModelSourceConfig):
        return _VideoTranscriptionModel(binding, source, api_key=api_key)
    raise TypeError(f"unsupported transcription adapter {source.adapter!r}")
