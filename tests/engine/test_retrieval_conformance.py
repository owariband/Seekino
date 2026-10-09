"""Cross-module Engine contracts with injected model outputs, not retrieval quality."""

import logging

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.rag import (
    ChunkIndexSource, ChunkIndexingContract, ChunkRetrievalFacts, EncoderContract,
    EventConstraint, QueryEncoderBinding, RawChunkHit, RerankResult, RerankScorerContract,
    RetrievalConsistencyError, RetrievalPolicy, RetrievalScope, SelectedVideoTask,
    SparseEncoderContract, VideoClipInputContract, VideoClipManifest,
    accept_embedding_result, accept_query_vector, accept_rerank_result,
    accept_sparse_result, build_recall_requests, finalize_candidate_rerank,
    finalize_chunk_index, fuse_retrieval_candidates, plan_candidate_rerank,
    plan_chunk_index, plan_retrieval_query,
)
from engine.rag.contracts import VideoChunkRetrievalIdentity


@pytest.fixture
def source_to_recall():
    encoder = EncoderContract(
        provider="test", model="embedding", encoder_revision="v1", dimensions=2,
    )
    contract = ChunkIndexingContract(
        retrieval_contract_id="retrieval-v1", semantic_encoder=encoder,
        vision_encoder=encoder, index_generation=3, description_language="zh-Hans",
        visual_input_contract=VideoClipInputContract(
            fps=1.0, max_height=720, mime_type="video/mp4", preprocess_revision="v1",
        ), sparse_encoder=SparseEncoderContract(
            provider="test", model="sparse", encoder_revision="v1",
        ),
    )
    documents, facts = [], []
    for sequence, (chunk_id, quote) in enumerate((
        ("first", "我没事"), ("wrong-quote", "我不知道"), ("last", "我没事"),
    )):
        start, end, version = sequence * 10000, (sequence + 1) * 10000, sequence + 2
        result = VideoParseResult.model_validate({
            "persons": ["千早爱音"], "timeline": [{
                "event_type": "DIALOGUE", "start_seconds": start + 1000,
                "end_seconds": start + 3000, "description": "千早爱音回答",
                "participants": ["千早爱音"], "visible_persons": ["千早爱音"],
                "speaker": "千早爱音", "text_variants": [{
                    "language": "zh-Hans", "role": "ORIGINAL", "text": quote,
                }],
            }],
        })
        source = ChunkIndexSource(
            tenant_id="workspace", series_id="series", video_id="video",
            video_chunk_id=chunk_id, parse_task_id="task", content_version=version,
            chunk_start_seconds=start, chunk_end_seconds=end, result=result,
            visual_input=VideoClipManifest(
                input_sha256=chr(97 + sequence) * 64, start_seconds=start,
                end_seconds=end, fps=1.0, max_height=720, mime_type="video/mp4",
                preprocess_revision="v1",
            ),
        )
        index_plan = plan_chunk_index(source, contract)
        dense = tuple(accept_embedding_result(
            request, modality="text" if request.channel == "semantic" else "video",
            model=request.encoder.model, vector=[1.0, 0.0],
        ) for request in index_plan.encoding_requests)
        sparse = accept_sparse_result(
            index_plan.sparse_request, model="sparse", weights={"utterance": 0.5},
        )
        document = finalize_chunk_index(index_plan, dense, sparse_artifact=sparse)
        assert document.receipt.as_dict() == {
            "parse_task_id": "task", "video_chunk_id": chunk_id,
            "content_version": version, "index_generation": 3,
        }
        identity = VideoChunkRetrievalIdentity.model_validate_json(
            document.retrieval_identity.model_dump_json(by_alias=True),
        )
        assert identity == document.retrieval_identity
        documents.append(document)
        facts.append(ChunkRetrievalFacts(
            video_chunk_id=chunk_id, retrieval_identity=identity, sequence_no=sequence,
            start_seconds=start, end_seconds=end, result=result,
            description_language="zh-Hans",
        ))
    query = plan_retrieval_query(
        "千早爱音说我没事", index_contract=contract,
        scope=RetrievalScope(tenant_id="workspace", tasks=(SelectedVideoTask(
            series_id="series", video_id="video", parse_task_id="task", status="COMPLETE",
        ),)), policy=RetrievalPolicy(
            lexical_weight=1.0, semantic_weight=1.0, vision_weight=1.0, sparse_weight=1.0,
            channel_top_k=3, candidate_budget=3, top_k=1, rrf_k=60,
        ), encoder_bindings=tuple(QueryEncoderBinding(
            channel=channel, document_encoder_id=getattr(contract, f"{channel}_encoder_id"),
            query_encoder=encoder, compatibility_provenance="injected test output-space proof",
        ) for channel in ("semantic", "vision")), constraints=(EventConstraint(
            key="quote", field="dialogue", text="我没事", speaker="千早爱音",
        ),),
    )
    vectors = tuple(accept_query_vector(
        request, model="embedding", vector=[1.0, 0.0],
    ) for request in query.encoding_requests)
    sparse_query = accept_sparse_result(query.sparse_request, model="sparse", weights={"utterance": 0.5})
    recalls = build_recall_requests(query, vectors=vectors, sparse=sparse_query)
    assert {request.channel for request in recalls} == {"lexical", "semantic", "vision", "sparse"}
    hits = [RawChunkHit(
        channel=request.channel, video_chunk_id=document.video_chunk_id,
        retrieval_identity=document.retrieval_identity, rank=rank, score=1.0 / rank,
        start_seconds=document.chunk_start_seconds, end_seconds=document.chunk_end_seconds,
        lexical_matches=tuple(field for field in document.projection.lexical_fields if field.field == "dialogue")
        if request.channel == "lexical" else (),
    ) for rank, document in enumerate(documents, 1) for request in recalls]
    hits = [RawChunkHit.model_validate_json(hit.model_dump_json(by_alias=True)) for hit in hits]
    candidates = fuse_retrieval_candidates(query, hits)
    assert [candidate.ranking.video_chunk_id for candidate in candidates] == ["first", "wrong-quote", "last"]
    assert [candidate.ranking.fused_score for candidate in candidates] == pytest.approx([1 / 61, 1 / 62, 1 / 63])
    return query, candidates, facts, hits, documents


def _scorer():
    return RerankScorerContract(
        model="scorer", query_modalities=("text",), candidate_input="text",
        protocol_provenance="injected test scorer protocol",
    )


def test_source_index_recall_and_rerank_preserve_identity_and_formal_conditions(source_to_recall):
    query, candidates, facts, _, documents = source_to_recall
    plan = plan_candidate_rerank(query, candidates, facts, scorer=_scorer())
    assert plan.request.query == query.query
    assert [item.candidate.candidate.ranking.video_chunk_id for item in plan.request.items] == ["first", "last"]
    scores = accept_rerank_result(plan.request, model="scorer", scores=[("first", 0.25), ("last", 0.95)])
    output = finalize_candidate_rerank(plan, scores)
    assert len(output.accepted) == 1
    selected = output.accepted[0]
    assert selected.candidate.ranking.video_chunk_id == "last"
    assert selected.candidate.ranking.fused_rank == 3
    assert selected.candidate.ranking.retrieval_identity == documents[2].retrieval_identity
    assert output.rejected_chunk_ids == ("wrong-quote",)
    assert selected.evidence[0].source.raw_text == "我没事"
    assert (selected.evidence[0].start_seconds, selected.evidence[0].end_seconds) == (21000, 23000)


@pytest.mark.parametrize("field,value", [("content_version", 99), ("parse_task_id", "old-task"), ("index_generation", 2)])
def test_scorer_cannot_bypass_inconsistent_formal_facts(source_to_recall, field, value):
    query, candidates, facts, _, _ = source_to_recall
    identity = facts[0].retrieval_identity.model_copy(update={field: value})
    changed = facts[0].model_copy(update={"retrieval_identity": identity})
    with pytest.raises(RetrievalConsistencyError):
        plan_candidate_rerank(query, candidates, [changed, *facts[1:]], scorer=_scorer())


def test_cross_channel_version_conflict_fails_before_a_scorer_can_rank_it(source_to_recall):
    query, _, _, hits, _ = source_to_recall
    vision = next(hit for hit in hits if hit.channel == "vision")
    changed = vision.model_copy(update={"retrieval_identity": vision.retrieval_identity.model_copy(
        update={"content_version": vision.retrieval_identity.content_version + 1},
    )})
    with pytest.raises(RetrievalConsistencyError):
        fuse_retrieval_candidates(query, [changed if hit is vision else hit for hit in hits])


@pytest.mark.parametrize("response", ["missing", "unrequested-candidate"])
def test_scorer_failure_warns_and_falls_back_to_the_verified_rrf_order(source_to_recall, caplog, response):
    query, candidates, facts, _, _ = source_to_recall
    plan = plan_candidate_rerank(query, candidates, facts, scorer=_scorer())
    with caplog.at_level(logging.WARNING):
        result = None if response == "missing" else RerankResult(
            request=plan.request, model="scorer", scores=(("first", 0.25), ("last", 0.95), ("wrong-quote", 100.0)),
        )
        output = finalize_candidate_rerank(plan, result)
    assert [item.candidate.ranking.video_chunk_id for item in output.accepted] == ["first"]
    assert output.rejected_chunk_ids == ("wrong-quote",)
    assert output.accepted[0].evidence[0].source.raw_text == "我没事"
    assert any(record.levelno == logging.WARNING for record in caplog.records)
