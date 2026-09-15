from __future__ import annotations

from dataclasses import asdict

from fastapi import Query, Request
from fastapi.responses import FileResponse

from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..repository.library import SeriesState
from .context import get_services
from .schema import SeriesCreateRequest, SeriesView, VideoSummary


def create_series(request: Request, body: SeriesCreateRequest) -> SeriesView:
    state = get_services(request).library.create_series(
        workspace_id=body.workspace_id,
        name=body.name,
        description=body.description,
        important_extra_data=body.important_extra_data,
        character_bible=body.character_bible,
    )
    return _series_view(state)


def list_series(
    request: Request,
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> list[SeriesView]:
    return [
        _series_view(item)
        for item in get_services(request).library.list_series(
            workspace_id=workspace_id
        )
    ]


def get_series(
    request: Request,
    series_id: str = Query(alias="seriesId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> SeriesView:
    state = get_services(request).library.get_series(
        series_id,
        workspace_id=workspace_id,
    )
    return _series_view(state)


def get_video_content(
    request: Request,
    video_id: str = Query(alias="videoId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> FileResponse:
    media = get_services(request).media.get_original(
        video_id,
        workspace_id=workspace_id,
    )
    return FileResponse(
        media.path,
        media_type=media.media_type,
        filename=media.path.name,
        content_disposition_type="inline",
    )


def _series_view(state: SeriesState) -> SeriesView:
    payload = asdict(state)
    payload["videos"] = [
        VideoSummary.model_validate(item) for item in payload["videos"]
    ]
    return SeriesView.model_validate(payload)
