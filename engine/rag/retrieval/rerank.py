"""Verified candidate pool → external scoring request → ordered final results."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from engine.rag._validation import _nonblank
from engine.rag.indexing.projection import project_chunk_index
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts, GifQueryInput, ImageQueryInput, RetrievalCandidate,
    RetrievalEvidenceResult, RetrievalQueryPlan, VerifiedRetrievalCandidate,
)
from engine.rag.retrieval.evidence import verify_candidate_evidence
from engine.rag.vision.inputs import (
    OrderedFramesManifest, VideoClipManifest, VisualInputManifest,
    plan_ordered_frame_timestamps,
)


@dataclass(frozen=True, slots=True)
class RerankScorerContract:
    """Caller-declared protocol; actual model support must be verified externally.

    Scores are finite relevance values with higher values ranked first. They do
    not have to be probabilities and no absolute acceptance threshold is used.
    """

    model: str
    protocol_provenance: str
    query_modalities: tuple[Literal["text", "image", "gif"], ...] = ("text",)
    candidate_input: Literal["text", "video_clip", "ordered_frames"] = "text"

    def __post_init__(self) -> None:
        if not _nonblank(self.model) or not _nonblank(self.protocol_provenance):
            raise ValueError("scorer model and protocol provenance must be nonblank")
        modalities = tuple(self.query_modalities)
        if (not modalities or len(set(modalities)) != len(modalities)
                or any(kind not in ("text", "image", "gif") for kind in modalities)):
            raise ValueError("scorer query modalities must be supported and unique")
        if self.candidate_input not in ("text", "video_clip", "ordered_frames"):
            raise ValueError("unsupported scorer candidate input")
        object.__setattr__(self, "query_modalities", modalities)


@dataclass(frozen=True, slots=True)
class RerankItem:
    candidate: VerifiedRetrievalCandidate
    text: str | None = None
    visual_input: VisualInputManifest | None = None


@dataclass(frozen=True, slots=True)
class RerankRequest:
    query: str | ImageQueryInput | GifQueryInput
    scorer: RerankScorerContract
    items: tuple[RerankItem, ...]


@dataclass(frozen=True, slots=True)
class RerankPlan:
    query_plan: RetrievalQueryPlan
    evidence: RetrievalEvidenceResult
    request: RerankRequest | None

    def __post_init__(self) -> None:
        # Each execution owns its request; results cannot cross copied plans.
        if self.request is not None:
            object.__setattr__(self, "request", replace(self.request))


@dataclass(frozen=True, slots=True)
class RerankResult:
    """External response retained for strict acceptance or final fallback."""

    request: RerankRequest
    model: str
    scores: tuple[tuple[str, float], ...]


def plan_candidate_rerank(
    query_plan: RetrievalQueryPlan,
    candidates: Sequence[RetrievalCandidate],
    facts: Sequence[ChunkRetrievalFacts],
    *,
    scorer: RerankScorerContract | None = None,
    contexts: Mapping[str, Sequence[ChunkRetrievalFacts]] | None = None,
    visual_inputs: Mapping[str, VisualInputManifest] | None = None,
) -> RerankPlan:
    """Verify the complete pool, then request scores only for eligible candidates.

    A missing scorer disables scoring. The host retains media bytes, performs
    model calls, and associates each returned document score with its Chunk.
    """

    facts = tuple(ChunkRetrievalFacts.model_validate(value) for value in facts)
    evidence = verify_candidate_evidence(query_plan, candidates, facts, contexts=contexts)
    if scorer is None or not evidence.accepted:
        return RerankPlan(query_plan=query_plan, evidence=evidence, request=None)
    kind = "text" if isinstance(query_plan.query, str) else query_plan.query.kind
    if kind not in scorer.query_modalities:
        raise ValueError("query modality is not supported by the scorer protocol")
    by_id = {fact.video_chunk_id: fact for fact in facts}
    visual_inputs = visual_inputs or {}
    if set(visual_inputs) - set(by_id):
        raise ValueError("scorer media references an unrequested Chunk")
    items = []
    for verified in evidence.accepted:
        fact = by_id[verified.candidate.ranking.video_chunk_id]
        if scorer.candidate_input == "text":
            # Scoring uses formal Timeline content; generated questions remain
            # recall hints and do not become scoring evidence.
            text = project_chunk_index(
                fact.result, chunk_start_seconds=fact.start_seconds,
                chunk_end_seconds=fact.end_seconds,
                description_language=fact.description_language,
            ).semantic_text.strip()
            if not text:
                raise ValueError("text scorer requires nonblank formal content for every candidate")
            items.append(RerankItem(candidate=verified, text=text))
            continue
        manifest = visual_inputs.get(fact.video_chunk_id)
        if (not isinstance(manifest, (VideoClipManifest, OrderedFramesManifest))
                or manifest.kind != scorer.candidate_input):
            raise ValueError("scorer requires the declared media input for every candidate")
        if isinstance(manifest, VideoClipManifest):
            if (manifest.start_seconds, manifest.end_seconds) != (fact.start_seconds, fact.end_seconds):
                raise ValueError("scorer clip must cover the exact candidate Chunk range")
        elif isinstance(manifest, OrderedFramesManifest):
            if any(frame.at_seconds < fact.start_seconds or frame.at_seconds >= fact.end_seconds
                   for frame in manifest.frames):
                raise ValueError("scorer frame time falls outside the candidate Chunk range")
            if manifest.sampling is not None:
                planned_times = plan_ordered_frame_timestamps(
                    fact.start_seconds, fact.end_seconds, manifest.sampling,
                )
                if tuple(frame.requested_at_seconds for frame in manifest.frames) != planned_times:
                    raise ValueError("scorer frames do not match their sampling time plan")
        else:
            raise ValueError("unsupported scorer media manifest")
        items.append(RerankItem(candidate=verified, visual_input=manifest))
    return RerankPlan(
        query_plan=query_plan, evidence=evidence,
        request=RerankRequest(query=query_plan.query, scorer=scorer, items=tuple(items)),
    )


def accept_rerank_result(
    request: RerankRequest,
    *,
    model: str,
    scores: Sequence[tuple[str, float]],
) -> RerankResult:
    """Strictly accept a complete response with one finite score per candidate."""

    result = RerankResult(request=request, model=model, scores=scores)
    validated = _validate_rerank_result(request, result)
    return RerankResult(request=request, model=model, scores=tuple(validated.items()))


def finalize_candidate_rerank(
    plan: RerankPlan,
    result: RerankResult | None = None,
) -> RetrievalEvidenceResult:
    """Order verified candidates, then apply top_k; failed scoring falls back to RRF."""

    accepted = plan.evidence.accepted
    if plan.request is None:
        if result is not None:
            raise ValueError("rerank result is not requested by the plan")
    else:
        try:
            scores = _validate_rerank_result(plan.request, result)
        except ValueError as error:
            logging.getLogger(__name__).warning(
                "Rerank scores rejected; using RRF: candidates=%d reason=%s",
                len(accepted), str(error),
            )
        else:
            accepted = tuple(sorted(accepted, key=lambda item: (
                -scores[item.candidate.ranking.video_chunk_id], item.candidate.ranking.fused_rank,
            )))
    return RetrievalEvidenceResult(
        accepted=accepted[:plan.query_plan.policy.top_k], rejected=plan.evidence.rejected,
    )


def _validate_rerank_result(
    request: RerankRequest,
    result: RerankResult | None,
) -> dict[str, float]:
    if not isinstance(result, RerankResult):
        raise ValueError("planned scores are missing")
    if result.request is not request:
        raise ValueError("scores belong to a different plan request")
    if result.model != request.scorer.model:
        raise ValueError("scorer response model differs from its request")
    expected = {item.candidate.candidate.ranking.video_chunk_id for item in request.items}
    try:
        entries = tuple(result.scores)
    except TypeError:
        raise ValueError("scores must be candidate/score pairs") from None
    scores = {}
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("scores must be candidate/score pairs")
        chunk_id, value = entry
        if not isinstance(chunk_id, str) or chunk_id not in expected or chunk_id in scores:
            raise ValueError("score candidate is unplanned or duplicate")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("scores must be finite numbers")
        try:
            number = float(value)
        except OverflowError:
            raise ValueError("scores must be finite numbers") from None
        if not math.isfinite(number):
            raise ValueError("scores must be finite numbers")
        scores[chunk_id] = number
    if set(scores) != expected:
        raise ValueError("scores must cover every planned candidate")
    return scores


__all__ = [
    "RerankScorerContract", "RerankItem", "RerankRequest", "RerankPlan", "RerankResult",
    "plan_candidate_rerank", "accept_rerank_result", "finalize_candidate_rerank",
]
