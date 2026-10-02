from __future__ import annotations

from fastapi import APIRouter

from ..api import jobs, parse_usage
from ..model.vo.commonVO import Page
from ..model.vo.jobVO import ParseJobView, JobSummary, VideoParseTaskView
from ..model.vo.parseUsageVO import ParseUsageVO

router = APIRouter()
router.add_api_route(
    "/api/jobs/usage/get",
    parse_usage.get_parse_usage,
    methods=["GET"],
    response_model=ParseUsageVO,
)
router.add_api_route(
    "/api/jobs/get",
    jobs.get_job,
    methods=["GET"],
    response_model=ParseJobView,
)
router.add_api_route(
    "/api/jobs/video-parse/get",
    jobs.get_video_parse_task,
    methods=["GET"],
    response_model=VideoParseTaskView,
)
router.add_api_route(
    "/api/jobs/video-parse/list",
    jobs.list_video_parse_tasks,
    methods=["GET"],
    response_model=Page[VideoParseTaskView],
)
router.add_api_route(
    "/api/jobs/video-parse/events",
    jobs.stream_video_parse_task_events,
    methods=["GET"],
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
