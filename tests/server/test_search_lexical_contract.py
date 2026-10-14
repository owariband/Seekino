import asyncio
from dataclasses import asdict, replace
import json
from threading import Event
from types import SimpleNamespace

import pytest

from engine.rag.contracts import (
    AgentReply,
    LexicalMatch,
    MatchedEvidence,
    SelectionReason,
    TranscriptCueView,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from server.api import agent as agent_api
from server.api.search import search_result_view
from server.config.sse import SSEConfig
from server.model.dto.agentDTO import AgentMessageState
from server.model.dto.mediaDTO import PlaybackMedia


def _evidence() -> VideoChunkEvidence:
    return VideoChunkEvidence(
        series_id="series-1", tenant_id="workspace-1", series_name="测试系列",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id="video-1", episode_order=1,
        original_filename="episode.mp4", original_source_uri="source.mp4",
        inference_proxy_uri=None, content_hash="hash", duration_seconds=30_000,
        upload_status="COMPLETED", parse_status="SUCCEEDED",
        video_chunk_id="chunk-1", sequence_no=0, start_seconds=10_000, end_seconds=20_000,
        analysis_window_start_seconds=10_000, analysis_window_end_seconds=20_000,
        chunk_media_ref="chunk.mp4", analysis_window_media_ref="window.mp4",
        transcript=[TranscriptCueView(
            speaker="高松灯", text="我是高松灯。", start_seconds=11_000, end_seconds=13_000,
        )], baseline_visual_caption="高松灯向千早爱音介绍自己。", keyframe_uris=[],
        all_time_events=[], all_time_event_text="", retrieval_text="",
        video_compact="", context_before_compact="", context_after_compact="",
        window_summary="", model_provider=None, model_name=None,
        pipeline_version="v2", is_fallback=False, parse_provenance=None, parsed_at=0,
    )


def _result() -> VideoChunkSearchResult:
    return VideoChunkSearchResult(
        query="高松灯怎样向千早爱音介绍自己", series_ids=["series-1"], searched_video_ids=["video-1"],
        agent_reply=AgentReply(message="找到线索", result_count=1),
        hits=[VideoChunkSearchHit(
            candidate=VideoChunkCandidate(
                video_chunk_id="chunk-1", fused_rank=1, fused_score=0.1,
                lexical_rank=1, lexical_score=2.0,
            ),
            selection_reason=SelectionReason(
                summary="高松灯向千早爱音介绍自己。",
                matched_evidence=[MatchedEvidence(
                    kind="TRANSCRIPT", text="高松灯向千早爱音介绍自己。",
                    start_seconds=11_000, end_seconds=13_000,
                )], limitations=[],
                lexical_matches=[LexicalMatch(
                    field="questions", raw_text="高松灯怎样向千早爱音介绍自己？", language="zh",
                    start_seconds=10_000, end_seconds=20_000, enrichment_index=0,
                )],
            ),
            evidence=_evidence(),
        )],
    )


def _media() -> SimpleNamespace:
    return SimpleNamespace(
        playback_view=lambda *_args, **_kwargs: PlaybackMedia(status="READY", url="/video"),
        keyframe_views=lambda *_args, **_kwargs: [],
    )


def _assistant(result: dict) -> AgentMessageState:
    return AgentMessageState(
        message_id="assistant-1", conversation_id="conversation-1", sequence_no=1,
        role="ASSISTANT", status="COMPLETED", content="高松灯向千早爱音介绍自己。",
        series_ids=("series-1",), model_name="gpt-6-astra", search_result=result,
        last_error=None, created_at=0, updated_at=0,
    )


@pytest.mark.parametrize("projection", ["search", "agent"])
def test_public_projection_hides_retrieval_matches_without_mutating_internal_evidence(
    projection,
) -> None:
    result = _result()
    original = result.model_dump(mode="json", by_alias=True)
    raw = result.model_dump(mode="json", by_alias=True)
    if projection == "search":
        wire = search_result_view(
            result, _media(), tenant_id="workspace-1",
        ).model_dump(mode="json", by_alias=True)
    else:
        message = agent_api._agent_message_view(
            _assistant(raw), media=_media(), tenant_id="workspace-1",
        ).model_dump(mode="json", by_alias=True)
        wire = message["searchResult"]

    hit = wire["hits"][0]
    assert hit["candidate"]["lexicalRank"] == 1
    assert hit["candidate"]["transcriptRank"] is None
    assert hit["selectionReason"]["summary"] == result.hits[0].selection_reason.summary
    assert hit["selectionReason"]["lexicalMatches"] == []
    assert hit["selectionReason"]["matchedEvidence"] == []
    assert hit["evidence"]["transcript"] == original["hits"][0]["evidence"]["transcript"]
    assert hit["evidence"]["baselineVisualCaption"] == result.hits[0].evidence.baseline_visual_caption
    assert result.model_dump(mode="json", by_alias=True) == original
    assert raw == original


def test_answer_sse_projects_retrieval_matches_from_evidence_and_completed_messages(
    monkeypatch,
) -> None:
    raw = _result().model_dump(mode="json", by_alias=True)
    original = _result().model_dump(mode="json", by_alias=True)
    assistant = _assistant(raw)
    user = replace(
        assistant, message_id="user-1", sequence_no=0, role="USER",
        content="高松灯怎样向千早爱音介绍自己？", model_name=None, search_result=None,
    )
    common = {
        "conversationId": assistant.conversation_id, "messageId": assistant.message_id,
        "occurredAt": 0,
    }
    events = iter([
        {**common, "sequence": 1, "type": "answer.evidence", "searchResult": raw},
        {**common, "sequence": 2, "type": "answer.completed",
         "userMessage": asdict(user), "assistantMessage": asdict(assistant)},
        None,
    ])
    detached = []
    execution = SimpleNamespace(
        next_event=lambda _timeout: next(events), cancellation=Event(),
        detach=lambda: detached.append(True),
    )

    async def is_disconnected():
        return False

    monkeypatch.setattr(agent_api, "revalidate_identity", lambda _request: SimpleNamespace(mode="local"))
    monkeypatch.setattr(agent_api, "require_tenant_read", lambda *_args: None)

    async def collect():
        return [event async for event in agent_api._answer_events(
            SimpleNamespace(is_disconnected=is_disconnected), execution, SSEConfig(),
            media=_media(), tenant_id="workspace-1", series_ids=["series-1"],
        )]

    payloads = [json.loads(event.split("data: ", 1)[1]) for event in asyncio.run(collect())]
    assert [payload["type"] for payload in payloads] == ["answer.evidence", "answer.completed"]
    for evidence in (payloads[0]["searchResult"], payloads[1]["assistantMessage"]["searchResult"]):
        hit = evidence["hits"][0]
        assert hit["selectionReason"]["matchedEvidence"] == []
        assert hit["selectionReason"]["lexicalMatches"] == []
        assert hit["selectionReason"]["summary"] == original["hits"][0]["selectionReason"]["summary"]
        assert hit["evidence"]["transcript"] == original["hits"][0]["evidence"]["transcript"]
    assert payloads[1]["assistantMessage"]["content"] == assistant.content
    assert raw == original
    assert detached == [True]
