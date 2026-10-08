from __future__ import annotations

import pytest
from pydantic import ValidationError

import engine.rag.indexing.projection as projection_module
from engine.parsev2.schemas import VideoParseResult
from engine.rag.indexing.projection import (
    INDEX_PROJECTION_VERSION,
    LEXICAL_FIELD_WEIGHTS,
    LEXICAL_PHRASE_FIELDS,
    LexicalField,
    SourceEnrichment,
    compute_index_projection_revision,
    project_chunk_index,
)


def _result() -> VideoParseResult:
    return VideoParseResult.model_validate({
        "persons": ["爱音", "乐奈"],
        "timeline": [
            {
                "event_type": "ACTION",
                "start_seconds": 0,
                "end_seconds": 4000,
                "description": "爱音进门",
                "participants": ["爱音"],
                "visible_persons": ["爱音"],
            },
            {
                "event_type": "DIALOGUE",
                "start_seconds": 9000,
                "end_seconds": 12000,
                "description": "爱音否认难过",
                "participants": ["爱音", "乐奈"],
                "visible_persons": ["乐奈"],
                "speaker": "爱音",
                "text_variants": [
                    {"language": "ja", "role": "ORIGINAL", "text": "大丈夫。"},
                    {"language": "zh-Hans", "role": "TRANSLATION", "text": "我没事。"},
                ],
            },
            {
                "event_type": "ON_SCREEN_TEXT",
                "start_seconds": 12000,
                "end_seconds": 15000,
                "description": "手机显示消息",
                "participants": [],
                "visible_persons": ["乐奈"],
                "language": "zh-Hans",
                "text": "稍后见",
            },
        ],
    })


def test_projection_keeps_full_timeline_indices_and_half_open_chunk_overlap() -> None:
    result = _result()
    projection = project_chunk_index(
        result, chunk_start_seconds=10000, chunk_end_seconds=12000,
        description_language="zh-Hans",
    )

    assert {field.timeline_index for field in projection.lexical_fields} == {1}
    assert {field.raw_text for field in projection.lexical_fields if field.field == "dialogue"} == {
        "大丈夫。", "我没事。"
    }
    assert not any(field.field == "screen_text" for field in projection.lexical_fields)
    assert next(field for field in projection.lexical_fields if field.field == "event_description").language == "zh-Hans"
    assert "爱音否认难过" in projection.semantic_text
    assert "手机显示消息" not in projection.semantic_text

    next_chunk = project_chunk_index(
        result, chunk_start_seconds=12000, chunk_end_seconds=16000
    )
    assert {field.timeline_index for field in next_chunk.lexical_fields} == {2}
    screen_text = [field for field in next_chunk.lexical_fields if field.field == "screen_text"]
    assert len(screen_text) == 1
    assert screen_text[0].raw_text == "稍后见"
    assert screen_text[0].language == "zh-Hans"


def test_names_and_dialogue_keep_the_same_event_source() -> None:
    projection = project_chunk_index(
        _result(), chunk_start_seconds=10000, chunk_end_seconds=12000
    )

    names = [field for field in projection.lexical_fields if field.field == "person_names"]
    assert [(field.raw_text, field.timeline_index) for field in names] == [
        ("爱音", 1), ("乐奈", 1)
    ]
    dialogue = [field for field in projection.lexical_fields if field.field == "dialogue"]
    assert [(field.language, field.role, field.speaker) for field in dialogue] == [
        ("ja", "ORIGINAL", "爱音"),
        ("zh-Hans", "TRANSLATION", "爱音"),
    ]
    assert all(field.start_seconds == 9000 and field.end_seconds == 12000 for field in dialogue)


def test_enrichments_keep_chunk_source_and_questions_stay_lexical() -> None:
    projection = project_chunk_index(
        _result(),
        chunk_start_seconds=10000,
        chunk_end_seconds=12000,
        enrichments=[SourceEnrichment(
            language="zh-Hans",
            summary="爱音向乐奈解释",
            keywords=["解释", "解释"],
            questions=["是谁说了我没事？"],
        )],
    )

    enhanced = [field for field in projection.lexical_fields if field.enrichment_index is not None]
    assert [(field.field, field.raw_text) for field in enhanced] == [
        ("summary", "爱音向乐奈解释"),
        ("keywords", "解释"),
        ("questions", "是谁说了我没事？"),
    ]
    assert all(field.timeline_index is None for field in enhanced)
    assert all((field.start_seconds, field.end_seconds) == (10000, 12000) for field in enhanced)
    assert "爱音向乐奈解释" in projection.semantic_text
    assert "是谁说了我没事？" not in projection.semantic_text
    assert not any(
        field.field == "dialogue" and field.raw_text == "是谁说了我没事？"
        for field in projection.lexical_fields
    )


def test_lexical_only_question_change_changes_projection_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = project_chunk_index(
        _result(), chunk_start_seconds=10000, chunk_end_seconds=12000,
        enrichments=[SourceEnrichment(language="zh-Hans", questions=["谁说了没事？"])],
    )
    new = project_chunk_index(
        _result(), chunk_start_seconds=10000, chunk_end_seconds=12000,
        enrichments=[SourceEnrichment(language="zh-Hans", questions=["谁说了我没事？"])],
    )

    assert old.semantic_text == new.semantic_text
    assert compute_index_projection_revision(old) != compute_index_projection_revision(new)
    assert compute_index_projection_revision(old) == compute_index_projection_revision(old)
    assert INDEX_PROJECTION_VERSION
    prior_revision = compute_index_projection_revision(old)
    monkeypatch.setattr(projection_module, "INDEX_PROJECTION_VERSION", "new-projection-contract")
    assert compute_index_projection_revision(old) != prior_revision


def test_lexical_policy_covers_every_projected_field() -> None:
    assert set(LEXICAL_FIELD_WEIGHTS) == {
        "dialogue", "screen_text", "person_names", "event_description",
        "keywords", "questions", "summary",
    }
    assert set(LEXICAL_PHRASE_FIELDS) == {"dialogue", "screen_text"}


def test_empty_chunk_has_no_semantic_input_or_fabricated_lexical_evidence() -> None:
    projection = project_chunk_index(
        _result(), chunk_start_seconds=4000, chunk_end_seconds=9000
    )

    assert projection.lexical_fields == []
    assert projection.semantic_text == ""


@pytest.mark.parametrize("start,end", [(10, 10), (-1, 10), (True, 10), (1.5, 10)])
def test_projection_rejects_invalid_chunk_bounds(start: object, end: object) -> None:
    with pytest.raises(ValueError, match="Chunk bounds"):
        project_chunk_index(
            _result(), chunk_start_seconds=start, chunk_end_seconds=end
        )


def test_lexical_field_rejects_unbound_or_misbound_source() -> None:
    with pytest.raises(ValidationError, match="event fields require only timeline_index"):
        LexicalField(
            field="dialogue", raw_text="我没事", start_seconds=9000, end_seconds=12000
        )
    with pytest.raises(ValidationError, match="enrichment fields require only enrichment_index"):
        LexicalField(
            field="questions", raw_text="她说了什么？", start_seconds=10000,
            end_seconds=12000, timeline_index=1,
        )


@pytest.mark.parametrize(
    ("language", "role"),
    [(None, "ORIGINAL"), ("ja", None)],
)
def test_dialogue_lexical_field_requires_language_and_role(
    language: str | None,
    role: str | None,
) -> None:
    with pytest.raises(ValidationError, match="dialogue requires language and role"):
        LexicalField(
            field="dialogue", raw_text="大丈夫。", language=language, role=role,
            start_seconds=9000, end_seconds=12000, timeline_index=1,
        )


@pytest.mark.parametrize("field", ["keywords", "questions", "summary"])
def test_enrichment_lexical_field_requires_language(field: str) -> None:
    with pytest.raises(ValidationError, match="enrichment fields require language"):
        LexicalField(
            field=field, raw_text="爱音", start_seconds=10000, end_seconds=12000,
            enrichment_index=0,
        )


def test_screen_text_lexical_field_allows_unknown_language() -> None:
    field = LexicalField(
        field="screen_text", raw_text="稍后见", start_seconds=12000,
        end_seconds=15000, timeline_index=2,
    )
    assert field.language is None
