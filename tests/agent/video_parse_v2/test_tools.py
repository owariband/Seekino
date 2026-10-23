from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from agent.utils.tool import AgentTool
from agent.utils.tool_result import JsonToolResult
from agent.utils.video_frames import build_inspect_video_frames_tool
from agent.video_parse_v2.timelineEvent_canvas import (
    FinishVoiceprintReview, ParseCanvasWrite, ParseContextCanvas,
)
from agent.video_parse_v2.tools import (
    AnalyzeSpeakerConsistencyArguments, TranscribeSegmentArguments,
    build_prefix_tools, build_refine_tools, build_understanding_tools, build_voiceprint_tools,
)


def _canvas(stage):
    return ParseContextCanvas(
        scope=f"{stage}:test", stage=stage,
        members={"member": {
            "sequence_no": 0, "version": 1, "bounds": (60_000, 80_000),
            "content": {"persons": [], "timeline": []},
        }}, evidence={"member": {"asr": {"transcript": []}}},
    )


@pytest.mark.parametrize("stage,builder", [
    ("understanding", build_understanding_tools),
    ("refine", build_refine_tools),
    ("prefix", build_prefix_tools),
])
def test_stage_collections_reuse_canvas_tools_without_optional_capabilities(stage, builder):
    canvas = _canvas(stage)
    tools = builder(canvas)
    assert all(isinstance(tool, AgentTool) for tool in tools)
    assert [tool.name for tool in tools] == ["get_context_canvas", "write_context_canvas"]
    write = tools[1]
    assert write.args_schema is ParseCanvasWrite
    receipt = write.execute(
        batch_id="add-person", expected_revision=canvas.revision,
        operations=[{"op": "persons", "member_ref": "member", "action": "add", "values": ["爽世"]}],
    )
    assert receipt.success and canvas.result("member").persons == ["爽世"]


def test_understanding_collection_has_canvas_and_both_audio_tools():
    tools = build_understanding_tools(
        _canvas("understanding"), analyze_speaker_consistency=lambda **_: {},
        transcribe_segment=lambda **_: {},
    )
    assert [tool.name for tool in tools] == [
        "get_context_canvas", "write_context_canvas", "analyze_speaker_consistency", "transcribe_segment",
    ]


@pytest.mark.parametrize("name", ["analyze_speaker_consistency", "transcribe_segment"])
def test_audio_tools_pass_absolute_ms_without_candidate_or_canvas_mutation(name):
    canvas = _canvas("understanding")
    before = canvas.snapshot()
    calls = []
    payload = {"evidence_ref": "issued-evidence", "start_ms": 61_000, "end_ms": 62_000}

    def host(**values):
        calls.append(("host", values))
        return payload

    tools = build_understanding_tools(
        canvas, **{name: host}, check_control=lambda: calls.append("check"),
    )
    tool = next(tool for tool in tools if tool.name == name)
    assert set(tool.args_schema.model_fields) == {"start_ms", "end_ms"}
    result = tool.execute(start_ms=61_000, end_ms=62_000)
    assert result.success and result.payload is payload
    assert calls == ["check", ("host", {"start_ms": 61_000, "end_ms": 62_000}), "check"]
    assert canvas.snapshot() == before


@pytest.mark.parametrize("schema", [AnalyzeSpeakerConsistencyArguments, TranscribeSegmentArguments])
@pytest.mark.parametrize("values", [
    {"start_ms": -1, "end_ms": 1000}, {"start_ms": 0, "end_ms": 0},
    {"start_ms": 1000, "end_ms": 1000}, {"start_ms": 2000, "end_ms": 1000},
    {"start_ms": 1.0, "end_ms": 1000}, {"start_ms": "1", "end_ms": 1000},
    {"start_ms": True, "end_ms": 1000}, {"start_ms": 0, "end_ms": 1000.0},
    {"start_ms": 0, "end_ms": 1000, "candidate_ref": "not-a-required-contract"},
    {"start_ms": 0, "end_ms": 1000, "path": "/arbitrary/source.wav"},
])
def test_audio_range_schema_requires_strict_increasing_integer_ms(schema, values):
    with pytest.raises(ValidationError):
        schema.model_validate(values)


@pytest.mark.parametrize("name", ["analyze_speaker_consistency", "transcribe_segment"])
def test_invalid_model_tool_arguments_return_feedback_without_host_call(name):
    calls = []
    tools = build_understanding_tools(
        _canvas("understanding"), **{name: lambda **values: calls.append(values) or {}},
    )
    tool = next(tool for tool in tools if tool.name == name)
    result = json.loads(tool.invoke({"start_ms": "61000", "end_ms": 62000}))
    assert result["error"]["code"] == "INVALID_ARGUMENTS" and not calls


@pytest.mark.parametrize("name", ["analyze_speaker_consistency", "transcribe_segment"])
@pytest.mark.parametrize("cancel_at", [1, 2])
def test_audio_tools_reject_cancellation_before_or_after_host_execution(name, cancel_at):
    checks, calls = [], []

    def check():
        checks.append(True)
        if len(checks) == cancel_at:
            raise RuntimeError("execution cancelled")

    tools = build_understanding_tools(
        _canvas("understanding"), **{name: lambda **values: calls.append(values) or {}},
        check_control=check,
    )
    tool = next(tool for tool in tools if tool.name == name)
    with pytest.raises(RuntimeError, match="execution cancelled"):
        tool.execute(start_ms=61_000, end_ms=62_000)
    assert len(calls) == cancel_at - 1


@pytest.mark.parametrize("name", ["analyze_speaker_consistency", "transcribe_segment"])
def test_host_scope_feedback_remains_recoverable(name):
    def reject(**_):
        raise AgentToolFeedbackError("OUTSIDE_CHUNK", "Outside bound audio.", "Use this Chunk's range.")

    tools = build_understanding_tools(_canvas("understanding"), **{name: reject})
    tool = next(tool for tool in tools if tool.name == name)
    result = tool.execute(start_ms=0, end_ms=1000)
    assert not result.success
    assert result.payload["error"]["code"] == "OUTSIDE_CHUNK"
    assert result.payload["error"]["retryable"] is True


def test_refine_collection_preserves_existing_finish_and_bound_frame_tool():
    frame_tool = build_inspect_video_frames_tool(
        source_path=Path("/host/bound-video.mp4"), read_bounds=(60_000, 80_000), max_height=360,
        timeout_seconds=10, check_control=lambda: None, trace_context={"video_id": "bound-video"},
    )
    calls = []
    receipt = JsonToolResult(success=True, payload={"ready": True})

    def finish(*, complete):
        calls.append(complete)
        return receipt

    tools = build_refine_tools(
        _canvas("refine"), compare_speaker=lambda **values: values,
        finish_voiceprint_review=finish, inspect_video_frames=frame_tool,
    )
    assert [tool.name for tool in tools] == [
        "get_context_canvas", "write_context_canvas", "finish_voiceprint_review", "compare_speaker",
        "inspect_video_frames",
    ]
    assert tools[-1] is frame_tool
    assert tools[2].args_schema is FinishVoiceprintReview
    assert tools[2].execute(complete=True) is receipt and calls == [True]
    assert json.loads(tools[2].invoke({"complete": False}))["error"]["code"] == "INVALID_ARGUMENTS"
    assert calls == [True]


def test_prefix_canvas_calls_keep_existing_tool_counter_and_cancel_checks():
    calls = []
    tools = build_prefix_tools(
        _canvas("prefix"), check_control=lambda: calls.append("check"), on_call=calls.append,
    )
    assert tools[0].execute(collection="events").success
    assert calls == ["check", "get_context_canvas", "check"]


def test_voiceprint_collection_preserves_read_only_canvas_and_preparation_tools():
    canvas = ContextCanvas(scope="voiceprint:test")
    tools = build_voiceprint_tools(
        canvas, get_speaker_list=lambda: {}, register_voiceprint=lambda **_: {},
        unregister_voiceprint=lambda **_: {}, finish_voiceprint_preparation=lambda **_: {},
    )
    assert [tool.name for tool in tools] == [
        "get_context_canvas", "get_speaker_list", "register_voiceprint", "unregister_voiceprint",
        "finish_voiceprint_preparation",
    ]
