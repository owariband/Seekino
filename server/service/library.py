from __future__ import annotations

from engine.common.background import extract_background_image_ids, prepare_background_text

from typing import TYPE_CHECKING

from ..constants.identity import LOCAL_USER_ID

from ..model.identifiers import derive_series_id
from ..model.dto.libraryDTO import SeriesState, VideoSummaryState
from ..model.video import SeriesVisibility
from ..repository.library import LibraryRepository
from ..storage.artifact import ArtifactStore
from ..utils.validation import canonical_uuid, normalize_language_tag_hint, require_nonempty_text

if TYPE_CHECKING:
    from .media import MediaService


class LibraryService:
    def __init__(
        self, repository: LibraryRepository, artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store

    def create_series(
        self,
        *,
        tenant_id: str,
        name: str,
        description: str | None = None,
        transcript_language_hint: str | None = None,
        important_extra_data: dict | None = None,
        character_bible: list[dict] | None = None,
    ) -> SeriesState:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
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
            tenant_id=normalized_workspace,
            name=normalized_name,
            description=normalized_description,
            transcript_language_hint=normalized_language_hint,
            important_extra_data=important_extra_data,
            character_bible=character_bible or [],
        )
        return self._repository.get_series(created.series_id, normalized_workspace)

    def update_series(
        self, series_id: str, *, tenant_id: str, changes: dict, owner_user_id: str = LOCAL_USER_ID,
    ) -> SeriesState:
        series = self.get_series(series_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        if changes.keys() - {
            "display_name", "description", "transcript_language_hint", "important_extra_data",
            "visibility",
        }:
            raise ValueError(
                "only display_name, description, transcript_language_hint, "
                "important_extra_data and visibility can be updated"
            )
        normalized = dict(changes)
        if "display_name" in normalized and normalized["display_name"] is not None:
            display_name = normalized["display_name"]
            if not isinstance(display_name, str) or len(display_name.strip()) > 255:
                raise ValueError("display_name must be text up to 255 characters or null")
            normalized["display_name"] = display_name.strip() or None
        if "visibility" in normalized:
            try:
                normalized["visibility"] = SeriesVisibility(normalized["visibility"])
            except (TypeError, ValueError) as exc:
                raise ValueError("visibility must be private or public") from exc
            if normalized["visibility"] == SeriesVisibility.PUBLIC and (
                series.status != "READY" or series.video_count == 0
                or series.ready_video_count != series.video_count or series.pending_upload_count > 0
            ):
                raise ValueError("only a complete, ready Series can be published")
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
            series.series_id, tenant_id=series.tenant_id, changes=normalized,
        )
        return self._repository.get_series(series.series_id, series.tenant_id, owner_user_id=owner_user_id)

    def update_video_episode_order(
        self,
        video_id: str,
        *,
        tenant_id: str,
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
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        self._repository.update_video_episode_order(
            normalized_video_id,
            tenant_id=normalized_workspace,
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
        tenant_id: str,
        status: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        public_only: bool = False,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[SeriesState]:
        if status is not None and status not in {"EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"}:
            raise ValueError("status is not a supported Series status")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise ValueError("limit must be between 1 and 100")
        return self._repository.list_series(
            require_nonempty_text(tenant_id, "tenant_id"),
            status=status, query=query.strip() if query else None, offset=offset, limit=limit,
            public_only=public_only, owner_user_id=owner_user_id,
        )

    def list_public_series_page(
        self, *, tenant_id: str | None, media: MediaService, owner_user_id: str,
        status: str | None = None, query: str | None = None, offset: int = 0, limit: int = 50,
    ) -> tuple[list[SeriesState], int]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if status is not None and status not in {"EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"}:
            raise ValueError("status is not a supported Series status")
        candidates = self._repository.list_series(
            tenant_id=None if tenant_id is None else require_nonempty_text(tenant_id, "tenant_id"),
            status=status, query=query.strip() if query else None,
            public_only=True, owner_user_id=owner_user_id,
        )
        visible: list[SeriesState] = []
        for candidate in candidates:
            if not candidate.publisher_active:
                continue
            state = self.get_series(candidate.series_id, tenant_id=candidate.tenant_id, owner_user_id=owner_user_id)
            if state.publisher_active and media.publication_ready(state):
                visible.append(state)
        return visible[offset:offset + limit], len(visible)

    def count_series(self, *, tenant_id: str, status: str | None = None, query: str | None = None) -> int:
        if status is not None and status not in {"EMPTY", "INDEXING", "PARTIAL", "READY", "FAILED"}:
            raise ValueError("status is not a supported Series status")
        return self._repository.count_series(
            require_nonempty_text(tenant_id, "tenant_id"),
            status=status, query=query.strip() if query else None,
        )

    def get_series(self, series_id: str, *, tenant_id: str, owner_user_id: str = LOCAL_USER_ID) -> SeriesState:
        return self._repository.get_series(
            canonical_uuid(series_id, "series_id"),
            require_nonempty_text(tenant_id, "tenant_id"),
            owner_user_id=owner_user_id,
        )

    def get_video(
        self,
        video_id: str,
        *,
        tenant_id: str,
    ) -> VideoSummaryState:
        return self._repository.get_video(
            canonical_uuid(video_id, "video_id"),
            require_nonempty_text(tenant_id, "tenant_id"),
        )
