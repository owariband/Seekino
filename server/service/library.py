from __future__ import annotations

from engine.parse.background import extract_background_image_ids, prepare_background_text

from ..identity import canonical_uuid, derive_series_id
from ..model.dto.libraryDTO import SeriesState, VideoSummaryState
from ..repository.library import LibraryRepository
from ..storage.artifact import ArtifactStore
from ..utils.validation import normalize_language_tag_hint, require_nonempty_text


class LibraryService:
    def __init__(
        self, repository: LibraryRepository, artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store

    def create_series(
        self,
        *,
        workspace_id: str,
        name: str,
        description: str | None = None,
        transcript_language_hint: str | None = None,
        important_extra_data: dict | None = None,
        character_bible: list[dict] | None = None,
    ) -> SeriesState:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        normalized_name = require_nonempty_text(name, "name")
        normalized_description = None
        if description is not None:
            normalized_description = description.strip() or None
        normalized_language_hint = normalize_language_tag_hint(
            transcript_language_hint
        )
        series_id = derive_series_id(normalized_workspace, normalized_name)
        self._validate_background_images(
            series_id, prepare_background_text(character_bible or [], important_extra_data),
        )
        created = self._repository.create_series(
            series_id=series_id,
            workspace_id=normalized_workspace,
            name=normalized_name,
            description=normalized_description,
            transcript_language_hint=normalized_language_hint,
            important_extra_data=important_extra_data,
            character_bible=character_bible or [],
        )
        return self._repository.get_series(created.series_id, normalized_workspace)

    def update_series(
        self, series_id: str, *, workspace_id: str, changes: dict,
    ) -> SeriesState:
        series = self.get_series(series_id, workspace_id=workspace_id)
        if changes.keys() - {
            "description", "transcript_language_hint", "important_extra_data",
        }:
            raise ValueError(
                "only description, transcript_language_hint and "
                "important_extra_data can be updated"
            )
        normalized = dict(changes)
        if "description" in normalized and normalized["description"] is not None:
            description = normalized["description"]
            if not isinstance(description, str):
                raise ValueError("description must be text or null")
            normalized["description"] = description.strip() or None
        if "transcript_language_hint" in normalized:
            normalized["transcript_language_hint"] = normalize_language_tag_hint(
                normalized["transcript_language_hint"]
            )
        if "important_extra_data" in normalized and normalized["important_extra_data"] is not None:
            document = normalized["important_extra_data"]
            if not isinstance(document, dict) or document.keys() - {"format", "content"}:
                raise ValueError("important_extra_data must contain format and content")
            document_format = document.get("format", "markdown")
            content = document.get("content")
            if document_format not in ("markdown", "json") or not isinstance(content, str):
                raise ValueError("background must have a supported format and text content")
            normalized["important_extra_data"] = {"format": document_format, "content": content}
            try:
                background_text = prepare_background_text([], normalized["important_extra_data"])
            except ValueError as exc:
                raise ValueError("background content is not valid JSON") from exc
            self._validate_background_images(series.series_id, background_text)
        self._repository.update_series(
            series.series_id, workspace_id=series.workspace_id, changes=normalized,
        )
        return self._repository.get_series(series.series_id, series.workspace_id)

    def update_video_episode_order(
        self,
        video_id: str,
        *,
        workspace_id: str,
        episode_order: int,
    ) -> VideoSummaryState:
        if (
            isinstance(episode_order, bool)
            or not isinstance(episode_order, int)
            or episode_order <= 0
            or episode_order > 2_147_483_647
        ):
            raise ValueError(
                "episode_order must be a positive 32-bit signed integer"
            )
        normalized_video_id = canonical_uuid(video_id, "video_id")
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        self._repository.update_video_episode_order(
            normalized_video_id,
            workspace_id=normalized_workspace,
            episode_order=episode_order,
        )
        return self._repository.get_video(
            normalized_video_id,
            normalized_workspace,
        )

    def _validate_background_images(self, series_id: str, text: str | None) -> None:
        image_ids = extract_background_image_ids(text or "")
        if image_ids and self._artifact_store is None:
            raise ValueError("background image storage is unavailable")
        for image_id in image_ids:
            try:
                self._artifact_store.resolve_series_image(image_id, series_id=series_id)
                self._artifact_store.resolve_series_image(image_id, series_id=series_id, variant="model")
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError("background image does not exist in this Series") from exc

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
