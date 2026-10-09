from __future__ import annotations

from dataclasses import asdict

from fastapi import Query, Request

from ..model.dto.parseUsageDTO import UsageOverviewState
from ..model.vo.parseUsageVO import UsageOverviewVO
from ..service.access import require_tenant_read
from ..error.auth import AuthError
from ..utils.time import MAX_UNIX_TIME_MS
from .context import get_identity, get_services


def get_usage_overview(
    request: Request,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    from_start_time: int | None = Query(None, alias="fromStartTime", ge=0, le=MAX_UNIX_TIME_MS),
    to_start_time: int | None = Query(None, alias="toStartTime", ge=0, le=MAX_UNIX_TIME_MS),
    series_id: str | None = Query(None, alias="seriesId", min_length=1, max_length=64),
    run_id: str | None = Query(None, alias="runId", min_length=1, max_length=256),
) -> UsageOverviewVO:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    if identity.mode != "local" and (identity.is_guest or identity.auth_level != "email"):
        raise AuthError("登录后可使用此功能", status_code=403, code="LOGIN_REQUIRED")
    service = get_services(request).parse_usage
    if service is None:
        raise RuntimeError("Model usage service is unavailable")
    return project_usage_overview(service.overview(
        tenant_id=tenant_id, from_start_time=from_start_time,
        to_start_time=to_start_time, series_id=series_id, run_id=run_id,
        actor_user_id=identity.user_id if identity.mode != "local" else None,
    ))


def project_usage_overview(state: UsageOverviewState) -> UsageOverviewVO:
    return UsageOverviewVO(
        status=state.status, message=state.message,
        from_start_time=state.from_start_time, to_start_time=state.to_start_time,
        fetched_at=state.fetched_at, truncated=state.truncated,
        totals=asdict(state.totals),
        models=[asdict(row) for row in state.models],
        runs=[asdict(row) for row in state.runs],
    )
