from __future__ import annotations

import pytest
from dataclasses import replace
from pydantic import ValidationError

from engine.parsev2.schemas import VideoParseResult
from engine.rag.encoding import EncoderContract, accept_embedding_result
from engine.rag.indexing.events import (
    EventIndexDocument, EventIndexingContract, build_event_index_receipt,
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


def _document(plan):
    artifact = accept_embedding_result(plan.semantic_request, modality="text", model="embedding", vector=[1., 2.])
    frames = tuple(CaptureFrame(requested_at_ms=at, image_sha256="a" * 64) for at in plan.frame_timestamps_ms)
    return finalize_event_index(plan, artifact, [0.6, 0.8], frames)


def test_event_projection_keeps_seven_types_and_root_ownership():
    result, contract = _result(), _contract()
    before = compute_index_projection_revision(project_chunk_index(result, chunk_start_seconds=0, chunk_end_seconds=7000))
    plans = plan_event_index("task", "chunk", 2, result, 0, 7000, contract)
    assert [plan.timeline_index for plan in plans] == list(range(7))
    assert len({plan.event_type for plan in plans}) == 7
    for plan in plans:
        source = _document(plan).as_source()
        assert set(source) == {
            "video_chunk_id", "parse_task_id", "version", "timeline_index", "event_type",
            "lexical_fields", "semantic_vector", "vision_vector", "detail",
        } | ({"speaker"} if plan.event_type == "DIALOGUE" else set())
        assert all(set(field) == {"field", "raw_text", "language", "role"} for field in source["lexical_fields"])
        assert source["detail"]["capture"]["frames"][0]["actual_at_ms"] is None
        assert "event_id" not in source
    dialogue = plans[2]
    assert dialogue.speaker == "A"
    assert [(field.language, field.role) for field in dialogue.lexical_fields if field.field == "dialogue"] == [
        ("ja", "ORIGINAL"), ("zh-Hans", "TRANSLATION"),
    ]
    assert "真是居高临下啊" in dialogue.semantic_request.semantic_text
    assert "event 1" not in dialogue.semantic_request.semantic_text
    assert compute_index_projection_revision(project_chunk_index(result, chunk_start_seconds=0, chunk_end_seconds=7000)) == before


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
    artifact = accept_embedding_result(other.semantic_request, modality="text", model="embedding", vector=[1., 2.])
    frames = [CaptureFrame(requested_at_ms=plan.frame_timestamps_ms[0], image_sha256="a" * 64)]
    with pytest.raises(ValueError, match="different plan"):
        finalize_event_index(plan, artifact, [1., 2.], frames)
    artifact = accept_embedding_result(plan.semantic_request, modality="text", model="embedding", vector=[1., 2.])
    with pytest.raises(ValueError, match="planned timestamps"):
        finalize_event_index(plan, artifact, [1., 2.], [])
    with pytest.raises(ValueError, match="capture range"):
        finalize_event_index(plan, artifact, [1., 2.], [frames[0].model_copy(update={"actual_at_ms":3000})])
    copied = replace(plan,version=3)
    with pytest.raises(ValueError, match="different plan"):
        finalize_event_index(copied, artifact, [1.,2.], frames)


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
