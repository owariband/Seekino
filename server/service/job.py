from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ..model.dto.jobDTO import VideoParseTaskDetail
from ..model.job import VideoParseTaskStatus
from ..repository.job import ParseJobRepository
from .library import LibraryService
from ..utils.validation import canonical_uuid, require_nonempty_text


class ParseJobService:
    def __init__(
        self,
        repository: ParseJobRepository,
        library: LibraryService,
        *,
        execution_mode: Literal["v2"] = "v2",
    ) -> None:
        if execution_mode != "v2":
            raise ValueError("execution_mode must be 'v2'")
        self._repository = repository
        self._library = library

    def set_paused(self, video_parse_task_id: str, *, tenant_id: str, paused: bool,
                   expected_version: int, expected_lease_version: int) -> VideoParseTaskDetail:
        from .parse_control import set_paused
        from ..utils.time import now_utc
        task_id = canonical_uuid(video_parse_task_id, "video_parse_task_id")
        tenant_id = require_nonempty_text(tenant_id, "tenant_id")
        with self._repository.transaction() as session:
            set_paused(session, task_id, tenant_id=tenant_id, paused=paused,
                       expected_version=expected_version, expected_lease_version=expected_lease_version,
                       now=now_utc())
        return self.get_video_parse_task(task_id, tenant_id=tenant_id)

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
            pending_takeover_enabled=True,
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
                pending_takeover_enabled=True,
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
