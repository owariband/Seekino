from __future__ import annotations

from collections.abc import Callable

from engine.rag.contracts import VideoChunkSearchResult
from engine.rag.retrieval.contracts import EventSearchResult

from ...common.state import ExecutionControl, RunInputs
from agent.utils.tool import AgentTool
from ..state import RetrievalCall, RetrievalChannelContract
from ..frame_evidence import FrameInspectionSession, FrameReader
from ..video_search_canvas import VideoSearchContextCanvas
from .canvas import build_video_search_canvas_tools
from .frames import build_inspect_video_frames_tool
from .retrieval import build_retrieve_tool
from .event_retrieval import build_search_video_events_tool
from .shared import _ToolCallBudget, DraftReadAudit

def build_query_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    canvas: VideoSearchContextCanvas,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
    retrieval_channel: RetrievalChannelContract = "transcript",
    frame_reader: FrameReader | None = None,
    frame_session: FrameInspectionSession | None = None,
    frame_max_height: int | None = None,
    supports_vision_input: bool = False,
    event_retrieve: Callable[..., EventSearchResult] | None = None,
    budget: _ToolCallBudget | None = None,
) -> list[AgentTool]:
    if budget is None and maximum_tool_calls is not None:
        budget = _ToolCallBudget(maximum_tool_calls, "Query")
    tools = [
        build_retrieve_tool(
            search,
            canvas=canvas,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            retrieval_channel=retrieval_channel,
            budget=budget,
        ),
        *build_video_search_canvas_tools(
            canvas=canvas, role="query", control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            maximum_read_chars=maximum_read_chars, budget=budget,
        ),
    ]
    if event_retrieve is not None:
        tools.append(build_search_video_events_tool(
            event_retrieve, canvas=canvas, inputs=inputs, control=control, calls=calls,
            role="query", maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items, budget=budget,
        ))
    if supports_vision_input and frame_reader is not None and frame_session is not None:
        tools.append(build_inspect_video_frames_tool(
            reader=frame_reader, session=frame_session, canvas=canvas, inputs=inputs,
            control=control, role="Query", max_height=frame_max_height,
            maximum_result_bytes=maximum_result_bytes, budget=budget,
        ))
    return tools


def build_supervisor_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    canvas: VideoSearchContextCanvas,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    maximum_top_k: int,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
    audit: DraftReadAudit | None = None,
    budget: _ToolCallBudget | None = None,
    retrieval_channel: RetrievalChannelContract = "transcript",
    frame_reader: FrameReader | None = None,
    frame_session: FrameInspectionSession | None = None,
    frame_max_height: int | None = None,
    supports_vision_input: bool = False,
    event_retrieve: Callable[..., EventSearchResult] | None = None,
) -> list[AgentTool]:
    if budget is None and maximum_tool_calls is not None:
        budget = _ToolCallBudget(maximum_tool_calls, "Supervisor")
    tools = [
        build_retrieve_tool(
            search,
            canvas=canvas,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            retrieval_channel=retrieval_channel,
            budget=budget, role="supervisor",
        ),
        *build_video_search_canvas_tools(
            canvas=canvas, role="supervisor", control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            maximum_read_chars=maximum_read_chars, budget=budget, audit=audit,
        ),
    ]
    if event_retrieve is not None:
        tools.append(build_search_video_events_tool(
            event_retrieve, canvas=canvas, inputs=inputs, control=control, calls=calls,
            role="supervisor", maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items, budget=budget,
        ))
    if supports_vision_input and frame_reader is not None and frame_session is not None:
        tools.append(build_inspect_video_frames_tool(
            reader=frame_reader, session=frame_session, canvas=canvas, inputs=inputs,
            control=control, role="Supervisor", max_height=frame_max_height,
            maximum_result_bytes=maximum_result_bytes, budget=budget,
        ))
    return tools
