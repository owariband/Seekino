"""Bridge verified retrieval candidates to an existing structured text model."""

from __future__ import annotations

from engine.rag.retrieval.rerank import (
    RerankRequest, RerankResult, RerankScorerContract, accept_rerank_result,
)
from gateway.rerank import RerankModel


_PROTOCOL = (
    "gateway structured text relevance: JSON scores[{video_chunk_id, score}]; "
    "one finite score per candidate; higher is more relevant; no new facts"
)


class StructuredTextReranker:
    """Explicit opt-in adapter; the caller supplies the already selected model."""

    def __init__(self, delegate: RerankModel) -> None:
        self._delegate = delegate

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def contract(self) -> RerankScorerContract:
        return RerankScorerContract(model=self.model_name, protocol_provenance=_PROTOCOL)

    def score(self, request: RerankRequest) -> RerankResult:
        """Score every supplied candidate without changing its identity or evidence."""

        if request.scorer != self.contract:
            raise ValueError("rerank request differs from the selected text scorer protocol")
        if not isinstance(request.query, str) or not request.query.strip():
            raise ValueError("structured text reranker supports only nonblank text queries")
        documents = []
        for item in request.items:
            if item.visual_input is not None or not isinstance(item.text, str) or not item.text.strip():
                raise ValueError("structured text reranker requires text candidates without media")
            documents.append({
                "video_chunk_id": item.candidate.candidate.ranking.video_chunk_id,
                "text": item.text,
            })
        ids = [document["video_chunk_id"] for document in documents]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("rerank request requires a nonempty unique candidate batch")
        output = self._delegate.rerank(
            request.query, [document["text"] for document in documents],
        )
        indexes = [item.index for item in output]
        if (any(isinstance(index, bool) or not isinstance(index, int) for index in indexes)
                or len(indexes) != len(ids) or set(indexes) != set(range(len(ids)))):
            raise ValueError("rerank response must score every candidate index exactly once")
        return accept_rerank_result(
            request, model=self.model_name,
            scores=tuple((ids[item.index], item.score) for item in output),
        )


__all__ = ["StructuredTextReranker"]
