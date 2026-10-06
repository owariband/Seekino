from dataclasses import replace

import pytest
from pydantic import ValidationError

from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import embedding_receipt
from engine.rag.indexing.pipeline import (
    ChunkIndexingContract,
    compute_source_revision,
)
from engine.rag.encoding import (
    EncoderContract,
    SparseEncoderContract,
    accept_sparse_result,
)
from engine.rag.vision.inputs import VideoClipInputContract
from engine.rag.indexing.projection import (
    SourceEnrichment, compute_index_projection_revision, project_chunk_index,
)
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts,
    EventConstraint,
    QueryEncoderBinding,
    QueryVectorArtifact,
    RawChunkHit,
    RetrievalConsistencyError,
    RetrievalPolicy,
    RetrievalScope,
    SelectedVideoTask,
    TemporalConstraint,
)
from engine.rag.nlp.query import (
    accept_query_vector,
    reuse_query_vector,
)
from engine.rag.retrieval.pipeline import (
    build_recall_requests,
    fuse_retrieval_candidates,
)
from engine.rag.query_pipeline import plan_retrieval_query
from engine.rag.retrieval.evidence import verify_retrieval_evidence


def _encoder(model="embedding"):
    return EncoderContract(provider="test", model=model, encoder_revision="v1", dimensions=2)


def _contract(sparse=False):
    return ChunkIndexingContract(
        retrieval_contract_id="retrieval-v1", semantic_encoder=_encoder(),
        vision_encoder=_encoder(), description_language="zh-Hans",
        visual_input_contract=VideoClipInputContract(
            fps=1.0, max_height=720, mime_type="video/mp4", preprocess_revision="v1",
        ), sparse_encoder=SparseEncoderContract(
            provider="test", model="sparse", encoder_revision="v1",
        ) if sparse else None,
    )


def _scope(**updates):
    values = dict(series_id="series", video_id="video", parse_task_id="task", status="COMPLETE")
    values.update(updates)
    return RetrievalScope(workspace_id="workspace", tasks=(SelectedVideoTask(**values),))


def _policy(**updates):
    values = dict(lexical_weight=1.0, semantic_weight=1.0, vision_weight=1.0,
                  channel_top_k=5, candidate_budget=5, top_k=2, rrf_k=60)
    values.update(updates)
    return RetrievalPolicy(**values)


def _bindings(contract, policy, vision_encoder=None):
    return tuple(QueryEncoderBinding(
        channel=channel, document_encoder_id=getattr(contract, f"{channel}_encoder_id"),
        query_encoder=vision_encoder if channel == "vision" and vision_encoder else _encoder(),
        compatibility_provenance="test protocol output-space proof",
    ) for channel in ("semantic", "vision") if policy.weights[channel])


def _plan(*, contract=None, policy=None, constraints=(), temporal=(), scope=None):
    contract, policy = contract or _contract(), policy or _policy()
    return plan_retrieval_query("我没事", scope=scope or _scope(), index_contract=contract,
                                policy=policy, encoder_bindings=_bindings(contract, policy),
                                constraints=constraints, temporal_constraints=temporal)


def _event(text="我没事", start=1, end=3, speaker="千早爱音", role="ORIGINAL"):
    return dict(event_type="DIALOGUE", start_seconds=start, end_seconds=end,
                description=f"{speaker}回答", participants=[speaker], visible_persons=[speaker],
                speaker=speaker, text_variants=[dict(language="zh-Hans", role=role, text=text)])


def _fact(plan, *, chunk="chunk", sequence=0, bounds=(0, 10), version=2,
          events=None, enrichments=()):
    result = VideoParseResult.model_validate({
        "persons": ["千早爱音", "高松灯"], "timeline": events or [_event()],
    })
    projection = project_chunk_index(result, chunk_start_seconds=bounds[0], chunk_end_seconds=bounds[1],
                                     description_language="zh-Hans", enrichments=enrichments)
    contract = plan.index_contract
    identity = VideoChunkRetrievalIdentity(
        workspace_id="workspace", series_id="series", video_id="video", parse_task_id="task",
        content_version=version, index_generation=contract.index_generation,
        source_revision=compute_source_revision(result),
        projection_revision=compute_index_projection_revision(projection),
        retrieval_contract_id=contract.retrieval_contract_id,
        semantic_encoder_id=contract.semantic_encoder_id, vision_encoder_id=contract.vision_encoder_id,
        sparse_encoder_id=contract.sparse_encoder.encoder_id if contract.sparse_encoder else None,
    )
    return ChunkRetrievalFacts(video_chunk_id=chunk, retrieval_identity=identity, sequence_no=sequence,
                               start_seconds=bounds[0], end_seconds=bounds[1], result=result,
                               description_language="zh-Hans", enrichments=enrichments)


def _projection(fact):
    return project_chunk_index(fact.result, chunk_start_seconds=fact.start_seconds,
                               chunk_end_seconds=fact.end_seconds, description_language=fact.description_language,
                               enrichments=fact.enrichments)


def _hit(fact, channel="semantic", rank=1, matches=()):
    return RawChunkHit(channel=channel, video_chunk_id=fact.video_chunk_id,
                       retrieval_identity=fact.retrieval_identity, rank=rank, score=0.8,
                       start_seconds=fact.start_seconds, end_seconds=fact.end_seconds,
                       lexical_matches=matches)


def _verify(plan, fact, *, channel="semantic", matches=(), contexts=None):
    candidates = fuse_retrieval_candidates(plan, [_hit(fact, channel, matches=matches)])
    return verify_retrieval_evidence(plan, candidates, [fact], contexts=contexts)


def test_text_pipeline_shares_compatible_encoding_and_preserves_formal_event():
    plan = _plan(constraints=[EventConstraint(key="quote", field="dialogue", text="我没事", speaker="千早爱音")])
    assert len(plan.encoding_requests) == 1
    assert plan.encoding_requests[0].channels == ("semantic", "vision")
    vector = accept_query_vector(plan.encoding_requests[0], model="embedding", vector=[1, 0])
    requests = build_recall_requests(plan, vectors=[vector])
    assert [request.channel for request in requests] == ["lexical", "semantic", "vision"]
    assert requests[1].vector == requests[2].vector == (1.0, 0.0)
    assert requests[1].document_encoder_id != requests[2].document_encoder_id
    fact = _fact(plan)
    dialogue = next(field for field in _projection(fact).lexical_fields if field.field == "dialogue")
    candidates = fuse_retrieval_candidates(plan, [_hit(fact), _hit(fact, "vision"),
                                                  _hit(fact, "lexical", matches=[dialogue])])
    output = verify_retrieval_evidence(plan, candidates, [fact])
    assert output.accepted[0].candidate.ranking.lexical_rank == 1
    proof = output.accepted[0].evidence[0]
    assert proof.source.speaker == "千早爱音"
    assert proof.retrieval_identity == fact.retrieval_identity
    assert (proof.start_seconds, proof.end_seconds) == (1, 3)


def test_distinct_query_encoders_require_two_calls():
    contract, policy = _contract(), _policy()
    plan = plan_retrieval_query("query", scope=_scope(), index_contract=contract, policy=policy,
                                encoder_bindings=_bindings(contract, policy, _encoder("visual-query")))
    assert [request.channels for request in plan.encoding_requests] == [("semantic",), ("vision",)]
    with pytest.raises(ValueError, match="missing"):
        build_recall_requests(plan, vectors=[accept_query_vector(plan.encoding_requests[0], model="embedding", vector=[1, 0])])


def test_missing_or_wrong_compatibility_binding_is_rejected():
    contract, policy = _contract(), _policy()
    bindings = _bindings(contract, policy)
    with pytest.raises(ValueError, match="compatibility"):
        plan_retrieval_query("query", scope=_scope(), index_contract=contract, policy=policy)
    wrong = bindings[0].model_copy(update={"document_encoder_id": "wrong"})
    with pytest.raises(ValueError, match="different document"):
        plan_retrieval_query("query", scope=_scope(), index_contract=contract, policy=policy,
                            encoder_bindings=[wrong, bindings[1]])
    with pytest.raises(ValidationError):
        QueryEncoderBinding(channel="semantic", document_encoder_id="encoder", query_encoder=_encoder(),
                            compatibility_provenance=" ")


@pytest.mark.parametrize("model,vector", [("wrong", [1, 0]), ("embedding", [1]),
    ("embedding", [0, 0]), ("embedding", [float("nan"), 1]), ("embedding", [True, 1])])
def test_invalid_model_vectors_cannot_be_accepted(model, vector):
    with pytest.raises(ValueError):
        accept_query_vector(_plan().encoding_requests[0], model=model, vector=vector)


def test_stale_and_duplicate_vectors_rejected_and_compatible_receipt_reused():
    plan = _plan()
    request = plan.encoding_requests[0]
    artifact = accept_query_vector(request, model="embedding", vector=[1, 0])
    stale = artifact.model_copy(update={"input_sha256": "old"})
    with pytest.raises(ValueError, match="missing"):
        build_recall_requests(plan, vectors=[stale])
    with pytest.raises(ValueError, match="duplicate"):
        build_recall_requests(plan, vectors=[artifact, artifact])
    receipt = embedding_receipt(request.target, [1, 0])
    assert reuse_query_vector(request, source_kind="v2_index_cache", metadata=receipt, vector=[1, 0]) == artifact
    assert reuse_query_vector(request, source_kind="v2_index_cache", metadata={**receipt, "input_sha256": "old"}, vector=[1, 0]) is None


def test_sparse_off_requires_no_output_and_on_reaches_request_and_fusion():
    assert _plan().sparse_request is None
    plan = _plan(contract=_contract(True), policy=_policy(sparse_weight=1.0))
    vector = accept_query_vector(plan.encoding_requests[0], model="embedding", vector=[1, 0])
    sparse = accept_sparse_result(plan.sparse_request, model="sparse", weights={"123": 0.3, "empty": 0})
    with pytest.raises(ValueError, match="Sparse.*missing"):
        build_recall_requests(plan, vectors=[vector])
    requests = build_recall_requests(plan, vectors=[vector], sparse=sparse)
    assert requests[-1].token_weights == (("123", 0.3),)
    assert requests[-1].document_encoder_id == plan.index_contract.sparse_encoder.encoder_id
    fact = _fact(plan)
    candidates = fuse_retrieval_candidates(plan, [_hit(fact), _hit(fact, "sparse")])
    assert candidates[0].ranking.sparse_rank == 1
    assert candidates[0].ranking.sparse_score == 0.8
    off = _plan()
    with pytest.raises(ValueError, match="unplanned Sparse"):
        build_recall_requests(off, vectors=[vector], sparse=sparse)


def test_empty_scope_needs_no_paid_encodings_and_non_complete_is_rejected():
    plan = _plan(scope=RetrievalScope(workspace_id="workspace", tasks=()))
    assert plan.encoding_requests == ()
    assert build_recall_requests(plan) == ()
    assert fuse_retrieval_candidates(plan, []) == ()
    with pytest.raises(ValidationError):
        _scope(status="RUNNING")


@pytest.mark.parametrize("changes", [{"parse_task_id": "old-task"}, {"index_generation": 2},
                                     {"workspace_id": "other"}, {"semantic_encoder_id": "old"}])
def test_raw_hits_outside_pinned_scope_fail(changes):
    plan = _plan()
    hit = _hit(_fact(plan))
    hit = hit.model_copy(update={"retrieval_identity": hit.retrieval_identity.model_copy(update=changes)})
    with pytest.raises(RetrievalConsistencyError, match="scope"):
        fuse_retrieval_candidates(plan, [hit])


def test_duplicate_ranks_cross_channel_version_and_disabled_route_fail():
    plan = _plan()
    first, second = _fact(plan), _fact(plan, chunk="other")
    with pytest.raises(RetrievalConsistencyError, match="duplicate"):
        fuse_retrieval_candidates(plan, [_hit(first), _hit(second)])
    vision = _hit(first, "vision")
    vision = vision.model_copy(update={"retrieval_identity": vision.retrieval_identity.model_copy(update={"content_version": 3})})
    with pytest.raises(RetrievalConsistencyError, match="disagree"):
        fuse_retrieval_candidates(plan, [_hit(first), vision])
    with pytest.raises(RetrievalConsistencyError, match="disabled"):
        fuse_retrieval_candidates(plan, [_hit(first, "sparse")])


def test_candidate_budget_then_hard_validation_then_top_k():
    plan = _plan(policy=_policy(candidate_budget=2, top_k=1), constraints=[
        EventConstraint(key="quote", field="dialogue", text="我没事"),
    ])
    facts = [_fact(plan, chunk="first", events=[_event("不匹配")]),
             _fact(plan, chunk="second"), _fact(plan, chunk="third")]
    candidates = fuse_retrieval_candidates(plan, [_hit(fact, rank=i + 1) for i, fact in enumerate(facts)])
    assert len(candidates) == 2
    output = verify_retrieval_evidence(plan, candidates, facts[:2])
    assert output.rejected_chunk_ids == ("first",)
    assert "quote" in output.rejected[0].message
    assert output.accepted[0].candidate.ranking.video_chunk_id == "second"


def test_fact_verification_honors_fused_rank_even_when_input_order_differs():
    plan = _plan(policy=_policy(top_k=1))
    facts = [_fact(plan, chunk="first"), _fact(plan, chunk="second")]
    candidates = fuse_retrieval_candidates(plan, [_hit(facts[0]), _hit(facts[1], rank=2)])
    output = verify_retrieval_evidence(plan, tuple(reversed(candidates)), facts)
    assert output.accepted[0].candidate.ranking.video_chunk_id == "first"
    duplicate_rank = candidates[1].model_copy(update={
        "ranking": candidates[1].ranking.model_copy(update={"fused_rank": 1}),
    })
    with pytest.raises(RetrievalConsistencyError, match="rank"):
        verify_retrieval_evidence(plan, [candidates[0], duplicate_rank], facts)


def test_neighbor_also_used_as_candidate_cannot_change_its_content_version():
    plan = _plan()
    fact = _fact(plan, sequence=1, bounds=(10, 20), events=[_event(start=11, end=13)])
    before = _fact(plan, chunk="before", sequence=0)
    candidates = fuse_retrieval_candidates(plan, [_hit(fact), _hit(before, rank=2)])
    changed = before.model_copy(update={
        "retrieval_identity": before.retrieval_identity.model_copy(update={"content_version": 9}),
    })
    with pytest.raises(RetrievalConsistencyError):
        verify_retrieval_evidence(plan, candidates, [fact, before], contexts={"chunk": [changed]})


@pytest.mark.parametrize("field", ["source_revision", "projection_revision"])
def test_forged_revision_cannot_be_used_as_fact(field):
    plan = _plan()
    fact = _fact(plan)
    forged = fact.model_copy(update={"retrieval_identity": fact.retrieval_identity.model_copy(update={field: "forged"})})
    with pytest.raises(RetrievalConsistencyError):
        _verify(plan, forged)


def test_forged_lexical_source_and_missing_facts_fail():
    plan = _plan()
    fact = _fact(plan)
    source = next(field for field in _projection(fact).lexical_fields if field.field == "dialogue")
    with pytest.raises(RetrievalConsistencyError, match="identical formal"):
        _verify(plan, fact, channel="lexical", matches=[source.model_copy(update={"raw_text": "伪造"})])
    candidates = fuse_retrieval_candidates(plan, [_hit(fact)])
    with pytest.raises(RetrievalConsistencyError, match="cover exactly"):
        verify_retrieval_evidence(plan, candidates, [])


def test_questions_are_recall_only_and_dense_proves_chunk_range():
    plan = _plan()
    fact = _fact(plan, enrichments=[SourceEnrichment(language="zh-Hans", questions=["只有问题有的原话"])])
    question = next(field for field in _projection(fact).lexical_fields if field.field == "questions")
    output = _verify(plan, fact, channel="lexical", matches=[question])
    assert output.accepted[0].evidence[0].granularity == "chunk"
    assert output.accepted[0].evidence[0].source is None
    dense = _verify(plan, fact).accepted[0].evidence[0]
    assert (dense.start_seconds, dense.end_seconds) == (0, 10)
    strict = replace(plan, constraints=(EventConstraint(key="quote", field="dialogue", text="只有问题有的原话"),))
    assert _verify(strict, fact).rejected_chunk_ids == ("chunk",)


def test_verbatim_speaker_must_match_same_event_and_translation_requires_opt_in():
    plan = _plan(constraints=[EventConstraint(key="quote", field="dialogue", text="我没事", speaker="千早爱音")])
    fact = _fact(plan, events=[_event(speaker="高松灯"), _event("另一句话", start=4, end=6)])
    assert _verify(plan, fact).rejected_chunk_ids == ("chunk",)
    translated = _fact(plan, events=[_event(role="TRANSLATION")])
    assert _verify(plan, translated).rejected_chunk_ids == ("chunk",)
    explicit = replace(plan, constraints=(plan.constraints[0].model_copy(update={"role": "TRANSLATION"}),))
    assert _verify(explicit, translated).accepted[0].evidence[0].source.role == "TRANSLATION"


def test_adjacent_context_retains_own_version_and_full_event_times():
    plan = _plan(constraints=[EventConstraint(key="quote", field="dialogue", text="先前原话", include_context=True)])
    fact = _fact(plan, sequence=1, bounds=(10, 20), events=[_event(start=11, end=13)])
    before = _fact(plan, chunk="before", sequence=0, version=9, events=[_event("先前原话", start=9, end=11)])
    output = _verify(plan, fact, contexts={"chunk": [before]})
    proof = output.accepted[0].evidence[0]
    assert proof.context == "before" and proof.video_chunk_id == "before"
    assert proof.retrieval_identity.content_version == 9
    assert (proof.start_seconds, proof.end_seconds) == (9, 11)
    denied = replace(plan, constraints=(plan.constraints[0].model_copy(update={"include_context": False}),))
    assert _verify(denied, fact, contexts={"chunk": [before]}).rejected_chunk_ids == ("chunk",)
    bad = before.model_copy(update={"sequence_no": 4})
    with pytest.raises(RetrievalConsistencyError, match="adjacent"):
        _verify(plan, fact, contexts={"chunk": [bad]})


@pytest.mark.parametrize("relation,times,accepted", [
    ("before", [(1, 3), (3, 5)], True), ("before", [(1, 4), (3, 5)], False),
    ("overlap", [(1, 4), (3, 5)], True), ("overlap", [(1, 3), (3, 5)], False),
])
def test_half_open_event_time_relations(relation, times, accepted):
    plan = _plan(constraints=[EventConstraint(key="a", field="dialogue", text="甲"),
                              EventConstraint(key="b", field="dialogue", text="乙")],
                 temporal=[TemporalConstraint(left="a", right="b", relation=relation)])
    fact = _fact(plan, events=[_event("甲", *times[0]), _event("乙", *times[1])])
    output = _verify(plan, fact)
    assert bool(output.accepted) is accepted


def test_temporal_constraints_cannot_mix_distinct_occurrences_of_one_condition():
    plan = _plan(constraints=[EventConstraint(key=key, field="dialogue", text=key) for key in ("a", "b", "c")],
                 temporal=[TemporalConstraint(left="a", right="b", relation="before"),
                           TemporalConstraint(left="b", right="c", relation="before")])
    fact = _fact(plan, events=[_event("b", 1, 2), _event("a", 3, 4), _event("c", 5, 6), _event("b", 7, 8)])
    output = _verify(plan, fact)
    assert output.rejected_chunk_ids == ("chunk",)
    assert "temporal" in output.rejected[0].message


@pytest.mark.parametrize("vector", [[True, 1], ["1", 0], [float("inf"), 0]])
def test_external_vector_artifacts_cannot_coerce_invalid_numbers(vector):
    request = _plan().encoding_requests[0]
    with pytest.raises(ValidationError):
        QueryVectorArtifact(input_sha256=request.target.input_sha256,
                            encoder_id=request.target.encoder_id, vector=vector)
