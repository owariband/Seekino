from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gateway.usage import TokenUsage
from server.config.usage import ModelPricingConfig
from server.service.model_pricing import estimate_model_cost
from server.utils.time import datetime_to_unix_time_ms

_NOW = datetime_to_unix_time_ms(datetime(2026, 10, 8, tzinfo=UTC))


def pricing(*, category="parse", tiers=None, **changes):
    return ModelPricingConfig.model_validate({"models": [{
        "provider": "api", "model": "model", "category": category,
        "source_url": "https://example.test/prices", "checked_at": "2026-10-01T00:00:00Z",
        "effective_from": "2026-10-01T00:00:00Z",
        "tiers": tiers or [{"input_cny_per_unit": "1", "output_cny_per_unit": "3",
                            "cached_input_cny_per_unit": "0.2", "audio_input_cny_per_unit": "10",
                            "cached_audio_input_cny_per_unit": "0.5"}],
        **changes,
    }]})


def estimate(config, tokens, **changes):
    return estimate_model_cost(config, **{
        "provider": "api", "model": "model", "category": "parse", "operation": "generate",
        "started_at": _NOW, "usage": tokens, **changes,
    })


def test_decimal_audio_and_cache_subsets_are_charged_once():
    tokens = TokenUsage(1000, 100, 1100, 200, 40, audio_input_tokens=300,
                        cached_audio_input_tokens=50)
    assert estimate(pricing(), tokens) == Decimal("0.003405")


@pytest.mark.parametrize("audio,cached,expected", [
    (300, 0, "0.004"), (0, 200, "0.00114"),
])
def test_zero_bound_proves_cached_audio_is_zero(audio, cached, expected):
    tokens = TokenUsage(1000, 100, 1100, cached, None, audio_input_tokens=audio)
    assert estimate(pricing(), tokens) == Decimal(expected)


@pytest.mark.parametrize("tokens", [
    TokenUsage(1000, 100, 1100, None, None, audio_input_tokens=0),
    TokenUsage(1000, 100, 1100, 200, None, audio_input_tokens=300),
    TokenUsage(1000, 100, 1100, 0, None),
    TokenUsage(1000, 100, 1100, 0, None, audio_input_tokens=0, cache_write_input_tokens=10),
    TokenUsage(1000, 100, 1100, 0, None, audio_input_tokens=0, cache_write_5m_input_tokens=10),
    TokenUsage(1000, 100, 1100, 0, None, audio_input_tokens=0, cache_write_1h_input_tokens=10),
    TokenUsage(100, 0, 100, 90, None, audio_input_tokens=90, cached_audio_input_tokens=0),
])
def test_unknown_subsets_and_unsupported_cache_writes_are_unpriced(tokens):
    assert estimate(pricing(), tokens) is None


@pytest.mark.parametrize("missing", [
    "input_cny_per_unit", "output_cny_per_unit", "cached_input_cny_per_unit",
    "audio_input_cny_per_unit", "cached_audio_input_cny_per_unit",
])
def test_nonzero_billable_part_requires_its_own_rate(missing):
    config = pricing()
    setattr(config.models[0].tiers[0], missing, None)
    tokens = TokenUsage(1000, 100, 1100, 200, None, audio_input_tokens=300,
                        cached_audio_input_tokens=50)
    assert estimate(config, tokens) is None


def test_embedding_charges_actual_text_and_image_tokens():
    config = pricing(category="embedding", tiers=[{
        "text_input_cny_per_unit": "0.7", "image_input_cny_per_unit": "1.8",
    }])
    tokens = TokenUsage(1000, 0, 1000, None, None, text_input_tokens=100, image_input_tokens=900)
    assert estimate(config, tokens, category="embedding", operation="video_embedding") == Decimal("0.00169")


@pytest.mark.parametrize("tokens", [
    TokenUsage(1000, 0, 1000, None, None),
    TokenUsage(1000, 0, 1000, None, None, text_input_tokens=100, image_input_tokens=800),
    TokenUsage(1000, 0, 1000, 10, None, text_input_tokens=100, image_input_tokens=900),
    TokenUsage(1000, 1, 1001, None, None, text_input_tokens=100, image_input_tokens=900),
])
def test_embedding_missing_or_uncovered_tokens_are_unpriced(tokens):
    config = pricing(category="embedding", tiers=[{
        "text_input_cny_per_unit": "0.7", "image_input_cny_per_unit": "1.8",
    }])
    assert estimate(config, tokens, category="embedding") is None


def test_zero_modality_needs_no_rate_but_transcript_audio_cannot_be_assumed_zero():
    config = pricing(category="embedding", tiers=[{"text_input_cny_per_unit": "0.7"}])
    text = TokenUsage(100, 0, 100, None, None, text_input_tokens=100, image_input_tokens=0)
    assert estimate(config, text, category="embedding") == Decimal("0.00007")
    config = pricing(tiers=[{"input_cny_per_unit": "1", "output_cny_per_unit": "3"}])
    tokens = TokenUsage(100, 20, 120, 0, None)
    assert estimate(config, tokens) == Decimal("0.00016")
    assert estimate(config, tokens, operation="candidate_transcript") is None


def test_input_tier_is_selected_per_call_and_uses_exclusive_upper_bound():
    config = pricing(tiers=[
        {"min_input_tokens": 0, "max_input_tokens": 1000, "input_cny_per_unit": "1"},
        {"min_input_tokens": 1000, "input_cny_per_unit": "2"},
    ])
    small = TokenUsage(600, 0, 600, 0, None)
    assert estimate(config, small) + estimate(config, small) == Decimal("0.0012")
    assert estimate(config, TokenUsage(1000, 0, 1000, 0, None)) == Decimal("0.002")


@pytest.mark.parametrize("changes", [
    {"provider": "other"}, {"model": "other"}, {"category": "other"},
    {"started_at": _NOW - 30 * 86400000}, {"usage": None}, {"provider": "codex"},
])
def test_missing_price_usage_and_codex_are_not_fabricated(changes):
    assert estimate(pricing(), TokenUsage(100, 20, 120, 0, None), **changes) is None


def test_expired_price_is_unavailable_at_its_end_boundary():
    config = pricing(effective_until="2026-10-08T00:00:00Z")
    assert estimate(config, TokenUsage(100, 20, 120, 0, None)) is None
