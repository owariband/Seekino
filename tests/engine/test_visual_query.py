from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import EncoderContract
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.query_pipeline import plan_retrieval_query
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts,
    GifQueryInput,
    ImageQueryInput,
    QueryEncoderBinding,
    RawChunkHit,
    RetrievalConsistencyError,
    RetrievalPolicy,
    RetrievalScope,
    SelectedVideoTask,
    VisualQueryEncodingRequest,
    VisualQueryFrame,
)
from engine.rag.retrieval.evidence import verify_retrieval_evidence
from engine.rag.retrieval.pipeline import build_recall_requests, fuse_retrieval_candidates
from engine.rag.vision.inputs import VideoClipInputContract
from engine.rag.vision.query import accept_visual_query_vector


def _encoder(model="visual-query", **updates):
    values = dict(provider="test", model=model, encoder_revision="v1", dimensions=2)
    values.update(updates)
    return EncoderContract(**values)


def _contract():
    return ChunkIndexingContract(
        retrieval_contract_id="retrieval-v1",
        semantic_encoder=_encoder("text-document"),
        vision_encoder=_encoder("video-document"),
        visual_input_contract=VideoClipInputContract(
            fps=1.0, max_height=720, mime_type="video/mp4", preprocess_revision="v1",
        ),
        index_generation=3,
    )


def _policy(**updates):
    values = dict(
        lexical_weight=1.0, semantic_weight=1.0, vision_weight=1.0, sparse_weight=1.0,
        channel_top_k=5, candidate_budget=5, top_k=2, rrf_k=60,
    )
    values.update(updates)
    return RetrievalPolicy(**values)


def _scope():
    return RetrievalScope(workspace_id="workspace", tasks=(SelectedVideoTask(
        series_id="series", video_id="video", parse_task_id="task", status="COMPLETE",
    ),))


def _image(**updates):
    values = dict(input_sha256="a" * 64, mime_type="image/png", preprocess_revision="v1")
    values.update(updates)
    return ImageQueryInput(**values)


def _gif(**updates):
    values = dict(frames=(
        VisualQueryFrame(input_sha256="a" * 64, at_seconds=0.0),
        VisualQueryFrame(input_sha256="b" * 64, at_seconds=0.25),
        VisualQueryFrame(input_sha256="c" * 64, at_seconds=0.5),
    ), mime_type="image/png", preprocess_revision="v1")
    values.update(updates)
    return GifQueryInput(**values)


def _binding(contract, modality="image", **updates):
    values = dict(
        channel="vision", document_encoder_id=contract.vision_encoder_id,
        query_encoder=_encoder(), query_modality=modality,
        compatibility_provenance="test visual query/document output-space proof",
    )
    values.update(updates)
    return QueryEncoderBinding(**values)


def _plan(query, *, contract=None, policy=None, scope=None, bindings=None):
    contract = contract or _contract()
    return plan_retrieval_query(
        query, scope=scope if scope is not None else _scope(), index_contract=contract,
        policy=policy or _policy(),
        encoder_bindings=bindings if bindings is not None else (_binding(contract, query.kind),),
    )


@pytest.mark.parametrize("query", [_image(), _gif()], ids=["image", "gif"])
def test_visual_query_keeps_media_and_selects_only_vision_without_changing_policy(query):
    policy = _policy()
    original = policy.model_dump()
    plan = _plan(query, policy=policy)
    assert plan.query == query
    assert policy.model_dump() == original
    assert plan.policy.vision_weight == policy.vision_weight
    assert (plan.policy.lexical_weight, plan.policy.semantic_weight, plan.policy.sparse_weight) == (0.0, 0.0, 0.0)
    assert plan.policy.channel_top_k == policy.channel_top_k
    assert plan.policy.candidate_budget == policy.candidate_budget
    assert plan.policy.top_k == policy.top_k
    assert plan.sparse_request is None
    assert len(plan.encoding_requests) == 1
    request = plan.encoding_requests[0]
    assert isinstance(request, VisualQueryEncodingRequest)
    assert request.channels == ("vision",)
    assert request.visual_input == query
    vector = accept_visual_query_vector(request, model="visual-query", vector=[1, 0])
    recalls = build_recall_requests(plan, vectors=[vector])
    assert len(recalls) == 1
    recall = recalls[0]
    assert recall.channel == "vision"
    assert recall.text is None and recall.token_weights is None
    assert recall.vector == (1.0, 0.0)
    assert recall.scope == plan.scope
    assert recall.index_generation == 3


def test_gif_preserves_all_frame_order_and_rejects_old_encoding_after_sequence_changes():
    query = _gif()
    plan = _plan(query)
    request = plan.encoding_requests[0]
    assert request.visual_input.frames == query.frames
    artifact = accept_visual_query_vector(request, model="visual-query", vector=[1, 0])
    # The new sequence has legal increasing times but different frame content order.
    reordered = _gif(frames=(
        VisualQueryFrame(input_sha256="c" * 64, at_seconds=0.0),
        VisualQueryFrame(input_sha256="b" * 64, at_seconds=0.25),
        VisualQueryFrame(input_sha256="a" * 64, at_seconds=0.5),
    ))
    changed = _plan(reordered)
    with pytest.raises(ValueError):
        build_recall_requests(changed, vectors=[artifact])


@pytest.mark.parametrize("updates", [
    {"input_sha256": "b" * 64}, {"preprocess_revision": "v2"}, {"mime_type": "image/jpeg"},
])
def test_image_encoding_response_cannot_be_used_for_changed_prepared_input(updates):
    plan = _plan(_image())
    artifact = accept_visual_query_vector(plan.encoding_requests[0], model="visual-query", vector=[1, 0])
    changed = _plan(_image(**updates))
    with pytest.raises(ValueError):
        build_recall_requests(changed, vectors=[artifact])


@pytest.mark.parametrize("query", [_image(), _gif()], ids=["image", "gif"])
def test_visual_query_requires_proof_for_its_input_modality(query):
    contract = _contract()
    text_binding = QueryEncoderBinding(
        channel="vision", document_encoder_id=contract.vision_encoder_id,
        query_encoder=_encoder(), compatibility_provenance="text-only protocol proof",
    )
    assert text_binding.query_modality == "text"
    with pytest.raises(ValueError):
        _plan(query, contract=contract, bindings=[text_binding])
    other_modality = "gif" if query.kind == "image" else "image"
    with pytest.raises(ValueError):
        _plan(query, contract=contract, bindings=[_binding(contract, other_modality)])
    with pytest.raises(ValueError):
        _plan(query, contract=contract, bindings=[])


@pytest.mark.parametrize("updates", [
    {"document_encoder_id": "wrong-document-encoder"},
    {"query_encoder": _encoder(dimensions=3)},
    {"query_encoder": _encoder(normalization="l2")},
    {"query_encoder": _encoder(output_contract="different-output-contract")},
])
def test_visual_query_rejects_mismatched_document_output_space(updates):
    contract = _contract()
    with pytest.raises(ValueError):
        _plan(_image(), contract=contract, bindings=[_binding(contract, **updates)])


def test_visual_query_cannot_use_policy_with_vision_disabled():
    with pytest.raises(ValueError):
        _plan(_image(), policy=_policy(vision_weight=0.0))


def test_visual_query_selects_vision_binding_from_shared_configuration_and_rejects_duplicates():
    contract = _contract()
    vision = _binding(contract)
    semantic = QueryEncoderBinding(
        channel="semantic", document_encoder_id=contract.semantic_encoder_id,
        query_encoder=_encoder("text-query"), compatibility_provenance="test text protocol proof",
    )
    plan = _plan(_image(), contract=contract, bindings=[semantic, vision])
    assert plan.encoder_bindings == (vision,)
    with pytest.raises(ValueError, match="unique"):
        _plan(_image(), contract=contract, bindings=[vision, vision])


def test_text_query_cannot_use_visual_modality_compatibility_proof():
    contract = _contract()
    with pytest.raises(ValueError, match="text compatibility"):
        plan_retrieval_query(
            "query", scope=_scope(), index_contract=contract, policy=_policy(sparse_weight=0.0),
            encoder_bindings=[_binding(contract)],
        )


@pytest.mark.parametrize("model,vector", [
    ("wrong", [1, 0]), ("visual-query", [1]), ("visual-query", [0, 0]),
    ("visual-query", [float("nan"), 1]), ("visual-query", [float("inf"), 1]),
    ("visual-query", [True, 1]),
])
def test_visual_encoding_response_validates_model_and_vector(model, vector):
    request = _plan(_image()).encoding_requests[0]
    with pytest.raises(ValueError):
        accept_visual_query_vector(request, model=model, vector=vector)


@pytest.mark.parametrize("query", [_image(), _gif()], ids=["image", "gif"])
def test_empty_visual_scope_needs_no_model_requests_or_recall(query):
    plan = _plan(query, scope=RetrievalScope(workspace_id="workspace", tasks=()))
    assert plan.encoding_requests == ()
    assert plan.sparse_request is None
    assert build_recall_requests(plan) == ()
    assert fuse_retrieval_candidates(plan, []) == ()


@pytest.mark.parametrize("query", [_image(), _gif()], ids=["image", "gif"])
def test_visual_dense_result_proves_chunk_range_without_inventing_frame_time(query):
    plan = _plan(query)
    identity = VideoChunkRetrievalIdentity(
        workspace_id="workspace", series_id="series", video_id="video", parse_task_id="task",
        content_version=2, index_generation=3,
    )
    hit = RawChunkHit(
        channel="vision", video_chunk_id="chunk", retrieval_identity=identity,
        rank=1, score=0.8, start_seconds=0, end_seconds=10,
    )
    fact = ChunkRetrievalFacts(
        video_chunk_id="chunk", retrieval_identity=identity, sequence_no=0,
        start_seconds=0, end_seconds=10,
        result=VideoParseResult.model_validate({"persons": [], "timeline": [{
            "event_type": "SCENE", "start_seconds": 0, "end_seconds": 10,
            "description": "室内画面", "participants": [], "visible_persons": [],
        }]}),
    )
    candidates = fuse_retrieval_candidates(plan, [hit])
    output = verify_retrieval_evidence(plan, candidates, [fact])
    assert len(output.accepted) == 1
    proof, = output.accepted[0].evidence
    assert proof.granularity == "chunk" and proof.source is None
    assert (proof.start_seconds, proof.end_seconds) == (0, 10)


@pytest.mark.parametrize("channel", ["lexical", "semantic", "sparse"])
def test_visual_query_rejects_hits_from_text_channels_even_if_policy_weight_is_positive(channel):
    plan = _plan(_image())
    hit = RawChunkHit(
        channel=channel, video_chunk_id="chunk", retrieval_identity=VideoChunkRetrievalIdentity(
            workspace_id="workspace", series_id="series", video_id="video", parse_task_id="task",
            content_version=2, index_generation=3,
        ), rank=1, score=0.8, start_seconds=0, end_seconds=10,
    )
    with pytest.raises(RetrievalConsistencyError):
        fuse_retrieval_candidates(plan, [hit])


@pytest.mark.parametrize("updates", [
    {"input_sha256": "not-a-digest"}, {"input_sha256": "A" * 64},
    {"mime_type": "video/mp4"}, {"mime_type": "image/gif"}, {"mime_type": "image/"},
    {"preprocess_revision": " "}, {"unsupported": True},
])
def test_image_query_rejects_invalid_prepared_media(updates):
    with pytest.raises(ValidationError):
        _image(**updates)


@pytest.mark.parametrize("times", [(0.25, 0.0), (0.0, 0.0)])
def test_gif_query_requires_strictly_increasing_relative_times(times):
    frames = tuple(VisualQueryFrame(input_sha256=letter * 64, at_seconds=time)
                   for letter, time in zip(("a", "b"), times))
    with pytest.raises(ValidationError):
        _gif(frames=frames)


def test_gif_query_requires_multiple_frames():
    with pytest.raises(ValidationError):
        _gif(frames=(VisualQueryFrame(input_sha256="a" * 64, at_seconds=0.0),))


def test_prepared_gif_frames_cannot_use_source_container_mime_type():
    with pytest.raises(ValidationError):
        _gif(mime_type="image/gif")


@pytest.mark.parametrize("time", [-0.1, float("nan"), float("inf"), True, "0.0"])
def test_visual_query_frame_rejects_invalid_relative_time(time):
    with pytest.raises(ValidationError):
        VisualQueryFrame(input_sha256="a" * 64, at_seconds=time)


def test_visual_input_contracts_are_frozen():
    image, gif = _image(), _gif()
    with pytest.raises(ValidationError):
        image.preprocess_revision = "v2"
    with pytest.raises(ValidationError):
        gif.frames[0].at_seconds = 1.0


@pytest.mark.parametrize("rag_first", [False, True], ids=["module-first", "rag-first"])
def test_visual_query_public_imports_work_in_fresh_process(rag_first):
    script = """
import importlib
import sys

if sys.argv[1] == "True":
    importlib.import_module("engine.rag")
visual = importlib.import_module("engine.rag.vision.query")
contracts = importlib.import_module("engine.rag.retrieval.contracts")
rag = importlib.import_module("engine.rag")
for name in ("VisualQueryFrame", "ImageQueryInput", "GifQueryInput", "VisualQueryEncodingRequest"):
    assert getattr(rag, name) is getattr(contracts, name), name
for name in ("plan_visual_query_signals", "accept_visual_query_vector"):
    assert getattr(rag, name) is getattr(visual, name), name
assert not any(name == "engine.parsev1" or name.startswith("engine.parsev1.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(rag_first)], cwd=Path(__file__).parents[2],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
