from dataclasses import replace
import json

import pytest

from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.retrieval.contracts import (
    ImageQueryInput, RetrievalCandidate, VerifiedRetrievalCandidate, VerifiedSourceEvidence,
)
from engine.rag.retrieval.fusion import VersionedFusedVideoChunkRanking
from engine.rag.retrieval.rerank import RerankItem, RerankRequest
from gateway.multimodal import TextInput
from gateway.rerank import StructuredTextReranker
from gateway.usage import capture_model_usage, track_model_call


class FakeTextModel:
    provider_name = "deepseek"
    model_name = "deepseek-flash"

    def __init__(self, scores=None, error=None):
        self.scores = scores if scores is not None else [
            {"video_chunk_id": "first", "score": 0.2},
            {"video_chunk_id": "last", "score": 0.9},
        ]
        self.error = error
        self.calls = []

    def generate(self, inputs, *, schema_name, result_type):
        self.calls.append((inputs, schema_name, result_type))
        with track_model_call(provider=self.provider_name, model=self.model_name, operation=schema_name):
            if self.error:
                raise self.error
            return result_type.model_validate({"scores": self.scores})


def _request(adapter):
    identity = VideoChunkRetrievalIdentity(
        workspace_id="workspace", series_id="series", video_id="video", parse_task_id="task",
        content_version=2, index_generation=3,
    )
    items = []
    for rank, chunk in enumerate(("first", "last"), 1):
        candidate = RetrievalCandidate(ranking=VersionedFusedVideoChunkRanking(
            video_chunk_id=chunk, fused_rank=rank, fused_score=1 / (60 + rank), retrieval_identity=identity,
        ), start_seconds=0, end_seconds=10)
        verified = VerifiedRetrievalCandidate(candidate=candidate, evidence=(VerifiedSourceEvidence(
            video_chunk_id=chunk, retrieval_identity=identity, context="self", granularity="chunk",
            start_seconds=0, end_seconds=10,
        ),))
        items.append(RerankItem(candidate=verified, text=f"formal text {chunk}"))
    return RerankRequest(query="query", scorer=adapter.contract, items=tuple(items))


def test_explicit_text_scorer_uses_selected_delegate_and_one_complete_structured_call():
    delegate = FakeTextModel()
    adapter = StructuredTextReranker(delegate)
    assert adapter.model_name == adapter.contract.model == "deepseek-flash"
    assert adapter.provider_name == "deepseek"
    assert adapter.contract.query_modalities == ("text",) and adapter.contract.candidate_input == "text"
    request = _request(adapter)
    usage = []
    with capture_model_usage(usage.append):
        result = adapter.score(request)
    assert result.request is request and result.model == "deepseek-flash"
    assert result.scores == (("first", 0.2), ("last", 0.9))
    assert len(delegate.calls) == len(usage) == 1
    inputs, schema_name, _ = delegate.calls[0]
    assert len(inputs) == 1 and isinstance(inputs[0], TextInput) and schema_name == "rerank"
    payload = json.loads(inputs[0].text.split("\n", 1)[1])
    assert payload == {"query": "query", "candidates": [
        {"video_chunk_id": "first", "text": "formal text first"},
        {"video_chunk_id": "last", "text": "formal text last"},
    ]}
    assert request.items[0].candidate.candidate.ranking.retrieval_identity.content_version == 2


@pytest.mark.parametrize("scores", [
    [{"video_chunk_id": "first", "score": 0.2}],
    [{"video_chunk_id": "first", "score": 0.2}, {"video_chunk_id": "first", "score": 0.9}],
    [{"video_chunk_id": "first", "score": 0.2}, {"video_chunk_id": "extra", "score": 0.9}],
    [{"video_chunk_id": "first", "score": True}, {"video_chunk_id": "last", "score": 0.9}],
    [{"video_chunk_id": "first", "score": float("nan")}, {"video_chunk_id": "last", "score": 0.9}],
])
def test_text_scorer_does_not_accept_partial_duplicate_unknown_or_invalid_scores(scores):
    adapter = StructuredTextReranker(FakeTextModel(scores=scores))
    with pytest.raises(ValueError):
        adapter.score(_request(adapter))


@pytest.mark.parametrize("failure", ["image", "another-model", "visual-candidate", "empty"])
def test_text_only_scorer_rejects_unsupported_requests_before_model_call(failure):
    delegate = FakeTextModel()
    adapter = StructuredTextReranker(delegate)
    request = _request(adapter)
    if failure == "image":
        request = replace(request, query=ImageQueryInput(
            input_sha256="a" * 64, mime_type="image/png", preprocess_revision="v1",
        ))
    elif failure == "another-model":
        request = replace(request, scorer=replace(request.scorer, model="another-model"))
    elif failure == "visual-candidate":
        request = replace(request, items=(replace(request.items[0], text=None), *request.items[1:]))
    else:
        request = replace(request, items=())
    with pytest.raises(ValueError):
        adapter.score(request)
    assert delegate.calls == []


def test_provider_timeout_propagates_for_host_owned_whole_batch_fallback():
    delegate = FakeTextModel(error=TimeoutError("provider deadline"))
    adapter = StructuredTextReranker(delegate)
    with pytest.raises(TimeoutError):
        adapter.score(_request(adapter))
    assert len(delegate.calls) == 1
