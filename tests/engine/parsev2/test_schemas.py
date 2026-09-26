from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from engine.parsev2.schemas import (
    ActionEvent,
    DialogueEvent,
    EmotionEvent,
    VideoParseResult,
)


def _valid_result_payload() -> dict[str, Any]:
    return {
        "persons": [
            {"person_id": "p_anon", "label": "爱音"},
            {"person_id": "p_rana", "label": "乐奈"},
        ],
        "timeline": [
            {
                "event_type": "EMOTION",
                "start_seconds": 11,
                "end_seconds": 17,
                "description": "爱音强忍眼泪，显得十分难过",
                "participant_person_ids": ["p_anon"],
                "visible_person_ids": ["p_anon"],
            },
            {
                "event_type": "DIALOGUE",
                "start_seconds": 12,
                "end_seconds": 16,
                "description": "爱音否认自己在难过",
                "participant_person_ids": ["p_anon", "p_rana"],
                "visible_person_ids": ["p_anon", "p_rana"],
                "speaker_person_id": "p_anon",
                "text_variants": [
                    {
                        "language": "ja",
                        "role": "ORIGINAL",
                        "text": "大丈夫、本当だよ。",
                    },
                    {
                        "language": "zh-Hans",
                        "role": "TRANSLATION",
                        "text": "我没事，真的。",
                    },
                ],
            },
        ],
    }


def test_result_dispatches_union_and_preserves_multilingual_dialogue() -> None:
    result = VideoParseResult.model_validate(_valid_result_payload())

    assert isinstance(result.timeline[0], EmotionEvent)
    assert isinstance(result.timeline[1], DialogueEvent)
    assert [variant.language for variant in result.timeline[1].text_variants] == [
        "ja",
        "zh-Hans",
    ]
    assert VideoParseResult.model_validate_json(result.model_dump_json()) == result


def test_dialogue_and_emotion_may_overlap() -> None:
    result = VideoParseResult.model_validate(_valid_result_payload())

    emotion, dialogue = result.timeline
    assert emotion.start_seconds < dialogue.start_seconds < emotion.end_seconds


def test_dialogue_requires_at_least_one_text_variant() -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["text_variants"] = []

    with pytest.raises(ValidationError, match="at least 1 item"):
        VideoParseResult.model_validate(payload)


def test_result_revalidates_prebuilt_event_instances() -> None:
    payload = _valid_result_payload()
    dialogue = DialogueEvent.model_validate(payload["timeline"][1])
    dialogue.text_variants.clear()
    payload["timeline"] = [dialogue]

    with pytest.raises(ValidationError, match="at least 1 item"):
        VideoParseResult.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", ""),
        ("text", ""),
        ("role", "PARAPHRASE"),
    ],
)
def test_dialogue_rejects_invalid_text_variant(field: str, value: str) -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["text_variants"][0][field] = value

    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(payload)


def test_dialogue_rejects_duplicate_language_and_role_pair() -> None:
    payload = _valid_result_payload()
    first_variant = payload["timeline"][1]["text_variants"][0]
    duplicate_variant = dict(first_variant)
    duplicate_variant["language"] = "JA"
    payload["timeline"][1]["text_variants"].append(duplicate_variant)

    with pytest.raises(ValidationError, match="unique language and role pairs"):
        VideoParseResult.model_validate(payload)


def test_dialogue_rejects_blank_variant_values_without_trimming_text() -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["text_variants"][0]["language"] = "  "

    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(payload)

    payload = _valid_result_payload()
    payload["timeline"][1]["text_variants"][0]["text"] = "  台词原文  "
    result = VideoParseResult.model_validate(payload)
    dialogue = result.timeline[1]
    assert isinstance(dialogue, DialogueEvent)
    assert dialogue.text_variants[0].text == "  台词原文  "


@pytest.mark.parametrize(
    ("start_seconds", "end_seconds"),
    [(3, 3), (4, 3), (-1, 3), (1.0, 3), ("1", 3)],
)
def test_event_rejects_invalid_integer_second_ranges(
    start_seconds: object,
    end_seconds: object,
) -> None:
    with pytest.raises(ValidationError):
        ActionEvent(
            event_type="ACTION",
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            description="爱音走进房间",
            participant_person_ids=["p_anon"],
            visible_person_ids=["p_anon"],
        )


def test_non_dialogue_event_rejects_dialogue_fields() -> None:
    payload = _valid_result_payload()
    payload["timeline"][0]["speaker_person_id"] = "p_anon"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VideoParseResult.model_validate(payload)


@pytest.mark.parametrize("event_type", [None, "SPEECH"])
def test_result_rejects_missing_or_unknown_event_type(event_type: str | None) -> None:
    payload = _valid_result_payload()
    if event_type is None:
        del payload["timeline"][1]["event_type"]
    else:
        payload["timeline"][1]["event_type"] = event_type

    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(payload)


def test_dialogue_requires_explicit_speaker_or_null() -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["speaker_person_id"] = None
    VideoParseResult.model_validate(payload)

    del payload["timeline"][1]["speaker_person_id"]
    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(payload)


def test_dialogue_speaker_must_be_a_participant() -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["participant_person_ids"] = ["p_rana"]

    with pytest.raises(
        ValidationError,
        match="speaker_person_id must be a participant",
    ):
        VideoParseResult.model_validate(payload)


@pytest.mark.parametrize(
    "reference_field",
    ["participant_person_ids", "visible_person_ids"],
)
def test_result_rejects_unknown_person_references(reference_field: str) -> None:
    payload = _valid_result_payload()
    payload["timeline"][0][reference_field] = ["p_unknown"]

    with pytest.raises(ValidationError, match="unknown person_id"):
        VideoParseResult.model_validate(payload)


def test_result_rejects_duplicate_person_ids() -> None:
    payload = _valid_result_payload()
    payload["persons"][1]["person_id"] = "p_anon"

    with pytest.raises(ValidationError, match="unique person_id"):
        VideoParseResult.model_validate(payload)


@pytest.mark.parametrize(
    "reference_field",
    ["participant_person_ids", "visible_person_ids"],
)
def test_event_rejects_duplicate_person_references(reference_field: str) -> None:
    payload = _valid_result_payload()
    payload["timeline"][0][reference_field] = ["p_anon", "p_anon"]

    with pytest.raises(ValidationError, match="must not contain duplicates"):
        VideoParseResult.model_validate(payload)


def test_result_rejects_unsorted_timeline_but_allows_equal_starts() -> None:
    payload = _valid_result_payload()
    payload["timeline"][1]["start_seconds"] = 11
    VideoParseResult.model_validate(payload)

    payload["timeline"][1]["start_seconds"] = 10
    with pytest.raises(ValidationError, match="ordered by start_seconds"):
        VideoParseResult.model_validate(payload)


def test_json_schema_exposes_event_type_discriminator() -> None:
    schema = VideoParseResult.model_json_schema()
    event_schema = schema["properties"]["timeline"]["items"]

    assert event_schema["discriminator"]["propertyName"] == "event_type"
    assert set(event_schema["discriminator"]["mapping"]) == {
        "ACTION",
        "DIALOGUE",
        "EMOTION",
        "ON_SCREEN_TEXT",
        "OTHER",
        "RELATION_CHANGE",
        "SCENE",
    }
    assert len(event_schema["oneOf"]) == 7
