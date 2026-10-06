from __future__ import annotations

from collections.abc import Callable

from engine.rag.contracts import VideoChunkSearchResult

from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool
from ..state import RetrievalCall, RetrievalChannelContract
from ..workspace import AgentWorkspace
from .draft import (
    build_get_draft_catelog_tool, build_read_draft_item_tool,
    build_remove_draft_item_tool, build_update_draft_item_tool,
)
from .retrieval import build_read_trieve_item_tool, build_retrieve_tool
from .shared import _ToolCallBudget, DraftReadAudit
from .video_item import build_remove_video_item_tool, build_update_video_item_tool

def build_query_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
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
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            retrieval_channel=retrieval_channel,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
        build_update_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_remove_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
    ]


def build_evaluator_tools(
    search: Callable[..., VideoChunkSearchResult],
    *,
    workspace: AgentWorkspace,
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
            workspace=workspace,
            inputs=inputs,
            control=control,
            calls=calls,
            maximum_top_k=maximum_top_k,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            retrieval_channel=retrieval_channel,
            budget=budget,
        ),
        build_read_trieve_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            budget=budget,
            audit=audit,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            budget=budget,
            audit=audit,
        ),
    ]


def build_chat_tools(
    *,
    workspace: AgentWorkspace,
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
    return [
        build_remove_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_update_video_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            budget=budget,
        ),
        build_get_draft_catelog_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_catalog_items=maximum_catalog_items,
            approved=True,
            budget=budget,
        ),
        build_read_draft_item_tool(
            workspace=workspace,
            control=control,
            maximum_result_bytes=maximum_result_bytes,
            maximum_read_chars=maximum_read_chars,
            approved=True,
            budget=budget,
        ),
    ]
