from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.video_parse_v2.state import ChunkExecution
from agent.video_parse_v2.understanding import ChunkUnderstandingWorkflow
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
    }, {"start_seconds": 65_000, "end_seconds": 67_000, "text": "うん。", "speaker_id": "speaker2"}]}
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
