from __future__ import annotations

from dataclasses import dataclass

from engine.rag.retrieval.contracts import RetrievalPolicy


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    channel_top_k: int = 20
    rrf_k: int = 60
    lexical_weight: float = 1.0
    text_weight: float = 1.0
    vision_weight: float = 1.0
    candidate_budget: int = 60
    sparse_weight: float = 0.0
    rerank_enabled: bool = False

    def __post_init__(self) -> None:
        RetrievalPolicy(
            lexical_weight=self.lexical_weight,
            channel_top_k=self.channel_top_k,
            rrf_k=self.rrf_k,
            semantic_weight=self.text_weight,
            vision_weight=self.vision_weight,
            sparse_weight=self.sparse_weight,
            candidate_budget=self.candidate_budget,
            top_k=1,
        )
        if not isinstance(self.rerank_enabled, bool):
            raise ValueError("rerank_enabled must be a boolean")
