from __future__ import annotations

from ..identity import canonical_uuid, derive_series_id
from ..model.dto.libraryDTO import SeriesState, VideoSummaryState
from ..repository.library import LibraryRepository
from ..utils.validation import require_nonempty_text


class LibraryService:
    def __init__(self, repository: LibraryRepository) -> None:
        self._repository = repository

    def create_series(
        self,
        *,
        workspace_id: str,
        name: str,
        description: str | None = None,
        important_extra_data: dict | None = None,
        character_bible: list[dict] | None = None,
    ) -> SeriesState:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        normalized_name = require_nonempty_text(name, "name")
        normalized_description = None
        if description is not None:
            normalized_description = description.strip() or None
        created = self._repository.create_series(
            series_id=derive_series_id(normalized_workspace, normalized_name),
            workspace_id=normalized_workspace,
            name=normalized_name,
            description=normalized_description,
            important_extra_data=important_extra_data,
            character_bible=character_bible or [],
        )
        return self._repository.get_series(created.series_id, normalized_workspace)

    def list_series(
        self,
        *,
        workspace_id: str,
        status: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[SeriesState]:
        if status is not None and status not in {"EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"}:
            raise ValueError("status is not a supported Series status")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise ValueError("limit must be between 1 and 100")
        return self._repository.list_series(
            require_nonempty_text(workspace_id, "workspace_id"),
            status=status, query=query.strip() if query else None, offset=offset, limit=limit,
        )

    def count_series(self, *, workspace_id: str, status: str | None = None, query: str | None = None) -> int:
        if status is not None and status not in {"EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"}:
            raise ValueError("status is not a supported Series status")
        return self._repository.count_series(
            require_nonempty_text(workspace_id, "workspace_id"),
            status=status, query=query.strip() if query else None,
        )

    def get_series(self, series_id: str, *, workspace_id: str) -> SeriesState:
        return self._repository.get_series(
            canonical_uuid(series_id, "series_id"),
            require_nonempty_text(workspace_id, "workspace_id"),
        )

    def get_video(
        self,
        video_id: str,
        *,
        workspace_id: str,
    ) -> VideoSummaryState:
        return self._repository.get_video(
            canonical_uuid(video_id, "video_id"),
            require_nonempty_text(workspace_id, "workspace_id"),
        )
