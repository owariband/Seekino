from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.parsev2.observations import OCRResult, build_chunk_without_understanding
from engine.parsev2.validation import validate_result_time_range


def test_asr_mapping_preserves_unknown_attribution_and_original_language() -> None:
    result = build_chunk_without_understanding(
        asr_result={"transcript": [{
            "start_seconds": 13_500, "end_seconds": 15_000, "text": " hello ",
        }]},
        ocr_result=None,
    )
    assert result.model_dump(mode="json") == {
        "persons": [],
        "timeline": [{
            "event_type": "DIALOGUE",
            "start_seconds": 13_500,
            "end_seconds": 15_000,
            "description": "hello",
            "participants": [],
            "visible_persons": [],
            "speaker": None,
            "text_variants": [{"language": "und", "role": "ORIGINAL", "text": "hello"}],
        }],
    }


@pytest.mark.parametrize("language", [None, "", "ja"])
def test_ocr_mapping_preserves_text_and_optional_language(language) -> None:
    result = build_chunk_without_understanding(
        asr_result={"transcript": []},
        ocr_result=OCRResult.model_validate({"lines": [{
            "start_seconds": 1200, "end_seconds": 1500,
            "text": "  営業中  ", "language": language,
        }]}),
    )
    assert result.model_dump(mode="json") == {
        "persons": [],
        "timeline": [{
            "event_type": "ON_SCREEN_TEXT",
            "start_seconds": 1200,
            "end_seconds": 1500,
            "description": "営業中",
            "participants": [],
            "visible_persons": [],
            "language": language or None,
            "text": "営業中",
        }],
    }


def test_observations_skip_blank_text_and_sort_stably_without_merging() -> None:
    result = build_chunk_without_understanding(
        asr_result={"transcript": [
            {"start_seconds": 1000, "end_seconds": 3000, "text": "later end"},
            {"start_seconds": 1000, "end_seconds": 2000, "text": "asr"},
            {"start_seconds": 0, "end_seconds": 1, "text": " \n "},
        ]},
        ocr_result=OCRResult.model_validate({"lines": [
            {"start_seconds": 1000, "end_seconds": 2000, "text": "ocr"},
            {"start_seconds": 500, "end_seconds": 1000, "text": "first"},
            {"start_seconds": 0, "end_seconds": 1, "text": " "},
        ]}),
    )
    assert [event.description for event in result.timeline] == [
        "first", "asr", "ocr", "later end",
    ]
    assert result.persons == []


@pytest.mark.parametrize("ocr_result", [None, OCRResult()])
def test_empty_observations_produce_an_empty_formal_result(ocr_result) -> None:
    result = build_chunk_without_understanding(
        asr_result={"transcript": []}, ocr_result=ocr_result,
    )
    assert result.model_dump(mode="json") == {"persons": [], "timeline": []}


def test_event_time_authority_remains_a_separate_engine_validation() -> None:
    result = build_chunk_without_understanding(
        asr_result={"transcript": [{
            "start_seconds": 1000, "end_seconds": 20_001, "text": "hello",
        }]},
        ocr_result=None,
    )
    with pytest.raises(ValueError):
        validate_result_time_range(result, start_seconds=0, end_seconds=20_000)


@pytest.mark.parametrize("invalid_time", [True, 1.5, "1000"])
def test_ocr_schema_retains_strict_integer_millisecond_fields(invalid_time) -> None:
    with pytest.raises(ValidationError):
        OCRResult.model_validate({"lines": [{
            "start_seconds": invalid_time, "end_seconds": 2000, "text": "sign",
        }]})
