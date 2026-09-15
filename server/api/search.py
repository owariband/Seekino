from __future__ import annotations

from fastapi import Request

from .context import get_services
from ..model.dto.mediaDTO import PlaybackMedia
from ..model.dto.searchDTO import VideoSearchRequest
from ..model.vo.searchVO import (SearchResultView, VideoChunkEvidenceView, VisualEventView, SearchHitView, VideoChunkCandidateView)
from ..schema import VideoChunkEvidence, VideoChunkSearchResult
from ..service.media import MediaService


def search_video_chunks(
    body: VideoSearchRequest,
    request: Request,
) -> SearchResultView:
    services = get_services(request)
    result = services.retrieval.search(
        body.query,
        workspace_id=body.workspace_id,
        series_ids=body.series_ids,
        top_k=body.top_k,
    )
    return search_result_view(result, services.media, workspace_id=body.workspace_id)


def evidence_view(
    evidence: VideoChunkEvidence, media: MediaService, *, workspace_id: str,
    playback: PlaybackMedia | None = None,
) -> tuple[VideoChunkEvidenceView, list[str]]:
    if evidence.workspace_id != workspace_id:
        from ..repository.library import SeriesNotFoundError
        raise SeriesNotFoundError("片段资源不存在")
    playback = playback or media.playback_view(evidence.video_id, workspace_id=workspace_id, video=evidence)
    warnings: list[str] = []
    if playback.status != "READY":
        warnings.append("原视频文件已丢失" if playback.status == "MISSING" else "尚无可确认兼容浏览器的播放媒体")
    # SQL currently records frame URIs without their original-video offsets.
    # An arbitrary URI or guessed time cannot become a public Keyframe.
    warnings.append("缺少带原视频时间位置的已登记关键帧")
    events: list[VisualEventView] = []
    for item in evidence.all_time_events:
        start = item.get("start_ms", item.get("startMs"))
        end = item.get("end_ms", item.get("endMs"))
        description = item.get("description")
        if (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and 0 <= start < end <= evidence.duration_ms
            and isinstance(description, str) and description.strip()):
            events.append(VisualEventView(description=description, start_ms=start, end_ms=end))
        else:
            warnings.append("部分视觉事件缺少合法的原视频时间位置，已保留文字摘要")
    transcript = [cue for cue in evidence.transcript if 0 <= cue.start_ms < cue.end_ms <= evidence.duration_ms]
    if len(transcript) != len(evidence.transcript):
        warnings.append("部分对白缺少合法的原视频时间位置")
    return VideoChunkEvidenceView(
        workspace_id=evidence.workspace_id, series_id=evidence.series_id,
        series_name=evidence.series_name, series_status=evidence.series_status,
        video_id=evidence.video_id, episode_order=evidence.episode_order,
        original_filename=evidence.original_filename, duration_ms=evidence.duration_ms,
        video_chunk_id=evidence.video_chunk_id, sequence_no=evidence.sequence_no,
        start_ms=evidence.start_ms, end_ms=evidence.end_ms,
        analysis_window_start_ms=evidence.analysis_window_start_ms,
        analysis_window_end_ms=evidence.analysis_window_end_ms,
        playback_status=playback.status, playback_url=playback.url,
        playback_url_expires_at=playback.expires_at, thumbnail=None, keyframes=[],
        transcript=transcript, baseline_visual_caption=evidence.baseline_visual_caption,
        all_time_events=events, all_time_event_text=evidence.all_time_event_text,
        video_compact=evidence.video_compact,
        context_before_compact=evidence.context_before_compact or None,
        context_after_compact=evidence.context_after_compact or None,
        window_summary=evidence.window_summary, is_fallback=evidence.is_fallback,
        model_provider=evidence.model_provider, model_name=evidence.model_name,
        pipeline_version=evidence.pipeline_version, parsed_at=evidence.parsed_at,
    ), list(dict.fromkeys(warnings))


def search_result_view(
    result: VideoChunkSearchResult | dict, media: MediaService, *, workspace_id: str,
) -> SearchResultView:
    result = VideoChunkSearchResult.model_validate(result)
    warnings = list(getattr(result, "warnings", []))
    searched_video_ids = result.searched_video_ids
    if searched_video_ids is None:
        warnings.append("原始记录缺少检索视频范围")
    hits: list[SearchHitView] = []
    playback_by_video: dict[str, PlaybackMedia] = {}
    for rank, hit in enumerate(result.hits, start=1):
        source = hit.evidence
        if source.workspace_id != workspace_id or source.series_id not in result.series_ids:
            from ..repository.library import SeriesNotFoundError
            raise SeriesNotFoundError("片段资源不存在")
        if source.video_id not in playback_by_video:
            playback_by_video[source.video_id] = media.playback_view(source.video_id, workspace_id=workspace_id, video=source)
        evidence, limitations = evidence_view(source, media, workspace_id=workspace_id, playback=playback_by_video[source.video_id])
        warnings.extend(f"片段 {source.video_chunk_id}：{message}" for message in limitations)
        reason = hit.selection_reason.model_copy(update={
            "limitations": list(dict.fromkeys([*hit.selection_reason.limitations, *limitations])),
        })
        hits.append(SearchHitView(
            candidate=VideoChunkCandidateView(**hit.candidate.model_dump(), rank=rank),
            selection_reason=reason, evidence=evidence,
        ))
    return SearchResultView(
        query=result.query, series_ids=result.series_ids,
        searched_video_ids=list(searched_video_ids or []),
        partial=bool(getattr(result, "partial", False) or warnings),
        warnings=list(dict.fromkeys(warnings)), agent_reply=result.agent_reply, hits=hits,
    )
