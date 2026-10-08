from dataclasses import replace
import logging

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import EncoderContract
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.indexing.projection import SourceEnrichment, project_chunk_index
from engine.rag.query_pipeline import plan_retrieval_query
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts,
    EventConstraint,
    GifQueryInput,
    ImageQueryInput,
    QueryEncoderBinding,
    RawChunkHit,
    RetrievalConsistencyError,
    RetrievalPolicy,
    RetrievalScope,
    SelectedVideoTask,
    VisualQueryFrame,
)
from engine.rag.retrieval.pipeline import fuse_retrieval_candidates
from engine.rag.retrieval.rerank import (
    RerankResult,
    RerankScorerContract,
    accept_rerank_result,
    finalize_candidate_rerank,
    plan_candidate_rerank,
)
from engine.rag.vision.inputs import (
    OrderedFrame,
    OrderedFrameSamplingContract,
    OrderedFramesManifest,
    VideoClipInputContract,
    VideoClipManifest,
)


def _query(kind):
    if kind == "text":
        return "找到这句原话"
    if kind == "image":
        return ImageQueryInput(input_sha256="a" * 64, mime_type="image/png", preprocess_revision="v1")
    return GifQueryInput(
        frames=tuple(VisualQueryFrame(input_sha256=digest * 64, at_seconds=at)
                     for digest, at in (("a", 0.0), ("b", 0.25), ("c", 0.75))),
        mime_type="image/png", preprocess_revision="v1",
    )


def _plan(*, kind="text", top_k=2, constraints=()):
    encoder = EncoderContract(provider="test", model="embedding", encoder_revision="v1", dimensions=2)
    contract = ChunkIndexingContract(
        retrieval_contract_id="test", semantic_encoder=encoder, vision_encoder=encoder,
        description_language="zh-Hans", visual_input_contract=VideoClipInputContract(
            fps=1.0, max_height=720, mime_type="video/mp4", preprocess_revision="v1",
        ),
    )
    scope = RetrievalScope(workspace_id="workspace", tasks=(SelectedVideoTask(
        series_id="series", video_id="video", parse_task_id="task", status="COMPLETE",
    ),))
    policy = RetrievalPolicy(
        lexical_weight=1.0 if kind == "text" else 0.0, semantic_weight=0.0,
        vision_weight=0.0 if kind == "text" else 1.0,
        channel_top_k=4, candidate_budget=4, top_k=top_k, rrf_k=60,
    )
    bindings = () if kind == "text" else (QueryEncoderBinding(
        channel="vision", query_modality=kind, document_encoder_id=contract.vision_encoder_id,
        query_encoder=encoder, compatibility_provenance="test visual query protocol",
    ),)
    return plan_retrieval_query(_query(kind), scope=scope, index_contract=contract,
                                policy=policy, encoder_bindings=bindings, constraints=constraints)


def _pool(plan, texts=("原话", "原话", "原话")):
    facts = tuple(ChunkRetrievalFacts(
        video_chunk_id=f"chunk-{rank}", sequence_no=rank - 1,
        retrieval_identity=VideoChunkRetrievalIdentity(
            workspace_id="workspace", series_id="series", video_id="video", parse_task_id="task",
            content_version=rank, index_generation=plan.index_contract.index_generation,
        ), start_seconds=(rank - 1) * 10, end_seconds=rank * 10,
        result=VideoParseResult.model_validate({"persons": ["人物"], "timeline": [{
            "event_type": "DIALOGUE", "start_seconds": (rank - 1) * 10 + 1,
            "end_seconds": (rank - 1) * 10 + 3, "description": f"片段{rank}的对白",
            "participants": ["人物"], "visible_persons": ["人物"], "speaker": "人物",
            "text_variants": [{"language": "zh-Hans", "role": "ORIGINAL", "text": text}],
        }]}), description_language="zh-Hans",
    ) for rank, text in enumerate(texts, 1))
    channel = "lexical" if isinstance(plan.query, str) else "vision"
    candidates = fuse_retrieval_candidates(plan, tuple(RawChunkHit(
        channel=channel, video_chunk_id=fact.video_chunk_id, retrieval_identity=fact.retrieval_identity,
        rank=rank, score=1.0 / rank, start_seconds=fact.start_seconds, end_seconds=fact.end_seconds,
    ) for rank, fact in enumerate(facts, 1)))
    return candidates, facts


def _scorer(**updates):
    values = dict(model="reranker", query_modalities=("text",), candidate_input="text",
                  protocol_provenance="test scoring input protocol")
    values.update(updates)
    return RerankScorerContract(**values)


def _ids(result):
    return tuple(item.candidate.ranking.video_chunk_id for item in result.accepted)


def _clip(fact):
    return VideoClipManifest(
        input_sha256="d" * 64, start_seconds=fact.start_seconds, end_seconds=fact.end_seconds,
        fps=2.0, max_height=480, mime_type="video/mp4", preprocess_revision="rerank-v1",
    )


def _frames(fact):
    return OrderedFramesManifest(
        frames=(OrderedFrame(ordinal=0, at_seconds=fact.start_seconds + 2.0, input_sha256="d" * 64),
                OrderedFrame(ordinal=1, at_seconds=fact.start_seconds + 7.0, input_sha256="e" * 64)),
        sampling_contract_id="rerank-sampling", mime_type="image/png", preprocess_revision="rerank-v1",
    )


def test_closed_rerank_keeps_full_evidence_pool_until_final_top_k():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts)
    assert plan.request is None
    assert len(plan.evidence.accepted) == 3
    assert _ids(finalize_candidate_rerank(plan)) == ("chunk-1", "chunk-2")


def test_text_request_uses_formal_projection_and_scores_can_select_tail_candidate():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    assert plan.request.query == query_plan.query
    assert len(plan.request.items) == 3
    for item, fact in zip(plan.request.items, facts):
        projection = project_chunk_index(fact.result, chunk_start_seconds=fact.start_seconds,
                                         chunk_end_seconds=fact.end_seconds, description_language="zh-Hans")
        assert item.text == projection.semantic_text
        assert item.visual_input is None
        assert item.candidate in plan.evidence.accepted
    result = accept_rerank_result(plan.request, model="reranker",
                                  scores=[("chunk-1", 0.1), ("chunk-2", 0.2), ("chunk-3", 0.9)])
    output = finalize_candidate_rerank(plan, result)
    assert _ids(output) == ("chunk-3", "chunk-2")
    assert output.accepted[0].candidate.ranking == candidates[2].ranking
    assert output.accepted[0].evidence == plan.evidence.accepted[2].evidence


def test_equal_scores_keep_rrf_order_even_when_score_pairs_arrive_reversed():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    result = accept_rerank_result(plan.request, model="reranker",
                                  scores=[("chunk-3", 1.0), ("chunk-2", 1.0), ("chunk-1", 1.0)])
    assert _ids(finalize_candidate_rerank(plan, result)) == ("chunk-1", "chunk-2")


def test_generated_questions_remain_recall_hints_and_are_not_scorer_text():
    query_plan = _plan()
    candidates, facts = _pool(query_plan, texts=("原话",))
    question = "假想问题不能成为正式内容"
    enriched = facts[0].model_copy(update={"enrichments": (SourceEnrichment(
        language="zh-Hans", questions=(question,),
    ),)})
    plan = plan_candidate_rerank(query_plan, candidates, (enriched,), scorer=_scorer())
    assert question not in plan.request.items[0].text
    assert "原话" in plan.request.items[0].text


def test_rerank_keeps_adjacent_hard_condition_proof_and_its_own_version():
    query_plan = _plan(constraints=(EventConstraint(
        key="quote", field="dialogue", text="原话", include_context=True,
    ),))
    candidates, facts = _pool(query_plan, texts=("不匹配", "原话"))
    plan = plan_candidate_rerank(query_plan, candidates[:1], facts[:1], scorer=_scorer(),
                                 contexts={"chunk-1": (facts[1],)})
    result = accept_rerank_result(plan.request, model="reranker", scores=(("chunk-1", 1.0),))
    output = finalize_candidate_rerank(plan, result)
    assert output.accepted == plan.evidence.accepted
    proof, = output.accepted[0].evidence
    assert proof.context == "after" and proof.video_chunk_id == "chunk-2"
    assert proof.retrieval_identity.content_version == 2
    assert (proof.start_seconds, proof.end_seconds) == (11, 13)


@pytest.mark.parametrize("scores", [
    [("chunk-1", 1.0), ("chunk-2", 0.5)],
    [("chunk-1", 1.0), ("chunk-1", 0.5), ("chunk-3", 0.2)],
    [("chunk-1", 1.0), ("chunk-2", 0.5), ("extra", 0.2)],
    [("chunk-1", float("nan")), ("chunk-2", 0.5), ("chunk-3", 0.2)],
    [("chunk-1", float("inf")), ("chunk-2", 0.5), ("chunk-3", 0.2)],
    [("chunk-1", True), ("chunk-2", 0.5), ("chunk-3", 0.2)],
    [("chunk-1", "1.0"), ("chunk-2", 0.5), ("chunk-3", 0.2)],
], ids=["missing", "duplicate", "extra", "nan", "inf", "bool", "numeric-string"])
def test_accept_rejects_incomplete_or_invalid_scores(scores):
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    with pytest.raises(ValueError):
        accept_rerank_result(plan.request, model="reranker", scores=scores)


def test_accept_requires_the_requested_model():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    with pytest.raises(ValueError, match="model"):
        accept_rerank_result(plan.request, model="another-model",
                             scores=[("chunk-1", 1.0), ("chunk-2", 0.5), ("chunk-3", 0.2)])


@pytest.mark.parametrize("failure", ["missing", "different-plan", "copied-plan", "malformed", "different-model"])
def test_missing_or_unusable_batch_warns_and_falls_back_as_a_whole(failure, caplog):
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    scores = (("chunk-1", 0.1), ("chunk-2", 0.2), ("chunk-3", 0.9))
    result = None
    if failure == "different-plan":
        other = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
        assert other.request is not plan.request
        result = accept_rerank_result(other.request, model="reranker", scores=scores)
    elif failure == "copied-plan":
        result = accept_rerank_result(plan.request, model="reranker", scores=scores)
        plan = replace(plan)
        assert result.request is not plan.request
    elif failure == "malformed":
        result = RerankResult(request=plan.request, model="reranker", scores=scores[:-1])
    elif failure == "different-model":
        result = RerankResult(request=plan.request, model="another-model", scores=scores)
    with caplog.at_level(logging.WARNING):
        output = finalize_candidate_rerank(plan, result)
    assert _ids(output) == ("chunk-1", "chunk-2")
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert query_plan.query not in caplog.text


def test_hard_rejected_candidate_is_not_sent_for_scoring_or_revived_by_a_score(caplog):
    query_plan = _plan(constraints=(EventConstraint(key="quote", field="dialogue", text="原话"),))
    candidates, facts = _pool(query_plan, texts=("原话", "其他句子", "原话"))
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    assert tuple(item.candidate.candidate.ranking.video_chunk_id for item in plan.request.items) == (
        "chunk-1", "chunk-3",
    )
    assert tuple(item.video_chunk_id for item in plan.evidence.rejected) == ("chunk-2",)
    invalid = RerankResult(request=plan.request, model="reranker", scores=(
        ("chunk-1", 0.1), ("chunk-2", 1000.0), ("chunk-3", 0.9),
    ))
    with caplog.at_level(logging.WARNING):
        output = finalize_candidate_rerank(plan, invalid)
    assert _ids(output) == ("chunk-1", "chunk-3")
    assert output.rejected == plan.evidence.rejected


def test_formal_fact_identity_error_still_raises_with_or_without_scorer():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    wrong = facts[0].model_copy(update={"retrieval_identity": facts[0].retrieval_identity.model_copy(
        update={"content_version": 999},
    )})
    for scorer in (None, _scorer()):
        with pytest.raises(RetrievalConsistencyError, match="version"):
            plan_candidate_rerank(query_plan, candidates, (wrong, *facts[1:]), scorer=scorer)


@pytest.mark.parametrize("kind", ["image", "gif"])
def test_text_scorer_cannot_claim_visual_query_support(kind):
    query_plan = _plan(kind=kind)
    candidates, facts = _pool(query_plan)
    with pytest.raises(ValueError):
        plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())


@pytest.mark.parametrize("kind", ["image", "gif"])
@pytest.mark.parametrize("candidate_input", ["video_clip", "ordered_frames"])
def test_visual_rerank_keeps_original_query_and_complete_candidate_media(kind, candidate_input):
    query_plan = _plan(kind=kind)
    candidates, facts = _pool(query_plan)
    factory = _clip if candidate_input == "video_clip" else _frames
    media = {fact.video_chunk_id: factory(fact) for fact in facts}
    plan = plan_candidate_rerank(query_plan, candidates, facts,
                                 scorer=_scorer(query_modalities=(kind,), candidate_input=candidate_input),
                                 visual_inputs=media)
    assert plan.request.query == query_plan.query
    if kind == "gif":
        assert tuple(frame.at_seconds for frame in plan.request.query.frames) == (0.0, 0.25, 0.75)
        assert tuple(frame.input_sha256 for frame in plan.request.query.frames) == ("a" * 64, "b" * 64, "c" * 64)
    for item, fact in zip(plan.request.items, facts):
        assert item.text is None
        assert item.visual_input == media[fact.video_chunk_id]
        if candidate_input == "ordered_frames":
            assert tuple(frame.at_seconds for frame in item.visual_input.frames) == (
                fact.start_seconds + 2.0, fact.start_seconds + 7.0,
            )
            assert tuple(frame.input_sha256 for frame in item.visual_input.frames) == ("d" * 64, "e" * 64)


@pytest.mark.parametrize("failure", ["missing", "wrong-kind", "outside-bounds"])
def test_visual_scorer_requires_complete_correctly_bounded_candidate_inputs(failure):
    query_plan = _plan(kind="image")
    candidates, facts = _pool(query_plan)
    media = {fact.video_chunk_id: _clip(fact) for fact in facts}
    if failure == "missing":
        media.pop("chunk-3")
    elif failure == "wrong-kind":
        media["chunk-1"] = _frames(facts[0])
    else:
        media["chunk-1"] = replace(media["chunk-1"], end_seconds=11)
    with pytest.raises(ValueError):
        plan_candidate_rerank(query_plan, candidates, facts,
                              scorer=_scorer(query_modalities=("image",), candidate_input="video_clip"),
                              visual_inputs=media)


def test_rerank_enforces_existing_candidate_budget_before_any_scoring():
    query_plan = _plan()
    candidates, facts = _pool(query_plan)
    with pytest.raises(RetrievalConsistencyError, match="budget"):
        plan_candidate_rerank(replace(query_plan, policy=query_plan.policy.model_copy(update={
            "candidate_budget": 2,
        })), candidates, facts, scorer=_scorer())


@pytest.mark.parametrize("updates", [
    {"model": " "}, {"protocol_provenance": " "}, {"query_modalities": ()},
    {"query_modalities": ("text", "text")}, {"query_modalities": ("audio",)},
    {"candidate_input": "image"},
])
def test_scorer_contract_requires_explicit_valid_capabilities(updates):
    with pytest.raises(ValueError):
        _scorer(**updates)


@pytest.mark.parametrize("failure", [None, "requested-time", "actual-end"])
def test_ordered_frames_obey_their_own_sampling_and_half_open_candidate_range(failure):
    query_plan = _plan(kind="gif")
    candidates, facts = _pool(query_plan, texts=("原话",))
    fact = facts[0]
    sampling = OrderedFrameSamplingContract(fps=0.2, max_frames_per_chunk=2)
    frames = (
        OrderedFrame(ordinal=0, at_seconds=2.0, input_sha256="d" * 64,
                     requested_at_seconds=3.0 if failure == "requested-time" else 2.5),
        OrderedFrame(ordinal=1, at_seconds=10.0 if failure == "actual-end" else 7.0,
                     input_sha256="e" * 64, requested_at_seconds=7.5),
    )
    manifest = OrderedFramesManifest(
        frames=frames, sampling_contract_id=sampling.sampling_contract_id,
        mime_type="image/png", preprocess_revision="rerank-v1", sampling=sampling, max_height=480,
    )
    scorer = _scorer(query_modalities=("gif",), candidate_input="ordered_frames")
    if failure:
        with pytest.raises(ValueError):
            plan_candidate_rerank(query_plan, candidates, facts, scorer=scorer,
                                  visual_inputs={fact.video_chunk_id: manifest})
    else:
        plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=scorer,
                                     visual_inputs={fact.video_chunk_id: manifest})
        assert plan.request.items[0].visual_input == manifest


def test_rerank_preserves_equal_realized_frame_times_allowed_by_existing_manifest():
    query_plan = _plan(kind="image")
    candidates, facts = _pool(query_plan, texts=("原话",))
    manifest = replace(_frames(facts[0]), frames=(
        OrderedFrame(ordinal=0, at_seconds=2.0, input_sha256="d" * 64),
        OrderedFrame(ordinal=1, at_seconds=2.0, input_sha256="e" * 64),
    ))
    plan = plan_candidate_rerank(query_plan, candidates, facts,
                                 scorer=_scorer(query_modalities=("image",), candidate_input="ordered_frames"),
                                 visual_inputs={facts[0].video_chunk_id: manifest})
    assert tuple(frame.at_seconds for frame in plan.request.items[0].visual_input.frames) == (2.0, 2.0)


def test_no_eligible_candidates_require_no_scorer_request_or_fallback_warning(caplog):
    query_plan = _plan(constraints=(EventConstraint(key="quote", field="dialogue", text="原话"),))
    candidates, facts = _pool(query_plan, texts=("别的句子",))
    plan = plan_candidate_rerank(query_plan, candidates, facts, scorer=_scorer())
    assert plan.request is None and plan.evidence.accepted == ()
    with caplog.at_level(logging.WARNING):
        output = finalize_candidate_rerank(plan)
    assert output.accepted == () and output.rejected == plan.evidence.rejected
    assert caplog.records == []
