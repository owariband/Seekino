from __future__ import annotations

from collections.abc import Callable

from engine.rag.contracts import VideoChunkSearchResult

from ...common.state import ExecutionControl, RunInputs
from agent.utils.tool import AgentTool
from ..state import RetrievalCall, RetrievalChannelContract
from ..video_search_canvas import VideoSearchContextCanvas
from .canvas import build_video_search_canvas_tools
from .retrieval import build_retrieve_tool
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
) -> list[AgentTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Query")
        if maximum_tool_calls is not None else None
    )
    return [
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


def build_evaluator_tools(
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
    retrieval_channel: RetrievalChannelContract = "transcript",
) -> list[AgentTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Evaluator")
        if maximum_tool_calls is not None else None
    )
    return [
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
            canvas=canvas, role="evaluator", control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            maximum_read_chars=maximum_read_chars, budget=budget, audit=audit,
        ),
    ]


def build_chat_tools(
    *,
    canvas: VideoSearchContextCanvas,
    control: ExecutionControl,
    maximum_tool_calls: int | None,
    maximum_result_bytes: int,
    maximum_catalog_items: int,
    maximum_read_chars: int = 16_384,
) -> list[AgentTool]:
    budget = (
        _ToolCallBudget(maximum_tool_calls, "Chat")
        if maximum_tool_calls is not None else None
    )
    return build_video_search_canvas_tools(
        canvas=canvas, role="chat", control=control,
        maximum_result_bytes=maximum_result_bytes,
        maximum_catalog_items=maximum_catalog_items,
        maximum_read_chars=maximum_read_chars, budget=budget,
    )
