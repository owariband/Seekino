from types import SimpleNamespace

from engine.rag.contracts import (
    AgentReply,
    LexicalMatch,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from server.api.search import search_evidence_view
from server.model.dto.mediaDTO import PlaybackMedia


def _evidence() -> VideoChunkEvidence:
    return VideoChunkEvidence(
        series_id="series-1", tenant_id="workspace-1", series_name="测试系列",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id="video-1", episode_order=1,
        original_filename="episode.mp4", original_source_uri="source.mp4",
        inference_proxy_uri=None, content_hash="hash", duration_seconds=30,
        upload_status="COMPLETED", parse_status="SUCCEEDED",
        video_chunk_id="chunk-1", sequence_no=0, start_seconds=10, end_seconds=20,
        analysis_window_start_seconds=10, analysis_window_end_seconds=20,
        chunk_media_ref="chunk.mp4", analysis_window_media_ref="window.mp4",
        transcript=[], baseline_visual_caption=None, keyframe_uris=[],
        all_time_events=[], all_time_event_text="", retrieval_text="",
        video_compact="", context_before_compact="", context_after_compact="",
        window_summary="", model_provider=None, model_name=None,
        pipeline_version="v2", is_fallback=False, parse_provenance=None, parsed_at=0,
    )


def test_search_api_preserves_lexical_source_separate_from_factual_evidence() -> None:
    result = VideoChunkSearchResult(
        query="谁在门口等待", series_ids=["series-1"], searched_video_ids=["video-1"],
        agent_reply=AgentReply(message="找到线索", result_count=1),
        hits=[VideoChunkSearchHit(
            candidate=VideoChunkCandidate(
                video_chunk_id="chunk-1", fused_rank=1, fused_score=0.1,
                lexical_rank=1, lexical_score=2.0,
            ),
            selection_reason=SelectionReason(
                summary="问题词项命中，等待事实核验。", matched_evidence=[], limitations=[],
                lexical_matches=[LexicalMatch(
                    field="questions", raw_text="谁在门口等待？", language="zh",
                    start_seconds=10, end_seconds=20, enrichment_index=0,
                )],
            ),
            evidence=_evidence(),
        )],
    )
    media = SimpleNamespace(
        playback_view=lambda *_args, **_kwargs: PlaybackMedia(status="READY", url="/video"),
        keyframe_views=lambda *_args, **_kwargs: [],
    )

    wire = search_evidence_view(
        result, media, tenant_id="workspace-1",
    ).model_dump(mode="json", by_alias=True)

    hit = wire["hits"][0]
    assert hit["candidate"]["lexicalRank"] == 1
    assert hit["candidate"]["transcriptRank"] is None
    assert hit["selectionReason"]["lexicalMatches"][0]["field"] == "questions"
    assert hit["selectionReason"]["lexicalMatches"][0]["enrichmentIndex"] == 0
    assert hit["selectionReason"]["matchedEvidence"] == []
