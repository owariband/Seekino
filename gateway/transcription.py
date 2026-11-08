"""Configured transcription transports; media preparation belongs to the caller."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import ArkModelSourceConfig, ChatAPISourceConfig, ModelBinding, OpenAIAudioSourceConfig, WhisperXSourceConfig
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

    def __init__(self, binding, source, *, api_key: str | None, allow_local: bool | None = None):
        if binding.request_timeout_seconds is None:
            raise ValueError("audio transcription requires a configured request timeout")
        self.provider_name = source.provider
        self.model_name = binding.model
        self._binding = binding
        self._source = source
        self._api_key = api_key
        self._allow_local = allow_local
        self._closed = False

    def transcribe(self, audio_path: str | Path) -> dict:
        if self._closed:
            raise RuntimeError("Transcription model is closed")
        # The transport owns and closes one SDK client per call.
        return transcribe_audio(
            Path(audio_path), model=self.model_name, provider=self.provider_name,
            base_url=self._source.base_url, api_key=self._api_key,
            timeout=self._binding.request_timeout_seconds,
            response_format=getattr(self._source, "response_format", "verbose_json"),
            **({"allow_local": self._allow_local} if self._allow_local is not None else {}),
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
            stream=binding.stream,
            temperature=binding.temperature,
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
    source: ArkModelSourceConfig | ChatAPISourceConfig | OpenAIAudioSourceConfig | WhisperXSourceConfig,
    *,
    api_key: str | None,
    local_runtime=None,
    allow_local: bool | None = None,
) -> AudioTranscriptionModel | VideoTranscriptionModel:
    """Bind the provider transport without introducing ASR prompt or timing rules."""
    if isinstance(source, WhisperXSourceConfig):
        from gateway.local.whisperx import WhisperXTranscriptionModel
        return WhisperXTranscriptionModel(binding, source, runtime=local_runtime)
    if isinstance(source, OpenAIAudioSourceConfig) or (isinstance(source, ChatAPISourceConfig) and source.protocol == "openai_chat"):
        return _AudioTranscriptionModel(binding, source, api_key=api_key, allow_local=allow_local)
    if isinstance(source, ArkModelSourceConfig):
        return _VideoTranscriptionModel(binding, source, api_key=api_key)
    raise TypeError(f"unsupported transcription adapter {source.adapter!r}")
