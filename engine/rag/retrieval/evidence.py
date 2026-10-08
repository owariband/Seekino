"""Same-version formal facts, source ownership and event/time verification."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from engine.rag.indexing.projection import (
    ChunkIndexProjection, LexicalField, project_chunk_index,
)
from engine.rag.retrieval.contracts import (
    RetrievalConsistencyError,
    RetrievalQueryPlan,
    RetrievalCandidate,
    EvidenceRequest,
    ChunkRetrievalFacts,
    VerifiedSourceEvidence,
    VerifiedRetrievalCandidate,
    RejectedRetrievalCandidate,
    RetrievalEvidenceResult,
    validate_retrieval_identity,
)


def build_evidence_requests(
    plan: RetrievalQueryPlan,
    candidates: Sequence[RetrievalCandidate],
) -> tuple[EvidenceRequest, ...]:
    """Plan formal reads for the full candidate budget before final top_k."""

    candidates = tuple(RetrievalCandidate.model_validate(item) for item in candidates)
    candidate_ids = {item.ranking.video_chunk_id for item in candidates}
    if len(candidate_ids) != len(candidates) or len(candidates) > plan.policy.candidate_budget:
        raise RetrievalConsistencyError("candidate list is duplicate or exceeds budget")
    if len({item.ranking.fused_rank for item in candidates}) != len(candidates):
        raise RetrievalConsistencyError("duplicate fused candidate rank")
    candidates = tuple(sorted(candidates, key=lambda item: item.ranking.fused_rank))
    include_context = any(condition.include_context for condition in plan.constraints)
    for candidate in candidates:
        validate_retrieval_identity(plan, candidate.ranking.retrieval_identity)
        if candidate.end_seconds <= candidate.start_seconds:
            raise RetrievalConsistencyError("candidate bounds must be increasing")
    return tuple(EvidenceRequest(candidate=item, include_context=include_context)
                 for item in candidates)


def verify_retrieval_evidence(
    plan: RetrievalQueryPlan,
    candidates: Sequence[RetrievalCandidate],
    facts: Sequence[ChunkRetrievalFacts],
    *,
    contexts: Mapping[str, Sequence[ChunkRetrievalFacts]] | None = None,
) -> RetrievalEvidenceResult:
    """Verify formal sources and hard conditions before applying the final top_k."""

    result = verify_candidate_evidence(plan, candidates, facts, contexts=contexts)
    return RetrievalEvidenceResult(
        accepted=result.accepted[:plan.policy.top_k], rejected=result.rejected,
    )


def verify_candidate_evidence(
    plan: RetrievalQueryPlan,
    candidates: Sequence[RetrievalCandidate],
    facts: Sequence[ChunkRetrievalFacts],
    *,
    contexts: Mapping[str, Sequence[ChunkRetrievalFacts]] | None = None,
) -> RetrievalEvidenceResult:
    """Return the full verified candidate pool without ranking or final truncation."""

    candidates = tuple(request.candidate for request in build_evidence_requests(plan, candidates))
    candidate_ids = {item.ranking.video_chunk_id for item in candidates}
    by_id: dict[str, ChunkRetrievalFacts] = {}
    for value in facts:
        fact = ChunkRetrievalFacts.model_validate(value)
        if fact.video_chunk_id in by_id:
            raise RetrievalConsistencyError("duplicate formal Chunk facts")
        by_id[fact.video_chunk_id] = fact
    if set(by_id) != candidate_ids:
        raise RetrievalConsistencyError("formal facts must cover exactly the pinned candidates")
    contexts = contexts or {}
    if set(contexts) - candidate_ids:
        raise RetrievalConsistencyError("context references an unrequested candidate")
    pinned_facts = dict(by_id)
    accepted, rejected = [], []
    for candidate in candidates:
        chunk_id = candidate.ranking.video_chunk_id
        fact = by_id[chunk_id]
        if (fact.retrieval_identity != candidate.ranking.retrieval_identity
                or (fact.start_seconds, fact.end_seconds) != (candidate.start_seconds, candidate.end_seconds)):
            raise RetrievalConsistencyError("formal facts differ from candidate version or bounds")
        projection = _verify_fact(plan, fact)
        if any(match not in projection.lexical_fields for match in candidate.lexical_matches):
            raise RetrievalConsistencyError("lexical match has no identical formal projection source")
        sources = [(fact, "self", projection)]
        neighbors = set()
        for value in contexts.get(chunk_id, ()):
            neighbor = ChunkRetrievalFacts.model_validate(value)
            if (neighbor.video_chunk_id in pinned_facts
                    and pinned_facts[neighbor.video_chunk_id] != neighbor):
                raise RetrievalConsistencyError("context differs from an already pinned Chunk fact")
            pinned_facts[neighbor.video_chunk_id] = neighbor
            direction = _check_neighbor(fact, neighbor)
            if direction in neighbors:
                raise RetrievalConsistencyError("duplicate adjacent context")
            neighbors.add(direction)
            sources.append((neighbor, direction, _verify_fact(plan, neighbor)))
        evidence = _match_constraints(plan, sources)
        if isinstance(evidence, RejectedRetrievalCandidate):
            rejected.append(evidence)
            continue
        for match in candidate.lexical_matches:
            if match.timeline_index is not None:
                proof = _source_evidence(fact, "self", match)
                if proof not in evidence:
                    evidence.append(proof)
        # Dense and enrichment-only matches prove a candidate range, not a
        # particular frame or the first unrelated visual event in that range.
        if not evidence:
            evidence.append(VerifiedSourceEvidence(
                video_chunk_id=chunk_id, retrieval_identity=fact.retrieval_identity,
                context="self", granularity="chunk",
                start_seconds=fact.start_seconds, end_seconds=fact.end_seconds,
            ))
        accepted.append(VerifiedRetrievalCandidate(candidate=candidate, evidence=tuple(evidence)))
    return RetrievalEvidenceResult(
        accepted=tuple(accepted), rejected=tuple(rejected),
    )


def _verify_fact(plan: RetrievalQueryPlan, fact: ChunkRetrievalFacts) -> ChunkIndexProjection:
    validate_retrieval_identity(plan, fact.retrieval_identity)
    if fact.description_language != plan.index_contract.description_language:
        raise RetrievalConsistencyError("formal projection configuration differs from index")
    projection = project_chunk_index(
        fact.result, chunk_start_seconds=fact.start_seconds, chunk_end_seconds=fact.end_seconds,
        description_language=fact.description_language, enrichments=fact.enrichments,
    )
    return projection


def _check_neighbor(fact: ChunkRetrievalFacts, neighbor: ChunkRetrievalFacts) -> str:
    identity, other = fact.retrieval_identity, neighbor.retrieval_identity
    if (neighbor.video_chunk_id == fact.video_chunk_id or any(
        getattr(identity, name) != getattr(other, name)
        for name in ("workspace_id", "series_id", "video_id", "parse_task_id")
    )):
        raise RetrievalConsistencyError("context must retain its adjacent Chunk's own identity")
    if neighbor.sequence_no == fact.sequence_no - 1 and neighbor.end_seconds == fact.start_seconds:
        return "before"
    if neighbor.sequence_no == fact.sequence_no + 1 and neighbor.start_seconds == fact.end_seconds:
        return "after"
    raise RetrievalConsistencyError("context is not a contiguous adjacent Chunk")


def _source_evidence(
    fact: ChunkRetrievalFacts, context: str, source: LexicalField, key: str | None = None,
) -> VerifiedSourceEvidence:
    return VerifiedSourceEvidence(
        video_chunk_id=fact.video_chunk_id, retrieval_identity=fact.retrieval_identity,
        context=context, granularity="event", source=source, constraint_key=key,
        start_seconds=source.start_seconds, end_seconds=source.end_seconds,
    )


def _match_constraints(
    plan: RetrievalQueryPlan,
    sources: Sequence[tuple[ChunkRetrievalFacts, str, ChunkIndexProjection]],
) -> list[VerifiedSourceEvidence] | RejectedRetrievalCandidate:
    options: dict[str, list[VerifiedSourceEvidence]] = {}
    for condition in plan.constraints:
        matches = []
        for fact, context, projection in sources:
            if context != "self" and not condition.include_context:
                continue
            for source in projection.lexical_fields:
                if source.field != condition.field or source.timeline_index is None:
                    continue
                text_matches = (source.raw_text == condition.text if condition.match == "exact"
                                else condition.text in source.raw_text)
                # Verbatim dialogue defaults to original wording; translations
                # qualify only when the caller explicitly requests that role.
                role = condition.role or ("ORIGINAL" if condition.field == "dialogue" else None)
                event = fact.result.timeline[source.timeline_index]
                if (not text_matches
                        or condition.speaker is not None and source.speaker != condition.speaker
                        or condition.language is not None and source.language != condition.language
                        or role is not None and source.role != role
                        or condition.event_type is not None and event.event_type != condition.event_type
                        or condition.visible_person is not None and condition.visible_person not in event.visible_persons):
                    continue
                matches.append(_source_evidence(fact, context, source, condition.key))
        options[condition.key] = matches
    missing = [key for key, values in options.items() if not values]
    if missing:
        return RejectedRetrievalCandidate(
            video_chunk_id=sources[0][0].video_chunk_id,
            message="No formal event satisfies conditions: " + ", ".join(missing),
        )
    # Find one coherent assignment. Checking each relation independently can
    # accidentally combine different occurrences of the same utterance.
    assigned: dict[str, VerifiedSourceEvidence] = {}
    keys = list(options)

    def choose(index: int) -> bool:
        if index == len(keys):
            return True
        key = keys[index]
        for evidence in options[key]:
            assigned[key] = evidence
            compatible = True
            for relation in plan.temporal_constraints:
                if relation.left not in assigned or relation.right not in assigned:
                    continue
                left, right = assigned[relation.left], assigned[relation.right]
                compatible = (left.end_seconds <= right.start_seconds if relation.relation == "before"
                              else max(left.start_seconds, right.start_seconds) < min(left.end_seconds, right.end_seconds))
                if not compatible:
                    break
            if compatible and choose(index + 1):
                return True
            del assigned[key]
        return False

    try:
        if choose(0):
            return list(assigned.values())
    except RecursionError:
        logging.getLogger(__name__).warning(
            "Event constraint verification reached recursion limit: "
            "chunk_id=%s conditions=%d temporal_relations=%d",
            sources[0][0].video_chunk_id, len(keys), len(plan.temporal_constraints),
        )
        return RejectedRetrievalCandidate(
            video_chunk_id=sources[0][0].video_chunk_id,
            message="Event condition verification incomplete: recursion depth limit reached.",
        )
    return RejectedRetrievalCandidate(
        video_chunk_id=sources[0][0].video_chunk_id,
        message="No coherent event assignment satisfies temporal conditions",
    )
