from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ..model.dto.taskDTO import VideoParseTaskDetail
from ..model.task import VideoParseTaskStatus
from ..repository.job import ParseJobRepository
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from .library import LibraryService
from ..utils.validation import canonical_uuid, require_nonempty_text


class ParseJobService:
    def __init__(
        self,
        repository: ParseJobRepository,
        library: LibraryService,
        *,
        execution_mode: Literal["v2"] = "v2",
        video_parse_index_store: ElasticsearchVideoChunkStore | None = None,
        video_event_index_store: ElasticsearchVideoEventStore | None = None,
    ) -> None:
        if execution_mode != "v2":
            raise ValueError("execution_mode must be 'v2'")
        self._repository = repository
        self._library = library
        self._video_parse_index_store = video_parse_index_store
        self._video_event_index_store = video_event_index_store

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

    def delete_video_parse_task(
        self,
        *,
        tenant_id: str,
        video_parse_task_id: str | None = None,
        expected_job_version: int | None = None,
        video_id: str | None = None,
    ) -> list[str]:
        from .parse_control import delete_task_versions
        from ..utils.time import now_utc
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        if video_parse_task_id is None and video_id is None:
            raise ValueError("one of video_parse_task_id or video_id is required")
        if video_parse_task_id is not None and video_id is not None:
            raise ValueError("video_parse_task_id and video_id are mutually exclusive")
        if video_parse_task_id is not None and (
            isinstance(expected_job_version, bool)
            or not isinstance(expected_job_version, int)
            or expected_job_version < 1
        ):
            raise ValueError("expected_job_version must be a positive integer")
        task_id = (
            canonical_uuid(video_parse_task_id, "video_parse_task_id")
            if video_parse_task_id is not None else None
        )
        normalized_video_id = (
            canonical_uuid(video_id, "video_id") if video_id is not None else None
        )
        with self._repository.transaction() as session:
            deleted_ids, es_keys = delete_task_versions(
                session,
                tenant_id=normalized_workspace,
                now=now_utc(),
                video_parse_task_id=task_id,
                expected_job_version=expected_job_version,
                video_id=normalized_video_id,
            )
        self._delete_version_es_documents(es_keys)
        return deleted_ids

    def _delete_version_es_documents(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        if self._video_parse_index_store is None:
            raise RuntimeError("Video Parse V2 index cleanup store is unavailable")
        self._video_parse_index_store.bulk_delete(keys, refresh="wait_for")
        if not self._video_parse_index_store.verify_absent(keys):
            raise RuntimeError("Video Parse V2 index cleanup was not visible")
        if self._video_event_index_store is not None:
            self._video_event_index_store.bulk_delete(keys, refresh="wait_for")
            if not self._video_event_index_store.verify_absent(keys):
                raise RuntimeError("Event index cleanup was not visible")

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
