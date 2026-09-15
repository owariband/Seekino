from __future__ import annotations

from fastapi import APIRouter

from ..api import jobs
from ..model.vo.commonVO import Page
from ..model.vo.jobVO import ParseJobView, JobSummary

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
    response_model=Page[ParseJobView],
)
router.add_api_route(
    "/api/jobs/summary",
    jobs.get_job_summary,
    methods=["GET"],
    response_model=JobSummary,
)
router.add_api_route(
    "/api/jobs/events",
    jobs.stream_job_events,
    methods=["GET"],
)
