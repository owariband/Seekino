"""Event fusion and formal Timeline verification, independent of storage adapters."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from engine.rag.indexing.events import EventIndexingContract, compute_event_id, plan_event_index
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts, FusedEventHit, RawEventHit, RetrievalConsistencyError,
)

EVENT_RETRIEVAL_STRATEGY_VERSION = "event-three-channel-rrf-v1"


def fuse_event_hits(
    hits: Sequence[RawEventHit], *, channel_weights: Mapping[str, float], rrf_k: int = 60,
) -> tuple[FusedEventHit, ...]:
    """Retain every returned candidate/channel; budgets belong to the query policy."""

    channels = {"lexical", "semantic", "vision"}
    if set(channel_weights) - channels or type(rrf_k) is not int or rrf_k <= 0:
        raise ValueError("Invalid Event fusion channels or rrf_k")
    if any(isinstance(value, bool) or not isinstance(value, (float, int))
           or not math.isfinite(value) or value < 0 for value in channel_weights.values()):
        raise ValueError("Event weights must be finite nonnegative numbers")
    maximum = max(channel_weights.values(), default=0)
    if maximum <= 0:
        raise ValueError("Event fusion requires an enabled channel")
    scaled = {channel: weight / maximum for channel, weight in channel_weights.items()}
    total = math.fsum(scaled.values())
    weights = {channel: weight / total for channel, weight in scaled.items()}
    grouped: dict[str, list[RawEventHit]] = {}
    ranks: set[tuple[str, int]] = set()
    ids: set[tuple[str, str]] = set()
    for value in hits:
        hit = RawEventHit.model_validate(value)
        if weights.get(hit.channel, 0) <= 0:
            raise RetrievalConsistencyError("Disabled Event channel returned a hit")
        if (hit.channel, hit.rank) in ranks or (hit.channel, hit.event_id) in ids:
            raise RetrievalConsistencyError("Duplicate Event channel rank or ID")
        ranks.add((hit.channel, hit.rank))
        ids.add((hit.channel, hit.event_id))
        if hit.event_id != compute_event_id(hit.parse_task_id, hit.video_chunk_id, hit.version, hit.timeline_index):
            raise RetrievalConsistencyError("Event ID disagrees with its source tuple")
        previous = grouped.setdefault(hit.event_id, [])
        identity = hit.model_dump(exclude={"channel", "rank", "score", "matched_fields"})
        if previous and previous[0].model_dump(exclude={"channel", "rank", "score", "matched_fields"}) != identity:
            raise RetrievalConsistencyError("Event channels disagree about the formal source")
        previous.append(hit)
    scored = [(
        math.fsum(weights[hit.channel] / (rrf_k + hit.rank) for hit in values), event_id, values,
    ) for event_id, values in grouped.items()]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(FusedEventHit(
        **values[0].model_dump(exclude={"channel", "rank", "score", "matched_fields"}),
        matched_fields=tuple(field for hit in values for field in hit.matched_fields),
        fused_rank=rank, fused_score=score,
        channel_ranks={hit.channel: hit.rank for hit in values},
        channel_scores={hit.channel: hit.score for hit in values},
    ) for rank, (score, _event_id, values) in enumerate(scored, 1))


def verify_event_candidate(
    hit: FusedEventHit, facts: ChunkRetrievalFacts, *, contract: EventIndexingContract,
) -> FusedEventHit:
    """Validate Event projection against the same formal parent version, then expose times."""

    hit = FusedEventHit.model_validate(hit)
    facts = ChunkRetrievalFacts.model_validate(facts)
    identity = facts.retrieval_identity
    if (hit.video_chunk_id != facts.video_chunk_id or hit.parse_task_id != identity.parse_task_id
            or hit.version != identity.content_version or hit.timeline_index >= len(facts.result.timeline)):
        raise RetrievalConsistencyError("Event source is absent or has changed in formal Chunk facts")
    expected = next((plan for plan in plan_event_index(
        hit.parse_task_id, hit.video_chunk_id, hit.version, facts.result,
        facts.start_seconds, facts.end_seconds, contract,
    ) if plan.timeline_index == hit.timeline_index), None)
    if (expected is None or hit.event_id != expected.event_id or hit.event_type != expected.event_type
            or hit.speaker != expected.speaker or hit.lexical_fields != expected.lexical_fields):
        raise RetrievalConsistencyError("Event indexed content disagrees with the formal Timeline")
    event = facts.result.timeline[hit.timeline_index]
    return hit.model_copy(update={"start_ms": event.start_seconds, "end_ms": event.end_seconds})


def select_event_chunk_hits(hits: Sequence[FusedEventHit], *, top_k: int) -> tuple[FusedEventHit, ...]:
    """Apply a distinct-Chunk limit while retaining all its matching Events."""

    if type(top_k) is not int or top_k <= 0:
        raise ValueError("Event top_k must be a positive integer")
    selected: set[str] = set()
    result = []
    for value in hits:
        hit = FusedEventHit.model_validate(value)
        if hit.video_chunk_id not in selected:
            if len(selected) == top_k:
                continue
            selected.add(hit.video_chunk_id)
        result.append(hit)
    return tuple(result)


__all__ = [
    "EVENT_RETRIEVAL_STRATEGY_VERSION", "fuse_event_hits",
    "verify_event_candidate", "select_event_chunk_hits",
]
