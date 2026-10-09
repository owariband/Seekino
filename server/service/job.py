from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ..model.dto.jobDTO import (
    ParseJobDetail,
    ParseJobSummaryState,
    VideoParseTaskDetail,
)
from ..model.job import ParseJobStatus, VideoParseTaskStatus
from ..repository.job import ParseJobRepository
from .library import LibraryService
from ..utils.validation import canonical_uuid, require_nonempty_text


class ParseJobService:
    def __init__(
        self,
        repository: ParseJobRepository,
        library: LibraryService,
        *,
        execution_mode: Literal["v1", "v2"] = "v1",
    ) -> None:
        self._repository = repository
        self._library = library
        self._pending_takeover_enabled = execution_mode == "v2"

    def get(self, job_id: str, *, tenant_id: str) -> ParseJobDetail:
        canonical_job_id = canonical_uuid(job_id, "job_id")
        job = self._repository.get(canonical_job_id)
        self._library.get_video(job.video_id, tenant_id=tenant_id)
        return ParseJobDetail(
            job=job,
            checkpoints=tuple(
                self._repository.list_checkpoints(canonical_job_id)
            ),
            processing_duration_ms=job.processing_duration_ms,
            processing_timing_complete=(
                job.processing_timing_complete
                and job.status
                in {ParseJobStatus.SUCCEEDED.value, ParseJobStatus.FAILED.value}
            ),
        )

    def get_video_parse_task(
        self,
        video_parse_task_id: str,
        *,
        tenant_id: str,
    ) -> VideoParseTaskDetail:
        task_id = canonical_uuid(video_parse_task_id, "video_parse_task_id")
        return replace(
            self._repository.get_video_parse_task_detail(
                task_id,
                tenant_id=require_nonempty_text(tenant_id, "tenant_id"),
            ),
            pending_takeover_enabled=self._pending_takeover_enabled,
        )

    def list_video_parse_tasks(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[VideoParseTaskDetail]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        return [
            replace(
                detail,
                pending_takeover_enabled=self._pending_takeover_enabled,
            )
            for detail in self._repository.list_video_parse_tasks_for_workspace(
                normalized_workspace,
                limit=limit,
                offset=offset,
                series_id=self._scope_series(series_id, normalized_workspace),
                statuses=self._video_parse_statuses(statuses),
            )
        ]

    def count_video_parse_tasks(
        self,
        *,
        tenant_id: str,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> int:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        return self._repository.count_video_parse_tasks_for_workspace(
            normalized_workspace,
            series_id=self._scope_series(series_id, normalized_workspace),
            statuses=self._video_parse_statuses(statuses),
        )

    def list(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
        series_id: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[ParseJobDetail]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        jobs = self._repository.list_for_workspace(
            normalized_workspace,
            limit=limit, offset=offset, series_id=self._scope_series(series_id, normalized_workspace),
            statuses=self._statuses(statuses),
        )
        checkpoints = self._repository.list_checkpoints_for_jobs(
            [item.job_id for item in jobs]
        )
        return [
            ParseJobDetail(
                job=item,
                checkpoints=tuple(checkpoints[item.job_id]),
                processing_duration_ms=item.processing_duration_ms,
                processing_timing_complete=(
                    item.processing_timing_complete
                    and item.status
                    in {
                        ParseJobStatus.SUCCEEDED.value,
                        ParseJobStatus.FAILED.value,
                    }
                ),
            )
            for item in jobs
        ]

    def count(self, *, tenant_id: str, series_id: str | None = None, statuses: tuple[str, ...] | None = None) -> int:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        return self._repository.count_for_workspace(
            normalized_workspace,
            series_id=self._scope_series(series_id, normalized_workspace), statuses=self._statuses(statuses),
        )

    def summary(self, *, tenant_id: str, series_id: str | None = None) -> ParseJobSummaryState:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        return self._repository.summary_for_workspace(
            normalized_workspace,
            series_id=self._scope_series(series_id, normalized_workspace),
        )

    @staticmethod
    def _statuses(statuses: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return tuple(sorted({ParseJobStatus(status).value for status in statuses})) if statuses is not None else None

    @staticmethod
    def _video_parse_statuses(
        statuses: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if statuses is None:
            return None
        return tuple(
            sorted({VideoParseTaskStatus(status).value for status in statuses})
        )

    def _scope_series(self, series_id: str | None, tenant_id: str) -> str | None:
        if series_id is None:
            return None
        normalized = canonical_uuid(series_id, "series_id")
        self._library.get_series(normalized, tenant_id=tenant_id)
        return normalized
