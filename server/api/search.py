from __future__ import annotations

from fastapi import Request

from engine.rag.contracts import VideoChunkSearchEvidence

from .context import get_identity, get_services, revalidate_identity
from .library import read_series
from ..config.retrieval import RetrievalConfig
from ..service.access import model_query, require_tenant_read
from ..model.dto.mediaDTO import PlaybackMedia
from ..model.dto.searchDTO import VideoSearchRequest
from ..model.vo.searchVO import (
    Keyframe,
    SearchEvidenceFieldsView,
    SearchEvidenceView,
    SearchHitView,
    SearchResultView,
    VideoChunkCandidateView,
    VideoChunkEvidenceView,
    VisualEventView,
)
from ..schema import VideoChunkEvidence, VideoChunkSearchResult
from ..service.media import MediaService


def search_video_chunks(
    body: VideoSearchRequest,
    request: Request,
) -> SearchResultView:
    services = get_services(request)
    identity = get_identity(request)
    require_tenant_read(identity, body.tenant_id)
    config = getattr(request.app.state, "retrieval_config", RetrievalConfig())
    top_k = config.search_default_top_k if body.top_k is None else body.top_k
    series_ids = list(body.series_ids)
    if identity.mode == "cloud":
        if not series_ids:
            if body.tenant_id == identity.public_tenant_id:
                states, _total = services.library.list_public_series_page(
                    tenant_id=body.tenant_id, media=services.media, owner_user_id=identity.user_id, limit=100,
                )
                while len(states) < _total:
                    page, _total = services.library.list_public_series_page(
                        tenant_id=body.tenant_id, media=services.media, owner_user_id=identity.user_id,
                        offset=len(states), limit=100,
                    )
                    if not page:
                        break
                    states.extend(page)
            else:
                states = services.library.list_series(tenant_id=body.tenant_id, owner_user_id=identity.user_id)
            series_ids = [state.series_id for state in states if state.status == "READY"]
        if not series_ids:
            raise ValueError("当前资料来源还没有可检索的视频课程")
        for series_id in series_ids:
            read_series(request, series_id, tenant_id=body.tenant_id)
    with model_query(getattr(services, "auth", None), identity, request.client.host if request.client else "unknown"):
        result = services.retrieval.search(
            body.query, tenant_id=body.tenant_id, series_ids=series_ids, top_k=top_k,
        )
    if identity.mode == "cloud":
        request.state.identity = revalidate_identity(request)
        result = _validated_search_result(result)
        services.media.require_series_access(
            request.state.identity,
            [*result.series_ids, *(hit.evidence.series_id for hit in result.hits)],
            tenant_id=body.tenant_id,
        )
    return search_result_view(result, services.media, tenant_id=body.tenant_id)


def evidence_view(
    evidence: VideoChunkEvidence, media: MediaService, *, tenant_id: str,
    playback: PlaybackMedia | None = None,
) -> tuple[VideoChunkEvidenceView, list[str]]:
    if evidence.tenant_id != tenant_id:
        from ..repository.library import SeriesNotFoundError
        raise SeriesNotFoundError("片段资源不存在")
    playback = playback or media.playback_view(evidence.video_id, tenant_id=tenant_id, video=evidence)
    warnings: list[str] = []
    if playback.status != "READY":
        warnings.append("原视频文件已丢失" if playback.status == "MISSING" else "尚无可确认兼容浏览器的播放媒体")
    keyframes = [
        Keyframe(
            image_id=item.image_id,
            url=item.url,
            expires_at=item.expires_at,
            at_seconds=item.at_seconds,
            alt=evidence.baseline_visual_caption or evidence.window_summary or None,
        )
        for item in media.keyframe_views(evidence, tenant_id=tenant_id)
    ]
    events: list[VisualEventView] = []
    for item in evidence.all_time_events:
        start = item.get("start_seconds", item.get("startSeconds"))
        end = item.get("end_seconds", item.get("endSeconds"))
        description = item.get("description")
        if (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and 0 <= start < end <= evidence.duration_seconds
            and isinstance(description, str) and description.strip()):
            events.append(VisualEventView(description=description, start_seconds=start, end_seconds=end))
        else:
            warnings.append("部分视觉事件缺少合法的原视频时间位置，已保留文字摘要")
    transcript = [cue for cue in evidence.transcript if 0 <= cue.start_seconds < cue.end_seconds <= evidence.duration_seconds]
    if len(transcript) != len(evidence.transcript):
        warnings.append("部分对白缺少合法的原视频时间位置")
    return VideoChunkEvidenceView(
        tenant_id=evidence.tenant_id, series_id=evidence.series_id,
        series_name=evidence.series_name, series_status=evidence.series_status,
        video_id=evidence.video_id, episode_order=evidence.episode_order,
        original_filename=evidence.original_filename, duration_seconds=evidence.duration_seconds,
        video_chunk_id=evidence.video_chunk_id, sequence_no=evidence.sequence_no,
        start_seconds=evidence.start_seconds, end_seconds=evidence.end_seconds,
        analysis_window_start_seconds=evidence.analysis_window_start_seconds,
        analysis_window_end_seconds=evidence.analysis_window_end_seconds,
        playback_status=playback.status, playback_url=playback.url,
        playback_url_expires_at=playback.expires_at,
        thumbnail=keyframes[0] if keyframes else None,
        keyframes=keyframes,
        transcript=transcript, baseline_visual_caption=evidence.baseline_visual_caption,
        all_time_events=events, all_time_event_text=evidence.all_time_event_text,
        video_compact=evidence.video_compact,
        context_before_compact=evidence.context_before_compact or None,
        context_after_compact=evidence.context_after_compact or None,
        window_summary=evidence.window_summary, is_fallback=evidence.is_fallback,
        model_provider=evidence.model_provider, model_name=evidence.model_name,
        pipeline_version=evidence.pipeline_version, parsed_at=evidence.parsed_at,
    ), list(dict.fromkeys(warnings))


def search_evidence_view(
    result: VideoChunkSearchResult | dict, media: MediaService, *, tenant_id: str,
) -> SearchEvidenceView:
    result = _validated_search_result(result)
    evidence = search_evidence_fields(result, media, tenant_id=tenant_id)
    return SearchEvidenceView(**evidence.model_dump(), query=result.query)


def search_evidence_fields(
    result: VideoChunkSearchEvidence | dict, media: MediaService, *, tenant_id: str,
) -> SearchEvidenceFieldsView:
    if isinstance(result, dict):
        result = VideoChunkSearchEvidence.model_validate({
            key: value for key, value in result.items()
            if key not in {
                "query", "renderBlocks", "render_blocks", "retrievalTrace", "retrieval_trace",
            }
        })
    warnings = list(getattr(result, "warnings", []))
    searched_video_ids = result.searched_video_ids
    if searched_video_ids is None:
        warnings.append("原始记录缺少检索视频范围")
    hits: list[SearchHitView] = []
    playback_by_video: dict[str, PlaybackMedia] = {}
    for rank, hit in enumerate(result.hits, start=1):
        source = hit.evidence
        if source.tenant_id != tenant_id or source.series_id not in result.series_ids:
            from ..repository.library import SeriesNotFoundError
            raise SeriesNotFoundError("片段资源不存在")
        if source.video_id not in playback_by_video:
            playback_by_video[source.video_id] = media.playback_view(source.video_id, tenant_id=tenant_id, video=source)
        evidence, limitations = evidence_view(source, media, tenant_id=tenant_id, playback=playback_by_video[source.video_id])
        warnings.extend(f"片段 {source.video_chunk_id}：{message}" for message in limitations)
        reason = hit.selection_reason.model_copy(update={
            "matched_evidence": [],
            "lexical_matches": [],
            "limitations": list(dict.fromkeys([*hit.selection_reason.limitations, *limitations])),
        })
        hits.append(SearchHitView(
            candidate=VideoChunkCandidateView(**hit.candidate.model_dump(), rank=rank),
            selection_reason=reason, evidence=evidence,
        ))
    return SearchEvidenceFieldsView(
        series_ids=result.series_ids,
        searched_video_ids=list(searched_video_ids or []),
        partial=bool(getattr(result, "partial", False) or searched_video_ids is None),
        warnings=list(dict.fromkeys(warnings)), hits=hits,
    )


def search_result_view(
    result: VideoChunkSearchResult | dict, media: MediaService, *, tenant_id: str,
) -> SearchResultView:
    result = _validated_search_result(result)
    evidence = search_evidence_view(result, media, tenant_id=tenant_id)
    return SearchResultView(
        **evidence.model_dump(), agent_reply=result.agent_reply,
    )


def _validated_search_result(
    result: VideoChunkSearchResult | dict,
) -> VideoChunkSearchResult:
    agent_only_fields = {
        "renderBlocks", "render_blocks", "retrievalTrace", "retrieval_trace",
    }
    if isinstance(result, dict) and (agent_only_fields & result.keys()):
        result = {
            key: value
            for key, value in result.items()
            if key not in agent_only_fields
        }
    return VideoChunkSearchResult.model_validate(result)
