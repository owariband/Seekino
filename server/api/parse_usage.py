from __future__ import annotations

from fastapi import Query, Request

from ..model.dto.parseUsageDTO import ParseUsageState
from ..model.vo.parseUsageVO import ParseUsageRowVO, ParseUsageVO
from .context import get_services


def get_parse_usage(request: Request, job_id: str = Query(alias="jobId"), workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64)) -> ParseUsageVO:
    service = get_services(request).parse_usage
    if service is None:
        raise RuntimeError("Parse usage service is unavailable")
    return project_parse_usage(service.get(job_id, workspace_id=workspace_id))


def project_parse_usage(state: ParseUsageState) -> ParseUsageVO:
    job = state.job
    return ParseUsageVO(
        job_id=job.job_id, video_id=job.video_id, series_id=job.series_id, series_name=job.series_name,
        episode_order=job.episode_order, original_filename=job.original_filename, coverage=state.coverage,
        total_tokens=state.total_tokens,
        estimated_cost_cny=format(state.estimated_cost_cny, "f") if state.estimated_cost_cny is not None else None,
        call_count=state.call_count, missing_usage_count=state.missing_usage_count, unpriced_call_count=state.unpriced_call_count,
        items=[ParseUsageRowVO(
            provider=item.provider, model=item.model, category=item.category, operation=item.operation,
            call_count=item.call_count, input_tokens=item.input_tokens, output_tokens=item.output_tokens,
            cached_input_tokens=item.cached_input_tokens, reasoning_tokens=item.reasoning_tokens,
            total_tokens=item.total_tokens,
            estimated_cost_cny=format(item.estimated_cost_cny, "f") if item.estimated_cost_cny is not None else None,
            missing_usage_count=item.missing_usage_count, unpriced_call_count=item.unpriced_call_count,
        ) for item in state.items],
    )
