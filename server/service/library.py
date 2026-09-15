from __future__ import annotations

from ..identity import canonical_uuid, derive_series_id
from ..repository.library import LibraryRepository, SeriesState, VideoSummaryState
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
        return self._repository.create_series(
            series_id=derive_series_id(normalized_workspace, normalized_name),
            workspace_id=normalized_workspace,
            name=normalized_name,
            description=normalized_description,
            important_extra_data=important_extra_data,
            character_bible=character_bible or [],
        )

    def list_series(self, *, workspace_id: str) -> list[SeriesState]:
        return self._repository.list_series(
            require_nonempty_text(workspace_id, "workspace_id")
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
