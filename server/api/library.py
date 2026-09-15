from __future__ import annotations

from dataclasses import asdict

from fastapi import Query, Request

from ..model.dto.libraryDTO import (
    Character,
    SeriesCreateRequest,
    SeriesState,
    VideoSummaryState,
)
from ..model.job import ParseJobStatus
from ..service.media import MediaService
from ..service.pagination import decode_cursor, encode_cursor
from .uploads import project_upload_session
from .context import get_services
from ..model.vo.commonVO import Page, Failure, ImageAsset
from ..model.vo.libraryVO import (
    SeriesView,
    SeriesSummary,
    SeriesDetail,
    VideoSummary,
    LastSearchView,
)


def create_series(request: Request, body: SeriesCreateRequest) -> SeriesView:
    state = get_services(request).library.create_series(
        workspace_id=body.workspace_id,
        name=body.name,
        description=body.description,
        important_extra_data=body.important_extra_data,
        character_bible=[item.model_dump() for item in body.character_bible],
    )
    return _series_view(state, get_services(request).media)


def list_series(
    request: Request,
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    status: str | None = Query(None),
    query: str | None = Query(None),
) -> Page[SeriesSummary]:
    services = get_services(request)
    normalized_query = query.strip() if query else None
    scope = {"workspaceId": workspace_id, "status": status, "query": normalized_query, "sort": "createdAt,seriesId:asc"}
    offset = decode_cursor(cursor, scope=scope)
    states = services.library.list_series(
        workspace_id=workspace_id, status=status, query=normalized_query, offset=offset, limit=limit,
    )
    total = services.library.count_series(workspace_id=workspace_id, status=status, query=normalized_query)
    has_more = offset + len(states) < total
    return Page[SeriesSummary](
        items=[project_series_summary(state, services.media) for state in states], total=total,
        next_cursor=encode_cursor(offset + len(states), scope=scope) if has_more else None,
        has_more=has_more,
    )


def get_series(
    request: Request,
    series_id: str = Query(alias="seriesId"),
    workspace_id: str = Query(alias="workspaceId", min_length=1, max_length=64),
) -> SeriesView:
    state = get_services(request).library.get_series(
        series_id,
        workspace_id=workspace_id,
    )
    return _series_view(state, get_services(request).media)


def _series_view(state: SeriesState, media: MediaService) -> SeriesView:
    return project_series_detail(state, media)


def project_video(state: VideoSummaryState, media: MediaService) -> VideoSummary:
    playback = media.playback_view(state.video_id, workspace_id=state.workspace_id, video=state)
    thumbnail = media.thumbnail_view(state.video_id, workspace_id=state.workspace_id, video=state)
    error = Failure(code="PROCESSING_FAILED", message="视频建库发生错误，请查看任务状态") if state.last_error else None
    if error is None and playback.status == "MISSING":
        error = Failure(code="MEDIA_MISSING", message="原视频文件已丢失")
    elif error is None and playback.status == "FAILED":
        error = Failure(code="MEDIA_NOT_READY", message="原视频尚无可确认兼容浏览器的播放媒体")
    return VideoSummary(
        video_id=state.video_id, workspace_id=state.workspace_id, series_id=state.series_id,
        episode_order=state.episode_order, upload_sequence=state.upload_sequence,
        original_filename=state.original_filename, total_bytes=state.total_bytes,
        duration_ms=state.duration_ms, upload_status=state.upload_status,
        upload_progress=state.upload_progress,
        parse_status=ParseJobStatus.SUCCEEDED if state.parse_status == "COMPLETED" else state.parse_status,
        parse_progress=state.parse_progress, index_status=state.index_status,
        playback_status=playback.status, playback_url=playback.url,
        playback_url_expires_at=playback.expires_at,
        thumbnail=ImageAsset.model_validate(asdict(thumbnail)) if thumbnail else None,
        latest_job_id=state.latest_job_id, last_error=error,
    )


def project_series_summary(state: SeriesState, media: MediaService) -> SeriesSummary:
    representative = state.representative_video
    cover = media.thumbnail_view(
        representative.video_id, workspace_id=state.workspace_id, video=representative,
    ) if representative else None
    return SeriesSummary(
        series_id=state.series_id, workspace_id=state.workspace_id,
        name=state.name, description=state.description, status=state.status,
        video_count=state.video_count, ready_video_count=state.ready_video_count,
        failed_video_count=state.failed_video_count, pending_upload_count=state.pending_upload_count,
        cover=ImageAsset.model_validate(asdict(cover)) if cover else None,
        last_search=LastSearchView.model_validate(asdict(state.last_search)) if state.last_search else None,
        created_at=state.created_at, updated_at=state.updated_at,
    )


def project_series_detail(state: SeriesState, media: MediaService) -> SeriesDetail:
    return SeriesDetail(
        **project_series_summary(state, media).model_dump(),
        important_extra_data=state.important_extra_data,
        character_bible=[Character(name=item["name"], aliases=item.get("aliases", [])) for item in state.character_bible],
        videos=[project_video(video, media) for video in state.videos],
        pending_uploads=[project_upload_session(upload) for upload in state.pending_uploads],
    )
