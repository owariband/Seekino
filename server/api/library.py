from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..model.dto.libraryDTO import (
    Character,
    SeriesCreateRequest,
    SeriesState,
    SeriesUpdateRequest,
    VideoEpisodeUpdateRequest,
    VideoSummaryState,
)
from ..model.job import ParseJobStatus
from ..model.video import SeriesVisibility
from ..service.media import MediaService
from ..service.access import is_series_member, require_admin, require_tenant_read, require_tenant_write, series_access
from ..service.pagination import decode_cursor, encode_cursor
from .uploads import project_upload_session
from .context import get_identity, get_services
from ..model.vo.commonVO import Page, Failure, ImageAsset
from ..model.vo.libraryVO import (
    SeriesView,
    SeriesSummary,
    SeriesDetail,
    VideoSummary,
    LastSearchView,
)


def create_series(request: Request, body: SeriesCreateRequest) -> SeriesView:
    identity = get_identity(request)
    require_tenant_write(identity, body.tenant_id)
    state = get_services(request).library.create_series(
        tenant_id=body.tenant_id,
        name=body.name,
        description=body.description,
        transcript_language_hint=body.transcript_language_hint,
        important_extra_data=body.important_extra_data,
        character_bible=[item.model_dump() for item in body.character_bible],
    )
    return _series_view(state, get_services(request).media)


def update_series(request: Request, body: SeriesUpdateRequest) -> SeriesView | Response:
    services = get_services(request)
    identity = get_identity(request)
    changes = body.model_dump(exclude_unset=True, exclude={"tenant_id", "series_id"})
    series = services.library.get_series(body.series_id, tenant_id=body.tenant_id, owner_user_id=identity.user_id)
    takedown = not is_series_member(identity, series.tenant_id) and changes == {
        "visibility": SeriesVisibility.PRIVATE,
    } and identity.role == "admin"
    if takedown:
        require_admin(identity)
    else:
        require_tenant_write(identity, series.tenant_id)
    if changes.get("visibility") == SeriesVisibility.PUBLIC:
        if not services.media.publication_ready(series, require_published=False):
            raise ValueError("only a complete Series with playable media can be published")
    state = services.library.update_series(
        body.series_id, tenant_id=body.tenant_id, changes=changes, owner_user_id=identity.user_id,
    )
    if takedown:
        return Response(status_code=204)
    return _series_view(state, services.media)


def update_video_episode(
    request: Request,
    body: VideoEpisodeUpdateRequest,
) -> VideoSummary:
    services = get_services(request)
    require_tenant_write(get_identity(request), body.tenant_id)
    state = services.library.update_video_episode_order(
        body.video_id,
        tenant_id=body.tenant_id,
        episode_order=body.episode_order,
    )
    return project_video(state, services.media)


async def upload_series_image(
    request: Request,
    series_id: str = Query(alias="seriesId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> ImageAsset:
    services = get_services(request)
    require_tenant_write(get_identity(request), tenant_id)
    await run_in_threadpool(services.library.get_series, series_id, tenant_id=tenant_id)
    maximum_bytes = services.media.image_upload_config.maximum_upload_size_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be a nonnegative integer") from exc
        if declared_size < 0 or declared_size > maximum_bytes:
            raise ValueError("background image exceeds the upload size limit")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError("background image exceeds the upload size limit")
        chunks.append(chunk)
    if content_length is not None and size != declared_size:
        raise ValueError("background image body size does not match Content-Length")
    image = await run_in_threadpool(
        services.media.upload_series_image, series_id, b"".join(chunks), tenant_id=tenant_id,
    )
    return ImageAsset.model_validate(asdict(image))


def list_series(
    request: Request,
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    status: str | None = Query(None),
    query: str | None = Query(None),
) -> Page[SeriesSummary]:
    services = get_services(request)
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    normalized_query = query.strip() if query else None
    scope = {"tenantId": tenant_id, "ownerUserId": identity.user_id, "status": status, "query": normalized_query, "sort": "createdAt,seriesId:asc"}
    offset = decode_cursor(cursor, scope=scope)
    if identity.mode == "cloud" and not is_series_member(identity, tenant_id):
        states, total = services.library.list_public_series_page(
            tenant_id=tenant_id, media=services.media, owner_user_id=identity.user_id,
            status=status, query=normalized_query, offset=offset, limit=limit,
        )
    else:
        states = services.library.list_series(
            tenant_id=tenant_id, status=status, query=normalized_query, offset=offset, limit=limit,
            owner_user_id=identity.user_id,
        )
        total = services.library.count_series(tenant_id=tenant_id, status=status, query=normalized_query)
    has_more = offset + len(states) < total
    return Page[SeriesSummary](
        items=[project_series_summary(state, services.media, access=series_access(identity, state)) for state in states], total=total,
        next_cursor=encode_cursor(offset + len(states), scope=scope) if has_more else None,
        has_more=has_more,
    )


def list_public_series(
    request: Request,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    query: str | None = Query(None),
) -> Page[SeriesSummary]:
    services = get_services(request)
    identity = get_identity(request)
    normalized_query = query.strip() if query else None
    scope = {"source": "public", "query": normalized_query, "sort": "createdAt,seriesId:asc"}
    offset = decode_cursor(cursor, scope=scope)
    states, total = services.library.list_public_series_page(
        tenant_id=None, media=services.media, owner_user_id=identity.user_id,
        query=normalized_query, offset=offset, limit=limit,
    )
    has_more = offset + len(states) < total
    return Page[SeriesSummary](
        items=[project_series_summary(state, services.media, access=series_access(identity, state)) for state in states],
        total=total,
        next_cursor=encode_cursor(offset + len(states), scope=scope) if has_more else None,
        has_more=has_more,
    )


def get_series(
    request: Request,
    series_id: str = Query(alias="seriesId"),
    tenant_id: str = Query(alias="tenantId", min_length=1, max_length=64),
) -> SeriesView:
    state = read_series(request, series_id, tenant_id=tenant_id)
    return _series_view(state, get_services(request).media, access=series_access(get_identity(request), state))


def read_series(request: Request, series_id: str, *, tenant_id: str) -> SeriesState:
    identity = get_identity(request)
    services = get_services(request)
    state = services.library.get_series(series_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
    services.media.require_readable_series(identity, state)
    return state


def _series_view(state: SeriesState, media: MediaService, *, access: Literal["member", "public"] = "member") -> SeriesView:
    return project_series_detail(state, media, access=access)


def project_video(state: VideoSummaryState, media: MediaService, *, access: Literal["member", "public"] = "member") -> VideoSummary:
    playback = media.playback_view(state.video_id, tenant_id=state.tenant_id, video=state)
    thumbnail = media.thumbnail_view(state.video_id, tenant_id=state.tenant_id, video=state)
    error = Failure(code="PROCESSING_FAILED", message="视频建库发生错误，请查看任务状态") if state.last_error else None
    if error is None and playback.status == "MISSING":
        error = Failure(code="MEDIA_MISSING", message="原视频文件已丢失")
    elif error is None and playback.status == "FAILED":
        error = Failure(code="MEDIA_NOT_READY", message="原视频尚无可确认兼容浏览器的播放媒体")
    return VideoSummary(
        video_id=state.video_id, tenant_id=state.tenant_id, series_id=state.series_id,
        episode_order=state.episode_order, upload_sequence=state.upload_sequence,
        original_filename=state.original_filename if access == "member" else None,
        total_bytes=state.total_bytes if access == "member" else None,
        duration_seconds=state.duration_seconds, upload_status=state.upload_status,
        upload_progress=state.upload_progress,
        parse_status=ParseJobStatus.SUCCEEDED if state.parse_status == "COMPLETED" else state.parse_status,
        parse_progress=state.parse_progress, index_status=state.index_status,
        playback_status=playback.status, playback_url=playback.url,
        playback_url_expires_at=playback.expires_at,
        thumbnail=ImageAsset.model_validate(asdict(thumbnail)) if thumbnail else None,
        latest_job_id=state.latest_job_id if access == "member" else None,
        last_error=error if access == "member" else None,
    )


def project_series_summary(state: SeriesState, media: MediaService, *, access: Literal["member", "public"] = "member") -> SeriesSummary:
    representative = state.representative_video
    cover = media.thumbnail_view(
        representative.video_id, tenant_id=state.tenant_id, video=representative,
    ) if representative else None
    return SeriesSummary(
        series_id=state.series_id, tenant_id=state.tenant_id,
        visibility=state.visibility, access=access,
        publisher=asdict(state.publisher) if state.publisher else None,
        name=state.name, display_name=state.display_name, description=state.description, status=state.status,
        video_count=state.video_count, ready_video_count=state.ready_video_count,
        failed_video_count=state.failed_video_count if access == "member" else 0,
        pending_upload_count=state.pending_upload_count if access == "member" else 0,
        cover=ImageAsset.model_validate(asdict(cover)) if cover else None,
        last_search=LastSearchView.model_validate(asdict(state.last_search)) if state.last_search else None,
        created_at=state.created_at, updated_at=state.updated_at,
    )


def project_series_detail(state: SeriesState, media: MediaService, *, access: Literal["member", "public"] = "member") -> SeriesDetail:
    return SeriesDetail(
        **project_series_summary(state, media, access=access).model_dump(),
        transcript_language_hint=state.transcript_language_hint if access == "member" else None,
        important_extra_data=state.important_extra_data if access == "member" else None,
        character_bible=[Character(name=item["name"], aliases=item.get("aliases", [])) for item in state.character_bible] if access == "member" else [],
        videos=[project_video(video, media, access=access) for video in state.videos],
        pending_uploads=[project_upload_session(upload) for upload in state.pending_uploads] if access == "member" else [],
    )
