"""Compose parsing adapters from validated model sources."""

from __future__ import annotations

import os

from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import OpenAIVisionSourceConfig
from gateway.model_sources import ark_options
from gateway.openai.multimodal import DeepSeekMultimodalModel

from engine.common.model_factory import build_openai_vision


def build_vision_model(
    config, *, model, timeout, max_output_tokens=None, ark_model_type=ArkMultimodalModel
):
    source = config.stage_source("vision")
    if isinstance(source, OpenAIVisionSourceConfig):
        return build_openai_vision(
            source, model=model, timeout=timeout, max_output_tokens=max_output_tokens
        )
    return ark_model_type(
        model,
        request_timeout_seconds=timeout,
        max_output_tokens=max_output_tokens,
        **ark_options(source),
    )


def build_fallback_model(
    config, fallback, *, deepseek_model_type=DeepSeekMultimodalModel
):
    if fallback.protocol == "openai_chat":
        return build_openai_vision(
            config.model_sources[fallback.vision_source],
            model=fallback.model,
            timeout=fallback.request_timeout_seconds,
            max_output_tokens=fallback.max_output_tokens,
        )
    key = os.getenv(fallback.api_key_env, "").strip()
    if not key:
        raise RuntimeError(
            f"missing DeepSeek fallback credential in environment variable {fallback.api_key_env!r}"
        )
    return deepseek_model_type(
        fallback.model,
        base_url=fallback.base_url,
        api_key=key,
        max_output_tokens=fallback.max_output_tokens,
        request_timeout_seconds=fallback.request_timeout_seconds,
        reasoning_effort=fallback.reasoning_effort,
    )
