from __future__ import annotations

from decimal import Decimal

from gateway.usage import TokenUsage

from ..config.usage import ModelPricingConfig


def estimate_model_cost(
    pricing: ModelPricingConfig, *, provider: str | None, model: str | None,
    category: str | None, operation: str | None, started_at: int,
    usage: TokenUsage | None,
) -> Decimal | None:
    """Estimate one recorded API call from its applicable configured price."""

    if provider == "codex" or usage is None:
        return None
    snapshot = pricing.snapshot(
        provider=provider, model=model, category=category,
        input_tokens=usage.input_tokens, started_at=started_at,
    )
    if snapshot is None or any((tokens or 0) > 0 for tokens in (
        usage.cache_write_input_tokens, usage.cache_write_5m_input_tokens,
        usage.cache_write_1h_input_tokens,
    )):
        return None
    tier = snapshot["tier"]
    if category == "embedding":
        text, image = usage.text_input_tokens, usage.image_input_tokens
        if (text is None or image is None or text + image != usage.input_tokens
                or usage.output_tokens or (usage.cached_input_tokens or 0) > 0):
            return None
        parts = ((text, "text_input_cny_per_unit"), (image, "image_input_cny_per_unit"))
    else:
        cached = usage.cached_input_tokens
        if cached is None:
            return None
        audio = usage.audio_input_tokens
        if audio is None:
            if (operation == "candidate_transcript" or tier.get("audio_input_cny_per_unit") is not None
                    or tier.get("cached_audio_input_cny_per_unit") is not None):
                return None
            # Without a modality-specific rate, the base input rate covers input.
            audio = 0
        cached_audio = usage.cached_audio_input_tokens
        if audio == 0 or cached == 0:
            cached_audio = 0
        if cached_audio is None or usage.input_tokens - audio - cached + cached_audio < 0:
            return None
        parts = (
            (usage.input_tokens - audio - cached + cached_audio, "input_cny_per_unit"),
            (cached - cached_audio, "cached_input_cny_per_unit"),
            (audio - cached_audio, "audio_input_cny_per_unit"),
            (cached_audio, "cached_audio_input_cny_per_unit"),
            (usage.output_tokens, "output_cny_per_unit"),
        )
    amount = Decimal(0)
    for tokens, rate_name in parts:
        rate = tier.get(rate_name)
        if tokens and rate is None:
            return None
        if tokens:
            amount += Decimal(tokens) * Decimal(rate)
    return amount / Decimal(snapshot["unit"])
