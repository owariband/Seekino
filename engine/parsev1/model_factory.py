"""Compose parsing adapters from validated model sources."""

from __future__ import annotations

from gateway.config import ModelBinding
from gateway.model_factory import GatewayClient


def build_vision_model(
    config, *, model, timeout, max_output_tokens=None
):
    binding = config.resolve_model_binding("vision")
    # Parse owns grouped-frame content-filter recovery; keep the transport single-model.
    updates = {"model": model, "request_timeout_seconds": timeout, "fallback": None}
    if max_output_tokens is not None:
        updates["max_output_tokens"] = max_output_tokens
    return _create_generative(config, binding.model_copy(update=updates))


def build_fallback_model(config, fallback=None):
    binding = fallback if isinstance(fallback, ModelBinding) else config.resolve_model_binding("vision_fallback")
    if fallback is not None:
        if not isinstance(fallback, ModelBinding):
            updates = {
                "model": fallback.model,
                "request_timeout_seconds": fallback.request_timeout_seconds,
                "max_output_tokens": fallback.max_output_tokens,
                "reasoning_effort": fallback.reasoning_effort,
            }
            if fallback.vision_source is not None:
                updates["source"] = fallback.vision_source
            binding = binding.model_copy(update=updates)
    return _create_generative(config, binding)


def _create_generative(config, binding):
    client = GatewayClient(config)
    try:
        return client.create_generative_model(binding)
    except BaseException:
        client.close()
        raise
