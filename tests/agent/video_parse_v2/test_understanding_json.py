from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from gateway.usage import ModelCallCancelledError

from agent.video_parse_v2.state import ChunkExecution
from agent.video_parse_v2.understanding import ChunkUnderstandingWorkflow
from agent.video_parse_v2.understanding.response import UnderstandingModelResponse
from engine.parsev2.observations import OCRResult
from engine.parsev2.schemas import VideoParseResult
from tests.agent.video_parse_v2.test_framework import _inputs, _lease


def _execution(*, understanding_enabled=True, ocr_enabled=False):
    return ChunkExecution(
        "chunk-3", "video-task", 3, 60_000, 80_000, 60_000, 80_000,
        _inputs(understanding_enabled=understanding_enabled, ocr_enabled=ocr_enabled),
        _lease(), background={"series": "乐队故事", "人物": {"爱音": "粉色头发"}},
        source_media_ref="artifact://source.mp4",
    )


def _result():
    return VideoParseResult.model_validate({
        "persons": ["爱音", "灯"],
        "timeline": [{
            "event_type": "DIALOGUE", "start_seconds": 61_000, "end_seconds": 63_000,
            "description": "爱音询问灯是否愿意组乐队。", "participants": ["爱音", "灯"],
            "visible_persons": ["灯"], "speaker": "爱音",
            "text_variants": [
                {"language": "ja", "role": "ORIGINAL", "text": "バンドやりたい？"},
                {"language": "zh-Hans", "role": "TRANSLATION", "text": "想组乐队吗？"},
            ],
        }, {
            "event_type": "ACTION", "start_seconds": 63_000, "end_seconds": 64_000,
            "description": "灯低下头。", "participants": ["灯"], "visible_persons": ["灯"],
        }, {
            "event_type": "DIALOGUE", "start_seconds": 65_000, "end_seconds": 67_000,
            "description": "画外传来简短回应，说话人无法确认。",
            "participants": [], "visible_persons": ["灯"], "speaker": None,
            "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "うん。"}],
        }, {
            "event_type": "ON_SCREEN_TEXT", "start_seconds": 70_000, "end_seconds": 80_000,
            "description": "墙上写着排练室。", "participants": [], "visible_persons": [],
            "language": "zh-Hans", "text": "排练室",
        }],
    })


def _runtime(execution, *, result=None, asr=None, ocr=None):
    return SimpleNamespace(
        load_chunk_execution=AsyncMock(return_value=execution),
        prepare_chunk_visual=AsyncMock(),
        transcribe_chunk=AsyncMock(return_value=asr if asr is not None else {"transcript": []}),
        read_chunk_text=AsyncMock(return_value=ocr),
        understand_chunk=AsyncMock(return_value=result),
        repair_chunk_understanding=AsyncMock(),
        commit_chunk_result=AsyncMock(),
        notify_chunk_committed=AsyncMock(),
        cleanup_chunk_execution=AsyncMock(),
    )


def test_direct_json_preserves_full_result_and_original_evidence_at_commit():
    execution = _execution(ocr_enabled=True)
    asr = {"language": "ja", "transcript": [{
        "start_seconds": 61_000, "end_seconds": 63_000,
        "text": "バンドやりたい？", "speaker_id": "speaker1",
        "words": [{"word": "バンド", "start_ms": 61_000, "end_ms": 61_700, "score": 0.9}],
    }]}
    original_asr = deepcopy(asr)
    ocr = OCRResult.model_validate({"lines": [{
        "start_seconds": 61_000, "end_seconds": 63_000,
        "language": "zh-Hans", "text": "想组乐队吗？",
    }]})
    result = _result()
    runtime = _runtime(execution, result=result, asr=asr, ocr=ocr)

    # A Canvas argument or a tool callback would violate this host interface.
    async def generate(loaded, *, asr_result, ocr_result, prompt):
        assert loaded is execution
        assert asr_result == original_asr
        assert ocr_result == ocr
        lines = prompt.splitlines()
        supplied_asr = json.loads(next(line.removeprefix("ASR input: ") for line in lines
                                       if line.startswith("ASR input: ")))
        expected_asr = deepcopy(original_asr)
        expected_asr["transcript"][0].pop("words")
        assert supplied_asr == expected_asr
        assert json.loads(next(line.removeprefix("OCR input: ") for line in lines
                               if line.startswith("OCR input: "))) == ocr.model_dump(mode="json")
        assert json.loads(next(line.removeprefix("Background context: ") for line in lines
                               if line.startswith("Background context: "))) == execution.background
        for tool_name in ("get_context_canvas", "write_context_canvas", "CanvasCompletion"):
            assert tool_name not in prompt
        return result

    runtime.understand_chunk.side_effect = generate
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding("chunk-3"))

    runtime.understand_chunk.assert_awaited_once()
    runtime.commit_chunk_result.assert_awaited_once()
    committed = runtime.commit_chunk_result.await_args
    assert committed.args[0] is execution
    assert committed.args[1].model_dump(mode="json") == result.model_dump(mode="json")
    assert committed.kwargs["evidence"] == {
        "asr": original_asr, "ocr": ocr.model_dump(mode="json"),
        "media": {"source_media_ref": "artifact://source.mp4", "video_id": "video-1",
                  "start_ms": 60_000, "end_ms": 80_000},
    }
    assert asr == original_asr
    runtime.notify_chunk_committed.assert_awaited_once_with(execution)
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


def _repair_execution():
    execution = _execution()
    config = execution.inputs.parse_config
    inputs = execution.inputs.model_copy(update={
        "parse_config": config.model_copy(update={
            "execution": config.execution.model_copy(update={"max_retry_count": 2}),
        }),
    })
    return replace(execution, inputs=inputs)


def _invalid_candidate():
    candidate = _result().model_dump(mode="json")
    candidate["timeline"][1]["text_variants"] = []
    return candidate


def test_understanding_transport_preserves_invalid_value_and_canonical_schema():
    candidate = _invalid_candidate()
    response = UnderstandingModelResponse.model_validate_json(json.dumps(candidate))
    assert response.root == candidate
    assert UnderstandingModelResponse.model_json_schema() == VideoParseResult.model_json_schema()
    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(response.root)


def test_schema_feedback_repairs_original_candidate_without_repeating_media():
    execution = _repair_execution()
    bad = _invalid_candidate()
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=bad))
    runtime.repair_chunk_understanding.return_value = UnderstandingModelResponse(root=_result().model_dump(mode="json"))
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    prompt = runtime.repair_chunk_understanding.await_args.kwargs["prompt"]
    assert "timeline.1.ACTION.text_variants" in prompt
    assert json.loads(prompt.split("Rejected response JSON:\n", 1)[1]) == bad
    assert 0 < runtime.repair_chunk_understanding.await_args.kwargs["remaining_seconds"] <= 120
    runtime.understand_chunk.assert_awaited_once()
    runtime.transcribe_chunk.assert_awaited_once()
    runtime.prepare_chunk_visual.assert_awaited_once()
    runtime.commit_chunk_result.assert_awaited_once()
    assert runtime.commit_chunk_result.await_args.args[1] == _result()


@pytest.mark.parametrize("null_candidate", [False, True])
def test_schema_repairs_are_bounded_and_never_commit_invalid_results(null_candidate):
    execution = _repair_execution()
    bad = None if null_candidate else _invalid_candidate()
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=bad))
    runtime.repair_chunk_understanding.return_value = UnderstandingModelResponse(root=bad)
    with pytest.raises(ValidationError):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    runtime.understand_chunk.assert_awaited_once()
    assert runtime.repair_chunk_understanding.await_count == 2
    runtime.commit_chunk_result.assert_not_awaited()
    runtime.cleanup_chunk_execution.assert_awaited_once()


def test_feedback_uses_latest_invalid_candidate_and_preserves_json_syntax_error():
    execution = _repair_execution()
    runtime = _runtime(execution)
    raw = '{"persons": [], "timeline": ['
    try:
        UnderstandingModelResponse.model_validate_json(raw)
    except ValidationError as error:
        runtime.understand_chunk.side_effect = error
    bad = _invalid_candidate()
    runtime.repair_chunk_understanding.side_effect = [
        UnderstandingModelResponse(root=bad), UnderstandingModelResponse(root=_result().model_dump(mode="json")),
    ]
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    prompts = [c.kwargs["prompt"] for c in runtime.repair_chunk_understanding.await_args_list]
    assert json.loads(prompts[0].split("Rejected response JSON:\n", 1)[1]) == raw
    assert json.loads(prompts[1].split("Rejected response JSON:\n", 1)[1]) == bad
    runtime.commit_chunk_result.assert_awaited_once()


def test_schema_repair_does_not_reset_original_deadline(monkeypatch):
    from agent.video_parse_v2.understanding import workflow
    execution = _repair_execution()
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(workflow, "time", SimpleNamespace(monotonic=lambda: clock.value))
    runtime = _runtime(execution)
    async def generate(*_args, **_kwargs):
        clock.value = 119
        return UnderstandingModelResponse(root=_invalid_candidate())
    async def repair(*_args, **kwargs):
        assert kwargs["remaining_seconds"] == 1
        clock.value = 121
        return UnderstandingModelResponse(root=_result().model_dump(mode="json"))
    runtime.understand_chunk.side_effect = generate
    runtime.repair_chunk_understanding.side_effect = repair
    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime)._generate_result(execution, {"transcript": []}, None))
    runtime.repair_chunk_understanding.assert_awaited_once()
    runtime.commit_chunk_result.assert_not_awaited()


def test_schema_repair_cancellation_is_not_retried():
    execution = _repair_execution()
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=_invalid_candidate()))
    runtime.repair_chunk_understanding.side_effect = ModelCallCancelledError("model call cancelled")
    with pytest.raises(ModelCallCancelledError):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    runtime.repair_chunk_understanding.assert_awaited_once()
    runtime.commit_chunk_result.assert_not_awaited()
    runtime.cleanup_chunk_execution.assert_awaited_once()


@pytest.mark.parametrize("understanding_enabled", [False, True])
def test_empty_observations_and_valid_empty_result_can_commit(understanding_enabled):
    execution = _execution(understanding_enabled=understanding_enabled)
    result = VideoParseResult(persons=[], timeline=[])
    runtime = _runtime(execution, result=result)

    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding("chunk-3"))

    assert runtime.understand_chunk.await_count == int(understanding_enabled)
    runtime.read_chunk_text.assert_not_awaited()
    runtime.commit_chunk_result.assert_awaited_once()
    assert runtime.commit_chunk_result.await_args.args[1] == result
    assert runtime.commit_chunk_result.await_args.kwargs["evidence"]["ocr"] is None
    runtime.notify_chunk_committed.assert_awaited_once_with(execution)
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


@pytest.mark.parametrize("invalid_bound", ["before", "after"])
def test_one_out_of_range_event_rejects_entire_direct_result(invalid_bound):
    execution = _execution()
    values = _result().model_dump(mode="json")
    if invalid_bound == "before":
        values["timeline"][0]["start_seconds"] = 59_999
    else:
        values["timeline"][-1]["end_seconds"] = 80_001
    runtime = _runtime(execution, result=VideoParseResult.model_validate(values))

    with pytest.raises(ValueError, match="outside the authorized media time range"):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding("chunk-3"))

    runtime.understand_chunk.assert_awaited_once()
    runtime.commit_chunk_result.assert_not_awaited()
    runtime.notify_chunk_committed.assert_not_awaited()
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


def test_failed_direct_generation_never_commits_and_preserves_original_error_on_cleanup():
    execution = _execution()
    runtime = _runtime(execution)
    original_error = RuntimeError("invalid model response")
    runtime.understand_chunk.side_effect = original_error
    runtime.cleanup_chunk_execution.side_effect = OSError("cleanup also failed")

    with pytest.raises(RuntimeError) as captured:
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding("chunk-3"))

    assert captured.value is original_error
    runtime.understand_chunk.assert_awaited_once()
    runtime.commit_chunk_result.assert_not_awaited()
    runtime.notify_chunk_committed.assert_not_awaited()
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


def test_understanding_stably_orders_events_without_another_model_request():
    execution = _repair_execution()
    candidate = _result().model_dump(mode="json")
    candidate["timeline"].reverse()
    # Same-time events keep their original relative order, without merging them.
    tied = {**candidate["timeline"][-1], "description": "Another observation at the same time"}
    candidate["timeline"].append(tied)
    original = deepcopy(candidate)
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=candidate))
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    committed = runtime.commit_chunk_result.await_args.args[1].model_dump(mode="json")
    assert committed["timeline"] == sorted(original["timeline"], key=lambda e: e["start_seconds"])
    assert candidate == original
    runtime.understand_chunk.assert_awaited_once()
    runtime.repair_chunk_understanding.assert_not_awaited()


def test_understanding_sorted_candidate_keeps_other_errors_and_feedback_locations():
    execution = _repair_execution()
    candidate = _invalid_candidate()
    candidate["timeline"].reverse()
    original = deepcopy(candidate)
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=candidate))
    runtime.repair_chunk_understanding.return_value = UnderstandingModelResponse(root=_result().model_dump(mode="json"))
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    prompt = runtime.repair_chunk_understanding.await_args.kwargs["prompt"]
    assert "timeline.1.ACTION.text_variants" in prompt
    rejected = json.loads(prompt.split("Rejected response JSON:\n", 1)[1])
    assert rejected["timeline"] == sorted(original["timeline"], key=lambda e: e["start_seconds"])
    assert rejected["timeline"][1]["text_variants"] == []
    assert candidate == original
    runtime.repair_chunk_understanding.assert_awaited_once()
    runtime.commit_chunk_result.assert_awaited_once()


@pytest.mark.parametrize("invalid_time", [True, "61000", None])
def test_understanding_normalization_never_coerces_invalid_times(invalid_time):
    from engine.parsev2.validation import normalize_parse_response
    candidate = _result().model_dump(mode="json")
    candidate["timeline"][0]["start_seconds"] = invalid_time
    original = deepcopy(candidate)
    normalized = normalize_parse_response(candidate)
    assert normalized == original
    with pytest.raises(ValidationError):
        VideoParseResult.model_validate(normalized)


@pytest.mark.parametrize("corrected", [True, False])
def test_time_range_feedback_retains_candidate_and_honors_retry_limit(corrected):
    from engine.parsev2.validation import ResultTimeRangeError
    execution = _repair_execution()
    candidate = _result().model_dump(mode="json")
    candidate["timeline"][-1]["end_seconds"] = execution.allowed_end_seconds + 300
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=candidate))
    runtime.repair_chunk_understanding.return_value = UnderstandingModelResponse(
        root=_result().model_dump(mode="json") if corrected else candidate,
    )
    if corrected:
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
        runtime.commit_chunk_result.assert_awaited_once()
        runtime.repair_chunk_understanding.assert_awaited_once()
    else:
        with pytest.raises(ResultTimeRangeError):
            asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
        runtime.commit_chunk_result.assert_not_awaited()
        assert runtime.repair_chunk_understanding.await_count == 2
    for call in runtime.repair_chunk_understanding.await_args_list:
        prompt = call.kwargs["prompt"]
        assert "timeline.3.ON_SCREEN_TEXT" in prompt
        assert "actual=[70000, 80300) ms, allowed=[60000, 80000) ms" in prompt
        assert json.loads(prompt.split("Rejected response JSON:\n", 1)[1]) == candidate
        assert call.kwargs["remaining_seconds"] <= 120
    runtime.understand_chunk.assert_awaited_once()


def test_unrelated_runtime_value_error_does_not_trigger_model_repair():
    execution = _repair_execution()
    runtime = _runtime(execution)
    runtime.understand_chunk.side_effect = ValueError("invalid local media input")
    with pytest.raises(ValueError, match="invalid local media input"):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    runtime.repair_chunk_understanding.assert_not_awaited()
    runtime.commit_chunk_result.assert_not_awaited()


def test_whole_video_cue_survives_understanding_canvas_and_commit_unchanged():
    execution = replace(_execution(), dialogue_bounds=(0, 120_000))
    cue = {"start_seconds": 59_700, "end_seconds": 80_300, "text": "A complete utterance crossing the Chunk."}
    value = _result().model_dump(mode="json")
    dialogue = value["timeline"][0]
    dialogue.update(start_seconds=cue["start_seconds"], end_seconds=cue["end_seconds"])
    dialogue["text_variants"] = [{"language": "en", "role": "ORIGINAL", "text": cue["text"]}]
    original = deepcopy(value)
    runtime = _runtime(execution, result=UnderstandingModelResponse(root=value), asr={"transcript": [cue]})
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    actual = runtime.commit_chunk_result.await_args.args[1].model_dump(mode="json")
    assert actual == original
    assert runtime.commit_chunk_result.await_args.kwargs["evidence"]["asr"]["transcript"] == [cue]
    prompt = runtime.understand_chunk.await_args.kwargs["prompt"]
    assert "DIALOGUE from whole-video ASR may cross Chunk boundaries within [0, 120000) ms" in prompt
    assert "ASR comes from the complete source video" in prompt
    assert "Do not split or clip a cue merely to fit the Chunk or a sentence" in prompt
    assert "Only report events inside" not in prompt
    assert "fully inside the authorized range" not in prompt
    runtime.repair_chunk_understanding.assert_not_awaited()
    runtime.commit_chunk_result.assert_awaited_once()


@pytest.mark.parametrize("event_type,end_ms", [("DIALOGUE", 120_001), ("ACTION", 80_300)])
def test_video_dialogue_authority_does_not_relax_video_or_visual_limits(event_type, end_ms):
    execution = replace(_execution(), dialogue_bounds=(0, 120_000))
    event = {"event_type": event_type, "start_seconds": 79_000, "end_seconds": end_ms,
             "description": "out of range", "participants": [], "visible_persons": []}
    if event_type == "DIALOGUE":
        event.update(speaker=None, text_variants=[{"language": "en", "role": "ORIGINAL", "text": "utterance"}])
    runtime = _runtime(execution, result=UnderstandingModelResponse(root={"persons": [], "timeline": [event]}))
    with pytest.raises(ValueError, match="time range"):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).InvokeUnderstanding(execution.chunk_parse_task_id))
    runtime.commit_chunk_result.assert_not_awaited()
