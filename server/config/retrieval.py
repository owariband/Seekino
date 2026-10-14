from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from engine.rag.retrieval.contracts import RetrievalPolicy

from .global_agent_settings import read_global_agent_settings


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    channel_top_k: int = 20
    rrf_k: int = 60
    lexical_weight: float = 1.0
    text_weight: float = 1.0
    vision_weight: float = 1.0
    candidate_budget: int | None = None
    sparse_weight: float = 0.0
    rerank_enabled: bool = False
    search_default_top_k: int = 30

    @classmethod
    def from_environment(
        cls, *, channel_top_k: int | str | None = None,
        rrf_k: int | str | None = None,
        lexical_weight: float | str | None = None,
        text_weight: float | str | None = None,
        vision_weight: float | str | None = None,
        candidate_budget: int | str | None = None,
        sparse_weight: float | str | None = None,
        rerank_enabled: bool | str | None = None,
        search_default_top_k: int | str | None = None,
    ) -> "RetrievalConfig":
        defaults = cls()
        raw_search_default_top_k = (
            search_default_top_k if search_default_top_k is not None else os.getenv(
                "ANIVAG_RETRIEVAL_SEARCH_DEFAULT_TOP_K", str(defaults.search_default_top_k),
            )
        )
        raw_candidate_budget = (
            candidate_budget if candidate_budget is not None
            else os.getenv("ANIVAG_RETRIEVAL_CANDIDATE_BUDGET", "").strip() or None
        )
        rerank = (rerank_enabled if rerank_enabled is not None else os.getenv(
            "ANIVAG_RETRIEVAL_RERANK_ENABLED", str(defaults.rerank_enabled),
        ))
        if isinstance(rerank, str):
            normalized = rerank.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                rerank = True
            elif normalized in {"0", "false", "no", "off"}:
                rerank = False
            else:
                raise ValueError("ANIVAG_RETRIEVAL_RERANK_ENABLED must be a boolean value")
        return cls(
            channel_top_k=int(channel_top_k if channel_top_k is not None else os.getenv(
                "ANIVAG_RETRIEVAL_CHANNEL_TOP_K", str(defaults.channel_top_k),
            )),
            rrf_k=int(rrf_k if rrf_k is not None else os.getenv(
                "ANIVAG_RETRIEVAL_RRF_K", str(defaults.rrf_k),
            )),
            lexical_weight=float(lexical_weight if lexical_weight is not None else os.getenv(
                "ANIVAG_RETRIEVAL_LEXICAL_WEIGHT", str(defaults.lexical_weight),
            )),
            text_weight=float(text_weight if text_weight is not None else os.getenv(
                "ANIVAG_RETRIEVAL_TEXT_WEIGHT", str(defaults.text_weight),
            )),
            vision_weight=float(vision_weight if vision_weight is not None else os.getenv(
                "ANIVAG_RETRIEVAL_VISION_WEIGHT", str(defaults.vision_weight),
            )),
            candidate_budget=int(raw_candidate_budget) if raw_candidate_budget is not None else None,
            sparse_weight=float(sparse_weight if sparse_weight is not None else os.getenv(
                "ANIVAG_RETRIEVAL_SPARSE_WEIGHT", str(defaults.sparse_weight),
            )),
            rerank_enabled=rerank,
            search_default_top_k=(
                int(raw_search_default_top_k)
                if isinstance(raw_search_default_top_k, str) else raw_search_default_top_k
            ),
        )

    def __post_init__(self) -> None:
        if type(self.search_default_top_k) is not int or self.search_default_top_k <= 0:
            raise ValueError("search_default_top_k must be a positive integer")
        RetrievalPolicy(
            lexical_weight=self.lexical_weight,
            channel_top_k=self.channel_top_k,
            rrf_k=self.rrf_k,
            semantic_weight=self.text_weight,
            vision_weight=self.vision_weight,
            sparse_weight=self.sparse_weight,
            candidate_budget=(
                self.channel_top_k if self.candidate_budget is None else self.candidate_budget
            ),
            top_k=1,
        )
        if not isinstance(self.rerank_enabled, bool):
            raise ValueError("rerank_enabled must be a boolean")


def resolve_retrieval_model_limits(
    config_path: str | Path | None = None, *, request_timeout_seconds: float | None = None,
) -> tuple[float, int]:
    if request_timeout_seconds is not None and (
        not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0
    ):
        raise ValueError("request_timeout_seconds must be finite and greater than zero")
    agent, _ = read_global_agent_settings(config_path)
    timeout = agent.execution_timeout_seconds
    if request_timeout_seconds is not None:
        timeout = min(timeout, request_timeout_seconds)
    return timeout, agent.max_output_tokens
