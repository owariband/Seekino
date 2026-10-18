from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity, VideoChunkSearchResult
from engine.rag.encoding import EncoderContract
from engine.rag.indexing.events import EventIndexingContract, compute_event_id, plan_event_index
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts, EventFilters, EventSearchResult, RawEventHit, RetrievalConsistencyError,
)
from engine.rag.retrieval.events import fuse_event_hits, select_event_chunk_hits, verify_event_candidate
from engine.rag.vision.event_frames import EventFrameContract


def _setup():
    encoder = EncoderContract("test", "embedding", "v1", 2)
    contract = EventIndexingContract(semantic_encoder=encoder, vision_encoder=encoder, capture=EventFrameContract(max_height=720))
    result = VideoParseResult.model_validate(dict(persons=["A"], timeline=[
        dict(event_type="DIALOGUE", start_seconds=1000, end_seconds=2000, description="A回答", participants=["A"],
             visible_persons=[], speaker="A", text_variants=[dict(language="ja", role="ORIGINAL", text="いいご身分ですわね。")]),
        dict(event_type="RELATION_CHANGE", start_seconds=2000, end_seconds=3000, description="达成约定", participants=["A"],visible_persons=[]),
    ]))
    plans = plan_event_index("task", "chunk", 2, result, 0, 4000, contract)
    facts = ChunkRetrievalFacts(video_chunk_id="chunk", retrieval_identity=VideoChunkRetrievalIdentity(
        tenant_id="tenant", series_id="series", video_id="video", parse_task_id="task", content_version=2,index_generation=17),
        sequence_no=0,start_seconds=0,end_seconds=4000,result=result)
    return contract, plans, facts


def _raw(plan, channel="lexical", rank=1, score=1.):
    return RawEventHit(channel=channel, event_id=plan.event_id, parse_task_id=plan.parse_task_id,
                       video_chunk_id=plan.video_chunk_id,version=plan.version,timeline_index=plan.timeline_index,
                       event_type=plan.event_type,speaker=plan.speaker,rank=rank,score=score,lexical_fields=plan.lexical_fields,
                       matched_fields=plan.lexical_fields[:1] if channel == "lexical" else ())


def test_speaker_filter_implies_dialogue_and_rejects_explicit_conflicts():
    assert EventFilters(speaker="A").event_types == ("DIALOGUE",)
    assert EventFilters().event_types == ()
    assert EventFilters(event_types=["ACTION", "EMOTION"]).event_types == ("ACTION", "EMOTION")
    with pytest.raises(ValidationError, match="only DIALOGUE"):
        EventFilters(event_types=["DIALOGUE", "ACTION"],speaker="A")


def test_event_fusion_keeps_channel_fields_and_every_event_before_chunk_limit():
    _contract, plans, _facts = _setup()
    hits = [_raw(plans[0]), _raw(plans[1], rank=2), _raw(plans[0], "semantic", 1, 0.8)]
    fused = fuse_event_hits(hits,channel_weights={"lexical":1.,"semantic":1.,"vision":0.})
    assert len(fused) == 2
    assert fused[0].channel_ranks == {"lexical":1,"semantic":1}
    assert fused[0].matched_fields == plans[0].lexical_fields[:1]
    assert len(select_event_chunk_hits(fused,top_k=1)) == 2
    assert len(select_event_chunk_hits(fused,top_k=1)[0].lexical_fields) == len(plans[0].lexical_fields)


def test_fusion_rejects_stale_ids_cross_channel_content_and_duplicate_ranks():
    _contract, plans, _facts = _setup()
    hit = _raw(plans[0])
    for bad in (hit.model_copy(update={"event_id":"stale"}),hit.model_copy(update={"version":3})):
        with pytest.raises(RetrievalConsistencyError, match="source tuple"):
            fuse_event_hits([bad],channel_weights={"lexical":1.})
    with pytest.raises(RetrievalConsistencyError, match="Duplicate"):
        fuse_event_hits([hit,hit],channel_weights={"lexical":1.})
    semantic = _raw(plans[0],"semantic").model_copy(update={"speaker":"B"})
    with pytest.raises(RetrievalConsistencyError, match="disagree"):
        fuse_event_hits([hit,semantic],channel_weights={"lexical":1.,"semantic":1.})


def test_formal_verification_uses_source_version_not_chunk_index_generation():
    contract, plans, facts = _setup()
    hit = fuse_event_hits([_raw(plans[0])],channel_weights={"lexical":1.})[0]
    verified = verify_event_candidate(hit,facts,contract=contract)
    assert (verified.start_ms,verified.end_ms) == (1000,2000)
    assert facts.retrieval_identity.index_generation == 17
    with pytest.raises(RetrievalConsistencyError, match="source"):
        verify_event_candidate(hit.model_copy(update={"version":3}),facts,contract=contract)
    with pytest.raises(RetrievalConsistencyError, match="Timeline"):
        verify_event_candidate(hit.model_copy(update={"lexical_fields":(),"matched_fields":()}),facts,contract=contract)


def test_event_search_result_retains_complete_trace_without_candidate_truncation():
    _contract, plans, _facts = _setup()
    matches = fuse_event_hits([_raw(plans[0]),_raw(plans[1],rank=2)],channel_weights={"lexical":1.})
    chunks = VideoChunkSearchResult.model_validate(dict(seriesIds=["series"],query="question",
        agentReply=dict(message="没有命中",resultCount=0),hits=[],partial=False,warnings=[]))
    trace = {"raw_hits":[match.model_dump(mode="json") for match in matches],"filtered":[{"reason":"changed","id":"old"}]}
    result = EventSearchResult(chunk_result=chunks,trace=trace)
    assert EventSearchResult.model_validate_json(result.model_dump_json()).trace == trace
    with pytest.raises(ValidationError,match="returned Chunk evidence"):
        EventSearchResult(chunk_result=chunks,matches=matches,trace=trace)
