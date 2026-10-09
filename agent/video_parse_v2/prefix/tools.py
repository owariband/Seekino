"""Prefix uses the same bounded Canvas tools as Understanding and Refine."""

from __future__ import annotations

from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import AgentTool

from ..timelineEvent_canvas import ParseCanvasWrite, ParseContextCanvas


def build_tools(canvas: ParseContextCanvas, *, check_control=None, on_call=None) -> list[AgentTool]:
    return build_context_canvas_tools(
        canvas, role="prefix", read_collections=("members", "events", "evidence", "background", "compact"),
        write_args_schema=ParseCanvasWrite, maximum_result_bytes=32_000,
        maximum_read_chars=12_000, maximum_catalog_items=100,
        check_control=check_control, on_call=on_call,
    )
