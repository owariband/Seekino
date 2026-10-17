from __future__ import annotations

import json
import logging
from threading import Event
from time import monotonic

import pytest

from agent.chat.common.errors import AgentModelInvocationError, AgentToolOutputError
from agent.chat.common.state import ExecutionControl
from agent.chat.common.tool import attach_agent_tool_logging
from agent.chat.video_search.frame_evidence import (
    FrameInspection, FrameInspectionSession, FrameReadError,
)
from agent.chat.video_search.tools.frames import build_inspect_video_frames_tool
from agent.chat.video_search.tools.shared import _ToolCallBudget
from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas
from engine.rag.contracts import AgentReply, VideoChunkSearchResult
from gateway.media import ToolImage, ToolImageArtifact
from tests.agent.chat.test_runtime import (
    Chat, Evaluator, Query, Retrieval, _inputs as _runtime_inputs,
)
from tests.agent.chat.test_tools import _hit, _inputs


class FrameReader:
    def __init__(self):
        self.calls = []

    def __call__(self, request, cached=None):
        self.calls.append((request, cached))
        artifact = cached.artifact if cached else ToolImageArtifact(tuple(
            ToolImage("data:image/png;base64,dGVzdA==", 4, f"frame {index}")
            for index in range(request.frame_count)
        ))
        return FrameInspection({
            "inspection_id": request.inspection_id,
            "video_chunk_id": request.evidence.video_chunk_id,
            "role": request.role,
            "start_ms": request.start_ms,
            "end_ms": request.end_ms,
            "frame_count": request.frame_count,
            "frames": [{
                "requested_timestamp_ms": request.start_ms + index,
                "artifact_uri": f"artifact://frames/{index}.png",
                "sha256": str(index) * 64,
            } for index in range(request.frame_count)],
            "cache_hit": cached is not None,
            "images_supplied": True,
            "manifest_uri": f"artifact://inspections/{request.inspection_id}.json",
            "manifest_sha256": "a" * 64,
        }, artifact)


def _canvas():
    hit = _hit("chunk-1")
    hit.evidence.start_seconds, hit.evidence.end_seconds = 10_000, 20_000
    canvas = VideoSearchContextCanvas("workspace", ("series",))
    canvas.record_retrieval(VideoChunkSearchResult(
        query="cup", series_ids=["series"],
        agent_reply=AgentReply(message="retrieved", result_count=1), hits=[hit],
    ))
    return canvas


def _tool(
    reader, *, session=None, canvas=None, control=None, inputs=None,
    role="Query", maximum_result_bytes=16_384, **kwargs,
):
    return build_inspect_video_frames_tool(
        reader=reader,
        session=session if session is not None else FrameInspectionSession(10),
        canvas=canvas if canvas is not None else _canvas(),
        inputs=_inputs() if inputs is None else inputs,
        control=control if control is not None else ExecutionControl(monotonic() + 60),
        role=role, max_height=480, maximum_result_bytes=maximum_result_bytes, **kwargs,
    )


@pytest.mark.parametrize("range_args,expected", [
    ({}, (10_000, 20_000)),
    ({"start_ms": 11_250, "end_ms": 13_750}, (11_250, 13_750)),
    ({"start_ms": 11_000}, (11_000, 20_000)),
    ({"end_ms": 12_000}, (10_000, 12_000)),
])
def test_frames_use_canonical_absolute_milliseconds_and_return_real_images(range_args, expected):
    reader = FrameReader()
    result = _tool(reader).invoke({
        "type": "tool_call", "name": "inspect_video_frames", "id": "frames-1",
        "args": {"video_chunk_id": "chunk-1", **range_args},
    })

    assert result.status == "success"
    payload = json.loads(result.content)
    request, cached = reader.calls[0]
    assert (request.start_ms, request.end_ms) == expected
    assert request.evidence.original_source_uri == "episode-1.mp4"
    assert request.frame_count == 3
    assert request.max_height == 480
    assert request.conversation_id == "conversation"
    assert request.message_id == "answer"
    assert 0 < request.timeout_seconds <= 60
    assert cached is None
    assert payload["images_supplied"] is True
    assert len(result.artifact.images) == 3
    assert result.artifact.openai_content()[1]["type"] == "image_url"


@pytest.mark.parametrize("arguments", [
    {"start_ms": 0, "end_ms": 1_000},
    {"start_ms": 9_999, "end_ms": 12_000},
    {"start_ms": 19_000, "end_ms": 20_001},
    {"start_ms": 12_000, "end_ms": 12_000},
    {"start_ms": 13_000, "end_ms": 12_000},
    {"start_ms": True, "end_ms": 12_000},
    {"start_ms": "11000", "end_ms": 12_000},
    {"start_ms": 11_000.5, "end_ms": 12_000},
    {"frame_count": 0}, {"frame_count": 5}, {"frame_count": True},
    {"frame_count": "3"}, {"frame_count": 3.5},
])
def test_invalid_frame_requests_never_call_reader(arguments):
    reader = FrameReader()
    result = json.loads(_tool(reader).invoke({"video_chunk_id": "chunk-1", **arguments}))
    assert result["error"]["code"]
    assert reader.calls == []


def test_frames_reject_unknown_candidates_and_changed_input_scope():
    reader = FrameReader()
    result = json.loads(_tool(reader).invoke({"video_chunk_id": "not-retrieved"}))
    assert result["error"]["code"] == "UNKNOWN_ITEM"
    inputs = _inputs()
    inputs["tenant_id"] = "other-workspace"
    result = json.loads(_tool(reader, inputs=inputs).invoke({"video_chunk_id": "chunk-1"}))
    assert result["error"]["code"] == "FRAME_SCOPE_MISMATCH"
    assert reader.calls == []


def test_shared_cache_still_delivers_images_and_separate_role_receipts():
    reader, session, canvas = FrameReader(), FrameInspectionSession(10), _canvas()
    query = _tool(reader, session=session, canvas=canvas, role="Query")
    evaluator = _tool(reader, session=session, canvas=canvas, role="Evaluator")
    arguments = {"video_chunk_id": "chunk-1", "frame_count": 4}
    first, second = query.execute(**arguments), evaluator.execute(**arguments)

    assert len(reader.calls) == 2
    assert reader.calls[0][1] is None
    assert reader.calls[1][1] is not None
    assert len(first.artifact.images) == len(second.artifact.images) == 4
    assert first.payload["inspection_id"] != second.payload["inspection_id"]
    assert first.payload["cache_hit"] is False
    assert second.payload["cache_hit"] is True
    receipts = session.summary()
    assert [item["role"] for item in receipts] == ["Query", "Evaluator"]
    assert [item["inspection_id"] for item in receipts] == [
        first.payload["inspection_id"], second.payload["inspection_id"],
    ]
    assert [canvas.get("frame_inspections", item["inspection_id"]) for item in receipts] == receipts
    assert len(session.cache) == 1

    independent = _tool(reader, session=FrameInspectionSession(10))
    independent.execute(**arguments)
    assert reader.calls[-1][1] is None


def test_frames_honor_tool_budget_session_limit_and_cancellation_before_reading():
    reader, budget = FrameReader(), _ToolCallBudget(1, "Query")
    tool = _tool(reader, budget=budget)
    tool.execute(video_chunk_id="chunk-1")
    with pytest.raises(AgentModelInvocationError, match="Tool-call budget"):
        tool.execute(video_chunk_id="chunk-1")
    assert len(reader.calls) == 1

    limited = _tool(reader, session=FrameInspectionSession(1))
    limited.execute(video_chunk_id="chunk-1")
    result = limited.execute(video_chunk_id="chunk-1", frame_count=1)
    assert result.success is False
    assert len(reader.calls) == 2

    cancellation = Event()
    cancellation.set()
    cancelled = _tool(reader, control=ExecutionControl(None, cancellation))
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        cancelled.execute(video_chunk_id="chunk-1")
    assert len(reader.calls) == 2


def test_failed_frame_read_consumes_inspection_budget_without_caching_images():
    calls, session, canvas = [], FrameInspectionSession(1), _canvas()

    def failing_reader(request, cached=None):
        calls.append(request)
        raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Source unavailable", retryable=True)

    tool = _tool(failing_reader, session=session, canvas=canvas)
    result = tool.execute(video_chunk_id="chunk-1")
    assert result.success is False and result.artifact is None
    assert result.payload["error"]["retryable"] is True
    assert result.payload["inspection_id"] == calls[0].inspection_id
    assert session.cache == {} and session.summary() == []
    assert canvas.items("frame_inspections") == []
    exhausted = tool.execute(video_chunk_id="chunk-1")
    assert exhausted.payload["error"]["code"] == "FRAME_INSPECTION_LIMIT"
    assert len(calls) == 1


def test_oversized_frame_result_does_not_record_success_or_cache_images():
    reader, session, canvas = FrameReader(), FrameInspectionSession(10), _canvas()

    def oversized_reader(request, cached=None):
        inspection = reader(request, cached)
        return FrameInspection({**inspection.payload, "extra": "x" * 2_000}, inspection.artifact)

    tool = _tool(oversized_reader, session=session, canvas=canvas, maximum_result_bytes=512)
    with pytest.raises(AgentToolOutputError):
        tool.invoke({
            "type": "tool_call", "name": "inspect_video_frames", "id": "oversized",
            "args": {"video_chunk_id": "chunk-1"},
        })
    assert len(reader.calls) == session.used_inspections == 1
    assert session.cache == {}
    assert session.summary() == []
    assert canvas.items("frame_inspections") == []


def test_failed_cached_frame_read_allows_fresh_retry():
    reader, session = FrameReader(), FrameInspectionSession(10)

    def reader_with_bad_cache(request, cached=None):
        if cached is not None:
            raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Cached image missing", retryable=True)
        return reader(request)

    tool = _tool(reader_with_bad_cache, session=session)
    assert tool.execute(video_chunk_id="chunk-1").success is True
    assert len(session.cache) == 1
    failed = tool.execute(video_chunk_id="chunk-1")
    assert failed.success is False and failed.artifact is None
    assert failed.payload["error"]["retryable"] is True
    assert session.cache == {}
    retried = tool.execute(video_chunk_id="chunk-1")
    assert retried.success is True and len(retried.artifact.images) == 3
    assert len(reader.calls) == 2
    assert len(session.summary()) == 2
    assert len(session.cache) == 1


def test_frame_tool_trace_retains_request_and_manifest_without_image_payload(caplog):
    caplog.set_level(logging.INFO, logger="agent.chat.common.tool")
    tool = _tool(FrameReader())
    attach_agent_tool_logging([tool], role="Query", inputs=_inputs())
    result = json.loads(tool.invoke({
        "video_chunk_id": "chunk-1", "start_ms": 11_000,
        "end_ms": 13_000, "frame_count": 2,
    }))

    assert '"start_ms":11000' in caplog.text
    assert '"end_ms":13000' in caplog.text
    assert '"frame_count":2' in caplog.text
    assert result["inspection_id"] in caplog.text
    assert result["manifest_uri"] in caplog.text
    assert '"images_supplied":true' in caplog.text
    assert "tool_run_id=" in caplog.text
    assert "base64" not in caplog.text
    assert "dGVzdA==" not in caplog.text


def _inspect_model_frames(tools, *, vision, role):
    tools_by_name = {tool.name: tool for tool in tools}
    if not vision:
        assert "inspect_video_frames" not in tools_by_name
        return None
    message = tools_by_name["inspect_video_frames"].invoke({
        "type": "tool_call", "name": "inspect_video_frames", "id": f"frames-{role}",
        "args": {"video_chunk_id": "chunk-1"},
    })
    assert message.status == "success"
    assert len(message.artifact.images) == 3
    return json.loads(message.content)


class InspectingQuery(Query):
    supports_vision_input = True

    def run_structured(self, prompt, **kwargs):
        result = super().run_structured(prompt, **kwargs)
        self.inspection = _inspect_model_frames(
            kwargs["tools"], vision=self.supports_vision_input, role="query",
        )
        return result


class InspectingEvaluator(Evaluator):
    supports_vision_input = True

    def run_structured(self, prompt, **kwargs):
        result = super().run_structured(prompt, **kwargs)
        self.inspection = _inspect_model_frames(
            kwargs["tools"], vision=self.supports_vision_input, role="evaluator",
        )
        return result


@pytest.mark.parametrize("query_vision,evaluator_vision", [(True, True), (False, True), (True, False)])
def test_runtime_gates_frames_per_model_and_resets_session_between_kicks(
    query_vision, evaluator_vision, monkeypatch,
):
    from agent.chat.video_search.runtime import AgentRuntime

    # These models use only single-entry Canvas reads; batch tokenization is unrelated.
    monkeypatch.setattr("agent.utils.context_canvas_tools.tiktoken.get_encoding", lambda _name: None)
    reader, query, evaluator = FrameReader(), InspectingQuery(), InspectingEvaluator()
    query.supports_vision_input, evaluator.supports_vision_input = query_vision, evaluator_vision
    runtime = AgentRuntime(
        query, evaluator, Chat(), Retrieval().search,
        retrieval_channel="lexical", frame_reader=reader,
        frame_max_height=480, maximum_frame_inspections=10,
    )
    receipts = []
    runtime.kick(_runtime_inputs(), on_handoff=lambda canvas, _turn: receipts.extend(
        receipt for _reference, receipt in canvas.items("frame_inspections")
    ))
    first_call_count = len(reader.calls)
    assert first_call_count == int(query_vision) + int(evaluator_vision)
    assert reader.calls[0][1] is None
    if query_vision and evaluator_vision:
        assert reader.calls[1][1] is not None
        assert receipts[0]["inspection_id"] == query.inspection["inspection_id"]
        assert receipts[0]["role"] == "Query"
        assert "data:image" not in json.dumps(receipts)
    first_ids = {request.inspection_id for request, _cached in reader.calls}

    evaluator.approve_at = 2
    runtime.kick(_runtime_inputs())
    assert len(reader.calls) == first_call_count * 2
    assert reader.calls[first_call_count][1] is None
    assert first_ids.isdisjoint(
        request.inspection_id for request, _cached in reader.calls[first_call_count:]
    )


@pytest.mark.parametrize("limits_enabled,max_loop_turns,limited", [
    (False, None, False),
    (True, None, True),
    (True, 0, False),
    (True, 100, False),
])
def test_runtime_frame_inspections_follow_effective_count_limits(
    limits_enabled, max_loop_turns, limited, monkeypatch,
):
    from agent.chat.video_search.runtime import AgentRuntime

    monkeypatch.setattr("agent.utils.context_canvas_tools.tiktoken.get_encoding", lambda _name: None)

    class RepeatedFrameQuery(Query):
        supports_vision_input = True

        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            tool = next(tool for tool in kwargs["tools"] if tool.name == "inspect_video_frames")
            self.inspections = [tool.invoke({
                "type": "tool_call", "name": tool.name, "id": f"query-frames-{index}",
                "args": {"video_chunk_id": "chunk-1", "frame_count": 1 if index == 12 else 3},
            }) for index in range(13)]
            return result

    class InspectingLimitEvaluator(Evaluator):
        supports_vision_input = True

        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            tool = next(tool for tool in kwargs["tools"] if tool.name == "inspect_video_frames")
            self.inspection = tool.invoke({
                "type": "tool_call", "name": tool.name, "id": "evaluator-frames",
                "args": {"video_chunk_id": "chunk-1"},
            })
            return result

    reader, query, evaluator = FrameReader(), RepeatedFrameQuery(), InspectingLimitEvaluator()
    runtime = AgentRuntime(
        query, evaluator, Chat(), Retrieval().search,
        retrieval_channel="lexical", frame_reader=reader,
        frame_max_height=480, maximum_frame_inspections=12, limits_enabled=limits_enabled,
    )
    receipts = []
    runtime.kick(_runtime_inputs(), max_loop_turns=max_loop_turns,
                 on_handoff=lambda canvas, _turn: receipts.extend(
                     receipt for _reference, receipt in canvas.items("frame_inspections")
                 ))

    first_call_count = 12 if limited else 14
    assert len(reader.calls) == first_call_count
    assert all(message.status == "success" for message in query.inspections[:12])
    assert all(len(message.artifact.images) == 3 for message in query.inspections[:12])
    assert reader.calls[0][1] is None
    assert all(cached is not None for _request, cached in reader.calls[1:12])
    if limited:
        for message in (query.inspections[-1], evaluator.inspection):
            assert message.status == "error"
            assert json.loads(message.content)["error"]["code"] == "FRAME_INSPECTION_LIMIT"
            assert message.artifact is None
    else:
        assert query.inspections[-1].status == evaluator.inspection.status == "success"
        assert len(query.inspections[-1].artifact.images) == 1
        assert reader.calls[12][1] is None
        assert reader.calls[13][1] is not None
        assert evaluator.inspection.artifact.images == query.inspections[0].artifact.images
        assert len(receipts) == 14
        assert [receipt["role"] for receipt in receipts] == ["Query"] * 13 + ["Evaluator"]

    first_ids = {request.inspection_id for request, _cached in reader.calls}
    evaluator.approve_at = 2
    runtime.kick(_runtime_inputs(), max_loop_turns=max_loop_turns)
    assert len(reader.calls) == first_call_count * 2
    assert reader.calls[first_call_count][1] is None
    assert first_ids.isdisjoint(
        request.inspection_id for request, _cached in reader.calls[first_call_count:]
    )
