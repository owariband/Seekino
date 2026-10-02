from __future__ import annotations

from dataclasses import dataclass

from engine.retrieval.video_retrieval import fuse_video_chunk_rankings


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    channel_top_k: int = 20
    rrf_k: int = 60
    lexical_weight: float = 1.0
    text_weight: float = 1.0
    vision_weight: float = 1.0

    def __post_init__(self) -> None:
        fuse_video_chunk_rankings(
            [],
            [],
            lexical_weight=self.lexical_weight,
            channel_top_k=self.channel_top_k,
            rrf_k=self.rrf_k,
            text_weight=self.text_weight,
            vision_weight=self.vision_weight,
        )
