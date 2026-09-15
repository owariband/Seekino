from __future__ import annotations

from fastapi import APIRouter

from ..api import jobs
from ..api.schema import ParseJobView

router = APIRouter()
router.add_api_route(
    "/api/jobs/get",
    jobs.get_job,
    methods=["GET"],
    response_model=ParseJobView,
)
router.add_api_route(
    "/api/jobs/list",
    jobs.list_jobs,
    methods=["GET"],
    response_model=list[ParseJobView],
)
router.add_api_route(
    "/api/jobs/events",
    jobs.stream_job_events,
    methods=["GET"],
)
