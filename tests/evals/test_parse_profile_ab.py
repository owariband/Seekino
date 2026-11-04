from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from evals.parse_profile_ab import (
    COURSE_EVENT_SENTENCE,
    EVENT_TYPES,
    FULL_EVENT_SENTENCE,
    build_variant,
    dialogue_coverage,
    estimate_cost_cny,
    summarize,
    variant_diff_paths,
)

PROMPTS_PATH = Path(__file__).resolve().parents[2] / "agent" / "video_parse_v2" / "prompts.py"


def _event_branch(event_type: str) -> dict:
    properties = {
        "event_type": {"type": "string", "const": event_type},
        "start_seconds": {"type": "integer"},
        "end_seconds": {"type": "integer"},
        "description": {"type": "string"},
        "participants": {"type": "array", "items": {"type": "string"}},
        "visible_persons": {"type": "array", "items": {"type": "string"}},
    }
    if event_type == "DIALOGUE":
        properties["speaker"] = {"type": ["string", "null"]}
        properties["text_variants"] = {
            "type": "array", "items": {"$ref": "#/$defs/DialogueTextVariant"},
        }
    elif event_type == "ON_SCREEN_TEXT":
        properties["language"] = {"type": ["string", "null"]}
        properties["text"] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _request(prompt: str | None = None) -> dict:
    prompt_text = "\n".join((
        "You are the understanding model for one fixed video chunk.",
        FULL_EVENT_SENTENCE,
        "OCR evidence has {\"lines\":[]}.",
    )) if prompt is None else prompt
    schema = {
        "$defs": {
            "DialogueTextVariant": {
                "type": "object",
                "properties": {
                    "language": {"type": "string"},
                    "role": {"type": "string", "enum": ["ORIGINAL", "TRANSLATION"]},
                    "text": {"type": "string"},
                },
                "required": ["language", "role", "text"],
                "additionalProperties": False,
            },
        },
        "type": "object",
        "properties": {
            "persons": {"type": "array", "items": {"type": "string"}},
            "timeline": {
                "type": "array",
                "items": {"anyOf": [_event_branch(event_type) for event_type in EVENT_TYPES]},
            },
        },
        "required": ["persons", "timeline"],
        "additionalProperties": False,
    }
    return {
        "model": "deepseek-flash",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "timestamp_ms=0"},
            {"type": "input_image", "image_url": "$diagnostic_media/frame-000.jpg", "detail": "high"},
            {"type": "input_text", "text": "timestamp_ms=1000"},
            {"type": "input_image", "image_url": "$diagnostic_media/frame-001.jpg", "detail": "high"},
            {"type": "input_text", "text": prompt_text},
        ]}],
        "text": {"format": {
            "type": "json_schema", "name": "video_parse_v2_chunk", "schema": schema, "strict": True,
        }},
        "reasoning": {"effort": "medium"},
        "temperature": 0.0,
        "max_output_tokens": 65536,
        "stream": True,
    }


def _timeline_branches(request: dict) -> list[dict]:
    return request["text"]["format"]["schema"]["properties"]["timeline"]["items"]["anyOf"]


def _event_types(request: dict) -> list[str]:
    return [branch["properties"]["event_type"]["const"] for branch in _timeline_branches(request)]


def test_full_event_sentence_still_matches_understanding_prompt():
    source = PROMPTS_PATH.read_text(encoding="utf-8")
    assert FULL_EVENT_SENTENCE in source


def test_course_variant_changes_only_reasoning_union_and_prompt():
    request = _request()
    untouched = deepcopy(request)
    original_branches = _timeline_branches(request)

    variant = build_variant(request, event_profile="course", reasoning_effort="none")

    assert request == untouched
    assert variant["reasoning"] == {"effort": "none"}
    assert _event_types(variant) == ["SCENE", "DIALOGUE", "ON_SCREEN_TEXT"]
    assert _timeline_branches(variant) == [
        original_branches[0], original_branches[2], original_branches[5],
    ]
    assert (variant["text"]["format"]["schema"]["$defs"]
            == request["text"]["format"]["schema"]["$defs"])
    assert variant_diff_paths(request, variant) == [
        "input[0].content[4].text",
        "text.format.schema.properties.timeline.items.anyOf",
        "reasoning.effort",
    ]

    prompt = variant["input"][0]["content"][4]["text"]
    assert prompt.count(COURSE_EVENT_SENTENCE) == 1
    assert FULL_EVENT_SENTENCE not in prompt
    for index, block in enumerate(variant["input"][0]["content"]):
        if index != 4:
            assert block == request["input"][0]["content"][index]


def test_full_variant_changes_only_reasoning_effort():
    request = _request()

    variant = build_variant(request, event_profile="full", reasoning_effort="low")

    assert variant_diff_paths(request, variant) == ["reasoning.effort"]
    assert variant["reasoning"] == {"effort": "low"}
    assert variant["input"] == request["input"]
    assert variant["text"] == request["text"]
    assert _event_types(variant) == list(EVENT_TYPES)


def test_course_variant_requires_the_full_event_sentence_exactly_once():
    missing = _request(prompt="You are the understanding model.\nNo Event guidance here.")
    with pytest.raises(ValueError, match="exactly once"):
        build_variant(missing, event_profile="course", reasoning_effort="none")

    duplicated = _request(prompt=f"{FULL_EVENT_SENTENCE}\n{FULL_EVENT_SENTENCE}")
    with pytest.raises(ValueError, match="exactly once"):
        build_variant(duplicated, event_profile="course", reasoning_effort="none")


@pytest.mark.parametrize("mutate", [
    lambda branches: branches.pop(),
    lambda branches: branches.__setitem__(6, _event_branch("SCENE")),
    lambda branches: branches.__setitem__(6, _event_branch("SUMMARY")),
])
def test_variant_rejects_unexpected_timeline_union(mutate):
    request = _request()
    mutate(_timeline_branches(request))
    with pytest.raises(ValueError, match="timeline union"):
        build_variant(request, event_profile="course", reasoning_effort="none")


def test_variant_rejects_missing_timeline_union():
    request = _request()
    del request["text"]["format"]["schema"]["properties"]["timeline"]["items"]["anyOf"]
    with pytest.raises(ValueError, match="anyOf"):
        build_variant(request, event_profile="course", reasoning_effort="none")


def test_variant_diff_paths_reports_nested_leaf_and_array_length_changes():
    assert variant_diff_paths(
        {"a": {"b": [1, 2]}, "keep": True},
        {"a": {"b": [1, 3]}, "keep": True},
    ) == ["a.b[1]"]
    assert variant_diff_paths(
        {"a": [1], "keep": True},
        {"a": [1, 2], "keep": True},
    ) == ["a"]
    assert variant_diff_paths({"a": 1}, {"a": 2}) == ["a"]


def _usage(*, input_tokens: int, cached_tokens: int = 0, output_tokens: int,
           reasoning_tokens: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


def test_estimate_cost_matches_chunk_53_low_at_both_bands():
    usage = _usage(input_tokens=24_755, output_tokens=9_083, reasoning_tokens=8_299)

    assert estimate_cost_cny(usage, band="base") == Decimal("0.061087")
    assert str(estimate_cost_cny(usage, band="base")) == "0.061087"
    assert estimate_cost_cny(usage, band="peak") == Decimal("0.122174")


def test_estimate_cost_uses_cached_input_rate_and_ignores_reasoning_split():
    usage = _usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000,
                   reasoning_tokens=50_000)

    assert estimate_cost_cny(usage, band="base") == Decimal("1.008")
    assert estimate_cost_cny(usage, band="peak") == Decimal("2.016")
    usage["output_tokens_details"]["reasoning_tokens"] = 90_000
    assert estimate_cost_cny(usage, band="base") == Decimal("1.008")
    with pytest.raises(ValueError, match="band"):
        estimate_cost_cny(usage, band="offpeak")


def _result(*events: tuple[str, int, int]) -> dict:
    return {
        "persons": [],
        "timeline": [
            {"event_type": event_type, "start_seconds": start, "end_seconds": end}
            for event_type, start, end in events
        ],
    }


def test_dialogue_coverage_uses_half_open_intervals():
    result = _result(("DIALOGUE", 1000, 2000), ("SCENE", 0, 5000))

    assert dialogue_coverage(result, []) == 1.0
    assert dialogue_coverage(result, [{"start_seconds": 0, "end_seconds": 1000}]) == 0.0
    assert dialogue_coverage(result, [{"start_seconds": 2000, "end_seconds": 3000}]) == 0.0
    assert dialogue_coverage(result, [{"start_seconds": 1000, "end_seconds": 2000}]) == 1.0
    assert dialogue_coverage(result, [{"start_seconds": 500, "end_seconds": 1500}]) == 1.0
    assert dialogue_coverage(result, [
        {"start_seconds": 0, "end_seconds": 1000},
        {"start_seconds": 1500, "end_seconds": 1600},
        {"start_seconds": 2500, "end_seconds": 3000},
    ]) == pytest.approx(1 / 3)


def test_dialogue_coverage_ignores_non_dialogue_events():
    result = _result(("SCENE", 0, 5000), ("ON_SCREEN_TEXT", 0, 5000))
    assert dialogue_coverage(result, [{"start_seconds": 1000, "end_seconds": 2000}]) == 0.0


def test_summarize_groups_rows_by_variant_with_decimal_costs():
    rows = [
        {"variant": "full/low", "chunk": 53, "repeat": 1, "cost_cny": Decimal("0.061087"),
         "reasoning_tokens": 8299, "output_tokens": 9083, "schema_ok": True,
         "dialogue_coverage": 1.0},
        {"variant": "full/low", "chunk": 54, "repeat": 1, "cost_cny": "0.020000",
         "reasoning_tokens": 1000, "output_tokens": 2000, "schema_ok": False,
         "dialogue_coverage": 0.5},
        {"variant": "course/none", "chunk": 53, "repeat": 1, "cost_cny": Decimal("0.03"),
         "reasoning_tokens": 0, "output_tokens": 3000, "schema_ok": True,
         "dialogue_coverage": 1.0},
    ]

    summary = summarize(rows)

    assert list(summary) == ["full/low", "course/none"]
    assert summary["full/low"] == {
        "n": 2,
        "mean_cost_cny": "0.0405435",
        "mean_reasoning_tokens": 4649.5,
        "mean_output_tokens": 5541.5,
        "schema_pass_rate": 0.5,
        "mean_dialogue_coverage": 0.75,
    }
    assert summary["course/none"] == {
        "n": 1,
        "mean_cost_cny": "0.03",
        "mean_reasoning_tokens": 0.0,
        "mean_output_tokens": 3000.0,
        "schema_pass_rate": 1.0,
        "mean_dialogue_coverage": 1.0,
    }
    assert isinstance(summary["full/low"]["mean_cost_cny"], str)


def test_summarize_returns_empty_mapping_without_rows():
    assert summarize([]) == {}


def test_build_variant_accepts_the_real_projected_canonical_schema():
    from engine.parsev2.schemas import VideoParseResult
    from gateway.openai.multimodal import _deepseek_response_schema
    from gateway.schema_normalization import inline_schema_refs

    request = _request()
    request["text"]["format"]["schema"] = _deepseek_response_schema(
        inline_schema_refs(VideoParseResult.model_json_schema())
    )

    variant = build_variant(request, event_profile="course", reasoning_effort="none")

    assert _event_types(variant) == ["SCENE", "DIALOGUE", "ON_SCREEN_TEXT"]
    assert _timeline_branches(variant) == [
        _timeline_branches(request)[0],
        _timeline_branches(request)[2],
        _timeline_branches(request)[5],
    ]
