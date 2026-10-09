"""Compose shared parsing adapters from validated model sources."""

from __future__ import annotations

from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import GatewayConfig, OpenAIAudioSourceConfig
from gateway.model_sources import ark_options, source_key
from gateway.openai.vision import OpenAIVisionModel

from .transcription import AudioTranscriptionAgent, VideoASRAgent


def build_openai_vision(source, *, model, timeout, max_output_tokens=None):
    return OpenAIVisionModel(
        model,
        provider=source.provider,
        base_url=source.base_url,
        api_key=source_key(source),
        request_timeout_seconds=timeout,
        max_output_tokens=max_output_tokens or 8192,
        token_parameter=source.token_parameter,
        response_format=source.response_format,
    )


def build_asr_agent(
    config: GatewayConfig,
    *,
    model,
    fps,
    timeout,
    max_output_tokens=None,
    ark_model_type=ArkMultimodalModel,
):
    source = config.stage_source("transcription")
    if isinstance(source, OpenAIAudioSourceConfig):
        return AudioTranscriptionAgent(
            model,
            provider=source.provider,
            base_url=source.base_url,
            api_key=source_key(source),
            request_timeout_seconds=timeout,
            response_format=source.response_format,
        )
    return VideoASRAgent(
        ark_model_type(
            model,
            request_timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            **ark_options(source),
        ),
        fps=fps,
    )
