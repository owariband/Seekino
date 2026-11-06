from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ...common.errors import (
    AgentModelInvocationError, AgentToolFeedbackError, AgentToolOutputError,
)
from ...common.state import ExecutionControl, RunInputs
from ...common.tool import AgentTool, build_agent_tool
from ...common.tool_output import bounded_json
from ...common.tool_result import JsonToolResult, ToolResult
from .shared import _ToolCallBudget, _start_tool_call


_TOOL_NAME = "get_series_overview"


class GetSeriesOverviewArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ] | None = None


def build_get_series_overview_tool(
    series_overview: Callable[..., dict | None],
    *,
    inputs: RunInputs,
    control: ExecutionControl,
    maximum_result_bytes: int,
    budget: _ToolCallBudget | None = None,
) -> AgentTool:
    def get_series_overview(series: str | None = None) -> ToolResult:
        _start_tool_call(control, budget, _TOOL_NAME)
        try:
            found = series_overview(
                series, tenant_id=inputs["tenant_id"],
                series_ids=list(inputs["series_ids"]),
            )
        except AgentModelInvocationError:
            raise
        except Exception as exc:
            control.check()
            raise AgentToolFeedbackError(
                "SERIES_OVERVIEW_FAILED", "The series overview could not be read.",
                "Fall back to get_video_outline or search_video_events for these series.",
            ) from exc
        control.check()
        if not isinstance(found, dict):
            raise AgentModelInvocationError("Series overview returned an invalid result")
        error = found.get("error")
        if isinstance(error, dict):
            raise AgentToolFeedbackError(
                str(error.get("code") or "SERIES_OVERVIEW_FAILED"),
                str(error.get("message") or "The requested series could not be resolved."),
                str(error.get("hint") or "Address series by name."),
            )
        payload = found.get("series")
        if not isinstance(payload, list):
            raise AgentModelInvocationError("Series overview returned an invalid result")
        try:
            bounded_json(payload, maximum_result_bytes)
        except AgentToolOutputError as exc:
            raise AgentToolFeedbackError(
                "SERIES_OVERVIEW_TOO_LARGE", "The series overview exceeds the result size limit.",
                "Name one series to read its overview alone.",
            ) from exc
        return JsonToolResult(success=True, payload=payload)

    return build_agent_tool(
        func=get_series_overview, name=_TOOL_NAME,
        description=(
            "Read a per-video overview for one or all in-scope series: each video's episode, "
            "title, description, whether it has a chapter outline (hasOutline), and whether it "
            "has been summarized (summarized). Use it first for whole-series or course questions "
            "like \"what does this series cover\"; organize the answer by series and episode, then "
            "read specific episodes with get_video_outline when more detail is needed. Titles and "
            "descriptions come from video summaries, not from series_context background."
        ),
        args_schema=GetSeriesOverviewArguments, maximum_result_bytes=maximum_result_bytes,
    )
