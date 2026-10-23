from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from engine.rag.contracts import VideoChunkSearchResult
from engine.rag.retrieval.contracts import EventSearchResult

from ...common.state import ExecutionControl, RunInputs
from agent.utils.tool import AgentTool
from ..state import RetrievalCall, RetrievalChannelContract
from ..frame_evidence import FrameInspectionSession, FrameReader
from .frames import build_inspect_video_frames_tool
from .retrieval import build_retrieve_tool, build_read_retrieval_evidence_tool
from .event_retrieval import build_search_video_events_tool
from .shared import _ToolCallBudget


def build_retrieval_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    calls: list[RetrievalCall],
    role: Literal["query", "supervisor"],
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
    role_label = "Query" if role == "query" else "Supervisor"
    if budget is None and maximum_tool_calls is not None:
        budget = _ToolCallBudget(maximum_tool_calls, role_label)
    tools = [
        build_retrieve_tool(
            search,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            retrieval_channel=retrieval_channel,
            budget=budget, role=role,
        ),
        build_read_retrieval_evidence_tool(
            calls=calls, control=control, maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars, budget=budget,
        ),
    ]
    if event_retrieve is not None:
        tools.append(build_search_video_events_tool(
            event_retrieve, inputs=inputs, control=control, calls=calls,
            role=role, maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items, budget=budget,
        ))
    if supports_vision_input and frame_reader is not None and frame_session is not None:
        tools.append(build_inspect_video_frames_tool(
            reader=frame_reader, session=frame_session, calls=calls, inputs=inputs,
            control=control, role=role_label, max_height=frame_max_height,
            maximum_result_bytes=maximum_result_bytes, budget=budget,
        ))
    return tools
