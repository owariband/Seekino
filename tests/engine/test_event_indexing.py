from __future__ import annotations

import pytest
from dataclasses import replace
from pydantic import ValidationError

from engine.parsev2.schemas import VideoParseResult
from engine.rag.encoding import EncoderContract, accept_embedding_result
from engine.rag.indexing.events import (
    EventIndexDocument, EventIndexingContract, EventSemanticSource, build_event_index_receipt,
    compute_event_id, finalize_event_index, plan_event_index,
)
from engine.rag.indexing.projection import compute_index_projection_revision, project_chunk_index
from engine.rag.vision.event_frames import CaptureFrame, EventFrameContract


def _contract(**capture_updates):
    encoder = EncoderContract("test", "embedding", "v1", 2)
    return EventIndexingContract(semantic_encoder=encoder, vision_encoder=encoder,
                                capture=EventFrameContract(max_height=720, **capture_updates))


def _result():
    timeline = []
    for index, kind in enumerate(("SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER")):
        event = dict(event_type=kind, start_seconds=index * 1000, end_seconds=(index + 1) * 1000,
                     description=f"event {index}", participants=["A"], visible_persons=["B"])
        if kind == "DIALOGUE":
            event.update(speaker="A", text_variants=[
                dict(language="ja", role="ORIGINAL", text="いいご身分ですわね。"),
                dict(language="zh-Hans", role="TRANSLATION", text="真是居高临下啊"),
            ])
        if kind == "ON_SCREEN_TEXT":
            event.update(language="ja", text="舞台")
        timeline.append(event)
    return VideoParseResult.model_validate(dict(persons=["A", "B"], timeline=timeline))


def _artifacts(plan):
    return [accept_embedding_result(item.request, modality="text", model="embedding", vector=[float(index + 1), 2.])
            for index, item in enumerate(plan.semantic_requests)]


def _document(plan):
    frames = tuple(CaptureFrame(requested_at_ms=at, image_sha256="a" * 64) for at in plan.frame_timestamps_ms)
    return finalize_event_index(plan, _artifacts(plan), [0.6, 0.8], frames)


def test_event_projection_keeps_seven_types_and_root_ownership():
    result, contract = _result(), _contract()
    before = compute_index_projection_revision(project_chunk_index(result, chunk_start_seconds=0, chunk_end_seconds=7000))
    plans = plan_event_index("task", "chunk", 2, result, 0, 7000, contract)
    assert [plan.timeline_index for plan in plans] == list(range(7))
    assert len({plan.event_type for plan in plans}) == 7
    for plan in plans:
        assert plan.semantic_requests[0].request.semantic_text == result.timeline[plan.timeline_index].description
        assert len(plan.semantic_requests) == (3 if plan.event_type == "DIALOGUE" else 1)
        source = _document(plan).as_source()
        assert set(source) == {
            "video_chunk_id", "parse_task_id", "version", "timeline_index", "event_type",
            "lexical_fields", "semantic_vectors", "vision_vector", "detail",
        } | ({"speaker"} if plan.event_type == "DIALOGUE" else set())
        assert all(set(field) == {"field", "raw_text", "language", "role"} for field in source["lexical_fields"])
        assert source["detail"]["capture"]["frames"][0]["actual_at_ms"] is None
        assert "event_id" not in source
    dialogue = plans[2]
    assert dialogue.speaker == "A"
    assert [item.request.semantic_text for item in dialogue.semantic_requests] == [
        "event 2", "いいご身分ですわね。", "真是居高临下啊",
    ]
    assert [item.source.model_dump() for item in dialogue.semantic_requests] == [
        {"source": "description", "language": None, "role": None},
        {"source": "dialogue", "language": "ja", "role": "ORIGINAL"},
        {"source": "dialogue", "language": "zh-Hans", "role": "TRANSLATION"},
    ]
    assert [item.model_dump(exclude={"vector"}) for item in _document(dialogue).semantic_vectors] == [
        item.source.model_dump() for item in dialogue.semantic_requests
    ]
    assert [(field.raw_text, field.language, field.role) for field in dialogue.lexical_fields if field.field == "dialogue"] == [
        ("いいご身分ですわね。", "ja", "ORIGINAL"), ("真是居高临下啊", "zh-Hans", "TRANSLATION"),
    ]
    assert [(field.raw_text, field.language) for field in plans[5].lexical_fields if field.field == "screen_text"] == [
        ("舞台", "ja"),
    ]
    assert compute_index_projection_revision(project_chunk_index(result, chunk_start_seconds=0, chunk_end_seconds=7000)) == before


@pytest.mark.parametrize("previous_version", ["timeline-event-index-v1", "timeline-event-index-v2"])
def test_old_single_text_contract_cannot_be_used_for_multivector_indexing(previous_version):
    contract = _contract()
    assert contract.projection_version == "timeline-event-index-v3"
    previous = contract.as_dict() | {"projection_version": previous_version}
    with pytest.raises(ValidationError, match="projection_version"):
        EventIndexingContract.model_validate(previous)


def test_source_identity_parent_version_and_original_index_do_not_depend_on_capture():
    result = _result()
    plan = plan_event_index("task", "chunk", 2, result, 2500, 4000, _contract())[0]
    assert plan.timeline_index == 2
    assert (plan.capture_start_ms, plan.capture_end_ms) == (2500, 3000)
    assert result.timeline[2].start_seconds == 2000
    assert plan.event_id == compute_event_id("task", "chunk", 2, 2)
    other = plan_event_index("task", "chunk", 2, result, 2500, 4000, _contract(max_frames=1))[0]
    assert other.event_id == plan.event_id
    assert other.contract.contract_id != plan.contract.contract_id
    assert plan.event_id != compute_event_id("task", "chunk", 3, 2)
    assert EventIndexingContract.model_validate_json(plan.contract.model_dump_json()) == plan.contract


def test_finalize_requires_exact_semantic_request_and_realized_sampling():
    plan, other = [plan_event_index("task", "chunk", 2, _result(), 0, 7000, _contract())[2] for _ in range(2)]
    frames = [CaptureFrame(requested_at_ms=plan.frame_timestamps_ms[0], image_sha256="a" * 64)]
    with pytest.raises(ValueError, match="different plan"):
        finalize_event_index(plan, _artifacts(other), [1., 2.], frames)
    artifacts = _artifacts(plan)
    with pytest.raises(ValueError, match="planned timestamps"):
        finalize_event_index(plan, artifacts, [1., 2.], [])
    with pytest.raises(ValueError, match="capture range"):
        finalize_event_index(plan, artifacts, [1., 2.], [frames[0].model_copy(update={"actual_at_ms":3000})])
    copied = replace(plan,version=3)
    assert copied.semantic_requests is not plan.semantic_requests
    for before, after in zip(plan.semantic_requests, copied.semantic_requests, strict=True):
        assert after is not before and after.request is not before.request
        assert after.source == before.source and after.request == before.request
    with pytest.raises(ValueError, match="different plan"):
        finalize_event_index(copied, artifacts, [1.,2.], frames)


@pytest.mark.parametrize("artifact_order", [(0, 1), (0, 1, 2, 2), (0, 2, 1), (0, 1, 1)])
def test_finalize_rejects_missing_extra_swapped_and_reused_text_artifacts(artifact_order):
    plan = plan_event_index("task", "chunk", 2, _result(), 0, 7000, _contract())[2]
    artifacts = _artifacts(plan)
    frames = [CaptureFrame(requested_at_ms=plan.frame_timestamps_ms[0], image_sha256="a" * 64)]
    with pytest.raises(ValueError, match="exactly one artifact|different plan"):
        finalize_event_index(plan, [artifacts[index] for index in artifact_order], [1., 2.], frames)


@pytest.mark.parametrize("source", [
    {"source": "description", "language": "zh-Hans"},
    {"source": "description", "role": "ORIGINAL"},
    {"source": "dialogue", "language": "ja"},
    {"source": "dialogue", "role": "ORIGINAL"},
])
def test_semantic_sources_require_unambiguous_metadata(source):
    with pytest.raises(ValidationError, match="semantic source"):
        EventSemanticSource.model_validate(source)


@pytest.mark.parametrize("representation_order", [(), (1, 2), (0, 1), (0, 1, 2, 2), (0, 0, 1, 2), (0, 2, 1)])
def test_event_document_requires_exactly_its_description_and_dialogue_sources(representation_order):
    plan = plan_event_index("task", "chunk", 2, _result(), 0, 7000, _contract())[2]
    source = _document(plan).as_source()
    vectors = source["semantic_vectors"]
    source["semantic_vectors"] = [vectors[index] for index in representation_order]
    with pytest.raises(ValidationError, match="semantic sources"):
        EventIndexDocument.model_validate(source)


def test_event_document_rejects_wrong_dialogue_source_and_mixed_dimensions():
    plan = plan_event_index("task", "chunk", 2, _result(), 0, 7000, _contract())[2]
    source = _document(plan).as_source()
    source["semantic_vectors"][1]["language"] = "en"
    with pytest.raises(ValidationError, match="semantic sources"):
        EventIndexDocument.model_validate(source)
    source = _document(plan).as_source()
    source["semantic_vectors"][1]["vector"] = [1., 2., 3.]
    with pytest.raises(ValidationError, match="share one dimension"):
        EventIndexDocument.model_validate(source)


@pytest.mark.parametrize("vector", [[], [0., 0.], [float("nan"), 1.]])
def test_event_document_rejects_invalid_text_vectors(vector):
    plan = plan_event_index("task", "chunk", 2, _result(), 0, 7000, _contract())[2]
    source = _document(plan).as_source()
    source["semantic_vectors"][1]["vector"] = vector
    with pytest.raises(ValidationError):
        EventIndexDocument.model_validate(source)


def test_additional_translation_keeps_one_event_and_adds_one_plain_text_request():
    result = _result()
    original = plan_event_index("task", "chunk", 2, result, 0, 7000, _contract())[2]
    payload = result.model_dump(mode="json")
    payload["timeline"][2]["text_variants"].append({
        "language": "en", "role": "TRANSLATION", "text": "You must have it nice.",
    })
    updated = plan_event_index("task", "chunk", 2, VideoParseResult.model_validate(payload), 0, 7000, _contract())[2]
    assert updated.event_id == original.event_id
    assert len(updated.semantic_requests) == 4
    assert updated.semantic_requests[-1].request.semantic_text == "You must have it nice."
    assert updated.semantic_requests[-1].source == EventSemanticSource(source="dialogue", language="en", role="TRANSLATION")
    assert len(_document(updated).semantic_vectors) == 4


def test_zero_receipt_is_explicit_and_set_validation_is_strict():
    contract = _contract()
    assert plan_event_index("task", "chunk", 1, _result(), 7000, 8000, contract) == ()
    receipt = build_event_index_receipt("events", contract, ())
    assert receipt.event_count == 0 and receipt.as_dict()["event_ids"] == []
    plans = plan_event_index("task", "chunk", 2, _result(), 0, 7000, contract)
    assert build_event_index_receipt("events", contract, plans) == build_event_index_receipt("events", contract, tuple(reversed(plans)))
    with pytest.raises(ValidationError, match="exact sorted"):
        build_event_index_receipt("events", contract, [plans[0], plans[0]])


def test_unknown_dialogue_speaker_is_not_inferred_and_non_dialogue_speaker_rejected():
    data = _result().model_dump(mode="json")
    data["timeline"][2]["speaker"] = None
    plan = plan_event_index("task", "chunk", 1, VideoParseResult.model_validate(data), 0, 7000, _contract())[2]
    assert _document(plan).as_source()["speaker"] is None
    source = _document(plan).as_source()
    source["event_type"] = "ACTION"
    source["speaker"] = "A"
    with pytest.raises(ValidationError, match="Only DIALOGUE"):
        EventIndexDocument.model_validate(source)
