from __future__ import annotations

import base64
from dataclasses import replace
from hashlib import sha256

import pytest

from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult
from agent.utils.video_frames import InspectVideoFramesArguments
from agent.video_summarize import SummaryChunk, VideoSummarizeInput, VideoSummarizeLimits
from agent.video_summarize.state import SummaryCanvas
from agent.video_summarize.tools import build_tools
from gateway.media import ToolImage, ToolImageArtifact


def _inputs(*, count=2):
    return VideoSummarizeInput(
        video_id="video-1", duration_ms=100_000, episode_order=99, background={"series": "Test Series"},
        chunks=tuple(SummaryChunk(
            chunk_parse_task_id=f"chunk-{index}", sequence_no=index, version=2,
            content={"timeline": [{"event_type": "ON_SCREEN_TEXT", "text": "第十二話 出発"},
                                  {"event_type": "SCENE", "description": f"The friends reach station {index}."}]},
        ) for index in range(count)),
    )


def _limits(**changes):
    return replace(VideoSummarizeLimits(
        maximum_result_bytes=32_000,
        maximum_read_chars=12_000, maximum_catalog_items=100,
    ), **changes)


def _frame_tool(*, success=True, bad_hash=False):
    data = b"original source image bytes"
    image = ToolImage("data:image/png;base64," + base64.b64encode(data).decode(), len(data), "source frame")

    def inspect(**_values):
        return JsonToolResult(success=success, payload={
            "inspection_id": "inspection-1", "video_id": "video-1", "images_supplied": True,
            "frame_count": 1, "frames": [{"requested_timestamp_ms": 1000,
                "sha256": "wrong" if bad_hash else sha256(data).hexdigest(), "size_bytes": len(data)}],
        }, artifact=ToolImageArtifact((image,)) if success else None)

    return build_agent_tool(
        func=inspect, name="inspect_video_frames", description="Inspect the bound source.",
        args_schema=InspectVideoFramesArguments, maximum_result_bytes=32_000,
    )


def _setup(*, inputs=None, limits=None, frame_tool=None, check_control=lambda: None):
    inputs = inputs or _inputs()
    canvas = SummaryCanvas(inputs)
    tools = build_tools(
        canvas, inputs=inputs, limits=limits or _limits(),
        inspect_video_frames=frame_tool or _frame_tool(), check_control=check_control,
    )
    return canvas, {tool.name: tool for tool in tools}


def _read_all(tools, inputs=None):
    inputs = inputs or _inputs()
    return tools["get_context_canvas"].execute(
        collection="chunks", references=[item.chunk_parse_task_id for item in inputs.chunks], section="content",
    )


def _summary(**changes):
    return {
        "title": "出発", "description": "The friends reach a station.", "episode_order": 12,
        "evidence": [
            {"field": "title", "chunk_parse_task_id": "chunk-0", "quote": "第十二話 出発"},
            {"field": "episode_order", "chunk_parse_task_id": "chunk-0", "quote": "第十二話 出発"},
            {"field": "description", "chunk_parse_task_id": "chunk-1", "quote": "The friends reach station 1."},
        ], **changes,
    }


def test_three_tools_submit_grounded_summary_and_return_original_cover():
    canvas, tools = _setup()
    assert set(tools) == {"get_context_canvas", "inspect_video_frames", "submit_video_summary"}
    assert _read_all(tools).success
    inspected = tools["inspect_video_frames"].execute(start_ms=0, end_ms=2000, frame_count=1)
    summary = _summary(cover={"inspection_id": "inspection-1", "frame_index": 0})
    summary["evidence"].append({"field": "cover", "inspection_id": "inspection-1", "frame_index": 0,
                                "quote": "The friends stand at the station."})
    assert tools["submit_video_summary"].execute(**summary).success
    assert canvas.result.episode_order == 12
    assert canvas.result.title == "出発"
    assert canvas.result.cover_image is inspected.artifact.images[0]
    assert canvas.result.frame_receipts == (inspected.payload,)
    assert tools["submit_video_summary"].execute(**summary).payload["replayed"] is True
    assert not tools["submit_video_summary"].execute(**{**summary, "description": "replacement"}).success


def test_catalog_and_overview_are_not_reading_and_sources_are_frozen():
    inputs = _inputs()
    canvas, tools = _setup(inputs=inputs)
    inputs.chunks[0].content["timeline"][0]["text"] = "mutated source"
    assert "mutated" not in canvas.sections("chunks", "chunk-0", role="summarize")["content"]
    tools["get_context_canvas"].execute(collection="chunks")
    tools["get_context_canvas"].execute(collection="chunks", reference="chunk-0", section="overview")
    response = tools["submit_video_summary"].execute(**_summary())
    assert response.payload["error"]["code"] == "INCOMPLETE_REVIEW"
    assert canvas.result is None


def test_truncated_batch_and_partial_ranges_cannot_skip_unread_chunk_content():
    inputs = _inputs()
    canvas, tools = _setup(inputs=inputs, limits=_limits(maximum_result_bytes=700))
    batch = _read_all(tools, inputs)
    assert batch.payload["truncated"] is True
    assert batch.payload["remaining_references"] == ["chunk-1"]
    assert canvas.unread_chunks() == ["chunk-1"]
    read = tools["get_context_canvas"]
    content = canvas.sections("chunks", "chunk-1", role="summarize")["content"]
    assert read.execute(collection="chunks", reference="chunk-1", char_start=0, char_end=30).success
    rejected = tools["submit_video_summary"].execute(**_summary())
    assert not rejected.success
    assert "['chunk-1']" in rejected.payload["error"]["retry_hint"]
    assert "chunk-0" not in rejected.payload["error"]["retry_hint"]
    assert read.execute(collection="chunks", reference="chunk-1", char_start=30, char_end=len(content)).success
    assert tools["submit_video_summary"].execute(**_summary()).success


@pytest.mark.parametrize("changes,code", [
    ({"title": "Invented"}, "UNSUPPORTED_TITLE"),
    ({"episode_order": 4}, "UNSUPPORTED_EPISODE"),
    ({"cover": {"inspection_id": "made-up", "frame_index": 0}}, "MISSING_EVIDENCE"),
])
def test_unsupported_metadata_never_becomes_result(changes, code):
    canvas, tools = _setup()
    _read_all(tools)
    result = tools["submit_video_summary"].execute(**_summary(**changes))
    assert result.payload["error"]["code"] == code
    assert canvas.result is None


@pytest.mark.parametrize("inspection_id,frame_index", [("made-up", 0), ("inspection-1", 1)])
def test_cover_must_reference_an_actual_inspected_frame(inspection_id, frame_index):
    canvas, tools = _setup()
    _read_all(tools)
    tools["inspect_video_frames"].execute()
    summary = _summary(cover={"inspection_id": inspection_id, "frame_index": frame_index})
    summary["evidence"].append({"field": "cover", "inspection_id": inspection_id,
                                "frame_index": frame_index, "quote": "A station."})
    result = tools["submit_video_summary"].execute(**summary)
    assert result.payload["error"]["code"] == "UNKNOWN_FRAME"
    assert canvas.result is None


def test_failed_inspection_and_mismatched_artifact_do_not_issue_frame_evidence():
    canvas, tools = _setup(frame_tool=_frame_tool(success=False))
    assert not tools["inspect_video_frames"].execute().success
    assert not canvas.inspections
    canvas, tools = _setup(frame_tool=_frame_tool(bad_hash=True))
    with pytest.raises(RuntimeError, match="does not match"):
        tools["inspect_video_frames"].execute()
    assert not canvas.inspections


@pytest.mark.parametrize("quote", ["Episode 12 出発", "#12 出発", "＃１２ 出発"])
def test_frames_support_title_and_episode_only_with_known_receipt_and_matching_quote(quote):
    canvas, tools = _setup()
    _read_all(tools)
    tools["inspect_video_frames"].execute()
    summary = _summary()
    for evidence in summary["evidence"][:2]:
        evidence.pop("chunk_parse_task_id")
        evidence.update(inspection_id="inspection-1", frame_index=0, quote=quote)
    assert tools["submit_video_summary"].execute(**summary).success
    assert canvas.result.episode_order == 12


def test_dialogue_cannot_prove_title_and_unavailable_fields_can_be_omitted():
    inputs = _inputs()
    inputs.chunks[0].content["timeline"][0]["event_type"] = "DIALOGUE"
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    assert tools["submit_video_summary"].execute(**_summary()).payload["error"]["code"] == "UNSUPPORTED_EVIDENCE"
    summary = _summary(title=None, episode_order=None)
    summary["evidence"] = summary["evidence"][2:]
    assert tools["submit_video_summary"].execute(**summary).success
    assert canvas.result.title is None and canvas.result.episode_order is None and canvas.result.cover_image is None


def _generated_summary():
    summary = _summary(title="朋友们抵达车站", title_kind="generated", episode_order=None)
    summary["evidence"] = [
        {"field": "title", "chunk_parse_task_id": "chunk-0", "quote": "The friends reach station 0."},
        summary["evidence"][2],
    ]
    return summary


def test_generated_title_accepts_grounded_content_without_claiming_an_official_title():
    inputs = _inputs()
    for chunk in inputs.chunks:
        chunk.content["timeline"] = chunk.content["timeline"][1:]
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    assert tools["submit_video_summary"].execute(**_generated_summary()).success
    assert canvas.result.title == "朋友们抵达车站"
    assert canvas.result.submission.title_kind == "generated"
    assert canvas.result.episode_order is None


@pytest.mark.parametrize("change,code", [
    ("fabricated_quote", "UNSUPPORTED_EVIDENCE"),
    ("missing_evidence", "MISSING_EVIDENCE"),
    ("invented_episode", "UNSUPPORTED_EPISODE"),
    ("preview", "PREVIEW_EVIDENCE"),
])
def test_generated_title_still_requires_current_content_and_independent_episode_evidence(change, code):
    inputs = _inputs()
    if change == "preview":
        inputs.chunks[0].content["timeline"][1]["context"] = "次回予告"
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    summary = _generated_summary()
    if change == "fabricated_quote":
        summary["evidence"][0]["quote"] = "The friends win a race."
    elif change == "missing_evidence":
        summary["evidence"] = summary["evidence"][1:]
    elif change == "invented_episode":
        summary["episode_order"] = 4
        summary["evidence"].append(_summary()["evidence"][1])
    result = tools["submit_video_summary"].execute(**summary)
    assert result.payload["error"]["code"] == code
    assert canvas.result is None


@pytest.mark.parametrize("title", [None, "", "长" * 81])
def test_generated_title_must_be_present_and_concise(title):
    canvas, tools = _setup()
    _read_all(tools)
    result = tools["submit_video_summary"].execute(**{**_generated_summary(), "title": title})
    assert result.payload["error"]["code"] == "INVALID_SUMMARY"
    assert canvas.result is None


@pytest.mark.parametrize("field,quote", [
    ("description", "invented quote"), ("episode_order", "Episode 12 Episode 13"),
])
def test_unknown_or_conflicting_chunk_quotes_are_rejected(field, quote):
    canvas, tools = _setup()
    _read_all(tools)
    summary = _summary()
    next(item for item in summary["evidence"] if item["field"] == field)["quote"] = quote
    assert not tools["submit_video_summary"].execute(**summary).success
    assert canvas.result is None


@pytest.mark.parametrize("quote", ["#12 #13", "#12.5", "＃１２．５", "#12/13", "#12–13"])
def test_hash_episode_marker_cannot_truncate_fractional_or_conflicting_numbers(quote):
    inputs = _inputs()
    inputs.chunks[0].content["timeline"][0]["text"] = quote
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    summary = _summary(title=None)
    summary["evidence"] = [item for item in summary["evidence"] if item["field"] != "title"]
    summary["evidence"][0]["quote"] = quote
    result = tools["submit_video_summary"].execute(**summary)
    assert result.payload["error"]["code"] == "UNSUPPORTED_EPISODE"
    assert canvas.result is None


def test_reading_and_frame_inspection_can_exceed_previous_call_caps():
    canvas, tools = _setup()
    for _ in range(40):
        assert _read_all(tools).success
    for _ in range(10):
        assert tools["inspect_video_frames"].execute().success
    assert tools["submit_video_summary"].execute(**_summary()).success
    assert canvas.result is not None


@pytest.mark.parametrize("name,arguments", [
    ("get_context_canvas", {"collection": "chunks"}),
    ("inspect_video_frames", {}),
    ("submit_video_summary", _summary()),
])
def test_host_control_still_stops_tools(name, arguments):
    def cancelled():
        raise TimeoutError("host cancelled")

    _, tools = _setup(check_control=cancelled)
    with pytest.raises(TimeoutError, match="host cancelled"):
        tools[name].execute(**arguments)


def test_langchain_tool_validates_schema_and_disallows_arbitrary_cover_urls():
    canvas, tools = _setup()
    _read_all(tools)
    result = tools["submit_video_summary"].invoke({**_summary(), "cover": {"url": "https://invalid/image.png"}})
    assert "INVALID_ARGUMENTS" in result
    assert canvas.result is None
    result = tools["submit_video_summary"].invoke(_summary())
    assert '"submitted":true' in result
    assert canvas.result.title == "出発"


@pytest.mark.parametrize("quote", [
    "第１２話 出発", "ＥＰＩＳＯＤＥ １２ 出発", "第十二話 出発", "#12 出発", "＃１２ 出発",
])
def test_episode_number_normalization_preserves_original_evidence(quote):
    inputs = _inputs()
    inputs.chunks[0].content["timeline"][0]["text"] = quote
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    summary = _summary()
    for evidence in summary["evidence"][:2]:
        evidence["quote"] = quote
    assert tools["submit_video_summary"].execute(**summary).success
    assert canvas.result.episode_order == 12
    assert canvas.result.evidence[1].quote == quote


@pytest.mark.parametrize("length,accepted", [(255, True), (256, False)])
def test_title_respects_video_display_name_limit(length, accepted):
    inputs = _inputs()
    title = "名" * length
    inputs.chunks[0].content["timeline"][0]["text"] = "第十二話 " + title
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    summary = _summary(title=title)
    for evidence in summary["evidence"][:2]:
        evidence["quote"] = "第十二話 " + title
    assert tools["submit_video_summary"].execute(**summary).success is accepted
    assert (canvas.result is not None) is accepted


@pytest.mark.parametrize("text,description", [
    ("次回予告 第5話 月の夜", "Next episode preview"),
    ("第5話 月の夜", "次回予告"),
    ("下集预告 第5話 月の夜", ""),
    ("第5話 月の夜", "Next episode preview"),
    ("＃5 月の夜", "次回予告"),
])
def test_trimming_preview_markers_from_event_quote_cannot_prove_episode_identity(text, description):
    inputs = _inputs()
    inputs.chunks[0].content["timeline"][0].update(text=text, description=description)
    canvas, tools = _setup(inputs=inputs)
    _read_all(tools, inputs)
    summary = _summary(title="月の夜", episode_order=5)
    for evidence in summary["evidence"][:2]:
        evidence["quote"] = "＃5 月の夜" if text.startswith("＃") else "第5話 月の夜"
    result = tools["submit_video_summary"].execute(**summary)
    assert result.payload["error"]["code"] == "PREVIEW_EVIDENCE"
    assert canvas.result is None


def test_frame_quote_identifying_a_preview_is_rejected():
    canvas, tools = _setup()
    _read_all(tools)
    tools["inspect_video_frames"].execute()
    summary = _summary()
    summary["evidence"][0] = {"field": "title", "inspection_id": "inspection-1", "frame_index": 0,
                               "quote": "次回予告 第十二話 出発"}
    result = tools["submit_video_summary"].execute(**summary)
    assert result.payload["error"]["code"] == "PREVIEW_EVIDENCE"
    assert canvas.result is None
