from __future__ import annotations

from dataclasses import asdict

from fastapi import Query, Request

from ..model.dto.parseUsageDTO import UsageOverviewState, UsageTotals
from ..model.vo.parseUsageVO import UsageOverviewVO
from ..utils.time import MAX_UNIX_TIME_MS
from .context import get_services


def get_usage_overview(
    request: Request,
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
    from_start_time: int | None = Query(None, alias="fromStartTime", ge=0, le=MAX_UNIX_TIME_MS),
    to_start_time: int | None = Query(None, alias="toStartTime", ge=0, le=MAX_UNIX_TIME_MS),
    series_id: str | None = Query(None, alias="seriesId", min_length=1, max_length=64),
    run_id: str | None = Query(None, alias="runId", min_length=1, max_length=256),
) -> UsageOverviewVO:
    service = get_services(request).parse_usage
    if service is None:
        raise RuntimeError("Model usage service is unavailable")
    return project_usage_overview(service.overview(
        workspace_id=workspace_id, from_start_time=from_start_time,
        to_start_time=to_start_time, series_id=series_id, run_id=run_id,
    ))


def _usage_values(state: UsageTotals) -> dict:
    values = asdict(state)
    values["estimated_cost_usd"] = (
        format(state.estimated_cost_usd, "f") if state.estimated_cost_usd is not None else None
    )
    values["known_cost_usd"] = format(state.known_cost_usd, "f")
    return values


def project_usage_overview(state: UsageOverviewState) -> UsageOverviewVO:
    return UsageOverviewVO(
        status=state.status, message=state.message,
        from_start_time=state.from_start_time, to_start_time=state.to_start_time,
        fetched_at=state.fetched_at, truncated=state.truncated,
        totals=_usage_values(state.totals),
        models=[_usage_values(row) for row in state.models],
        runs=[_usage_values(row) for row in state.runs],
    )
