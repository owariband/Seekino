from datetime import datetime
from decimal import Decimal

import pytest

from gateway.usage import TokenUsage
from server.config.usage import PriceTier, load_model_pricing
from server.service.parse_usage import estimate_cost_cny


def _estimate(model: str, usage: TokenUsage, *, category="parse", operation="video_timeline", instant=None):
    config = load_model_pricing()
    snapshot = config.snapshot(
        provider="deepseek" if model == "deepseek-flash" else "ark",
        model=model, category=category, input_tokens=usage.input_tokens,
        started_at=instant or datetime(2026, 9, 15, 11, 30),
    )
    if snapshot is None:
        return None
    return estimate_cost_cny(
        usage, PriceTier.model_validate(snapshot["tier"]),
        category=category, operation=operation, unit=snapshot["unit"],
    )


@pytest.mark.parametrize(("input_tokens", "amount"), [
    (32_000, "0.0192"), (32_001, "0.0288009"),
    (128_000, "0.1152"), (128_001, "0.2304018"),
    (256_000, "0.4608"), (256_001, None),
])
def test_default_lite_price_applies_inclusive_official_length_tiers(input_tokens, amount):
    usage = TokenUsage(input_tokens, 0, input_tokens, 0, 0, audio_input_tokens=0)
    cost = _estimate("doubao-seed-2-0-lite-260428", usage)
    assert cost == (Decimal(amount) if amount is not None else None)


def test_default_lite_audio_cache_and_output_are_charged_once():
    usage = TokenUsage(
        1_000, 100, 1_100, 200, 20,
        audio_input_tokens=400, cached_audio_input_tokens=80,
    )
    # 480 non-audio + 120 cached non-audio + 320 audio + 80 cached audio + 100 output.
    assert _estimate("doubao-seed-2-0-lite-260428", usage) == Decimal("0.0036864")
    # Pro uses one input rate; audio remains part of input and reasoning part of output.
    assert _estimate("doubao-seed-2-1-pro-260628", usage) == Decimal("0.00804")


@pytest.mark.parametrize(("operation", "text", "image", "amount"), [
    ("text_embedding", 100, 0, "0.00007"),
    ("video_embedding", 0, 100, "0.00018"),
    ("video_embedding", 70, 30, "0.000103"),
])
def test_default_embedding_prices_real_modality_counts(operation, text, image, amount):
    usage = TokenUsage(100, 0, 100, None, None, text_input_tokens=text, image_input_tokens=image)
    assert _estimate("doubao-embedding-vision-251215", usage, category="embedding", operation=operation) == Decimal(amount)


@pytest.mark.parametrize(("instant", "amount"), [
    (datetime(2026, 9, 15, 0, 59, 59), "0.002804"),
    (datetime(2026, 9, 15, 1, 0), "0.005608"),
    (datetime(2026, 9, 15, 4, 0), "0.002804"),
    (datetime(2026, 9, 15, 6, 0), "0.005608"),
    (datetime(2026, 9, 15, 10, 0), "0.002804"),
    (datetime(2026, 9, 19, 1, 0), "0.002804"),
])
def test_default_deepseek_uses_cny_and_beijing_weekday_peak_boundaries(instant, amount):
    usage = TokenUsage(1_000, 500, 1_500, 200, 100)
    assert _estimate("deepseek-flash", usage, instant=instant) == Decimal(amount)


def test_default_pricing_does_not_invent_historical_or_missing_modality_costs():
    usage = TokenUsage(100, 0, 100, 0, 0)
    assert _estimate("doubao-seed-2-0-lite-260428", usage) is None
    assert _estimate("doubao-embedding-vision-251215", usage, category="embedding", operation="video_embedding") is None
    assert _estimate("deepseek-flash", usage, instant=datetime(2026, 9, 10, 3, 59, 59)) is None
    assert _estimate("custom-model-with-no-rate", usage) is None
