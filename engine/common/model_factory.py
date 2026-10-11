"""Compose shared parsing adapters from validated model sources."""

from __future__ import annotations

from gateway.config import GatewayConfig
from gateway.model_factory import GatewayClient

from .transcription import AudioTranscriptionAgent, VideoASRAgent


def build_asr_agent(
    config: GatewayConfig,
    *,
    model,
    fps,
    timeout,
    max_output_tokens=None,
):
    binding = config.resolve_model_binding("transcription")
    updates = {"model": model, "request_timeout_seconds": timeout}
    if max_output_tokens is not None:
        updates["max_output_tokens"] = max_output_tokens
    binding = config.resolve_model_binding(binding.model_copy(update=updates))
    client = GatewayClient(config)
    try:
        delegate = client.create_transcription_model(binding)
        if delegate.input_mode == "audio":
            return AudioTranscriptionAgent(delegate, request_timeout_seconds=binding.request_timeout_seconds)
        if delegate.input_mode == "video":
            return VideoASRAgent(delegate, fps=fps)
        raise ValueError(f"unsupported transcription input mode {delegate.input_mode!r}")
    except BaseException:
        client.close()
        raise
