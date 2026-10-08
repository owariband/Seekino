"""Shared recall requests and Chunk candidate fusion for prepared query signals."""

from __future__ import annotations

from collections.abc import Sequence

from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import SparseEmbeddingArtifact, validate_embedding_vector, validate_sparse_artifact
from engine.rag.indexing.projection import LexicalField
from engine.rag.retrieval.fusion import VersionedRankedVideoChunkMatch, fuse_versioned_video_chunk_rankings
from engine.rag.retrieval.contracts import (
    RetrievalConsistencyError,
    QueryVectorArtifact,
    RetrievalQueryPlan,
    RecallRequest,
    RawChunkHit,
    RetrievalCandidate,
    validate_retrieval_identity,
)


def build_recall_requests(
    plan: RetrievalQueryPlan, *, vectors: Sequence[QueryVectorArtifact] = (),
    sparse: SparseEmbeddingArtifact | None = None,
) -> tuple[RecallRequest, ...]:
    """Accept exactly the planned encodings, then expose executable recall signals."""

    artifacts: dict[tuple[str, str], QueryVectorArtifact] = {}
    for value in vectors:
        artifact = QueryVectorArtifact.model_validate(value)
        key = (artifact.input_sha256, artifact.encoder_id)
        if key in artifacts:
            raise ValueError("duplicate query vector artifact")
        artifacts[key] = artifact
    dense_signals = {}
    for request in plan.encoding_requests:
        artifact = artifacts.pop((request.input_sha256, request.encoder_id), None)
        if artifact is None:
            raise ValueError("planned query vector is missing")
        vector = validate_embedding_vector(artifact.vector, dimensions=request.encoder.dimensions)
        dense_signals.update((channel, vector) for channel in request.channels)
    if artifacts:
        raise ValueError("unplanned or stale query vector artifact")
    sparse_weights = None
    if plan.sparse_request is not None:
        if sparse is None:
            raise ValueError("planned Sparse query artifact is missing")
        sparse_weights = validate_sparse_artifact(plan.sparse_request, sparse)
    elif sparse is not None:
        raise ValueError("unplanned Sparse query artifact")
    if not plan.scope.tasks:
        return ()
    requests = []
    for channel, weight in plan.policy.weights.items():
        if not weight:
            continue
        requests.append(RecallRequest(
            channel=channel, scope=plan.scope,
            index_generation=plan.index_contract.index_generation, limit=plan.policy.channel_top_k,
            text=plan.query if channel == "lexical" and isinstance(plan.query, str) else None,
            vector=dense_signals.get(channel),
            token_weights=sparse_weights if channel == "sparse" else None,
        ))
    return tuple(requests)


def fuse_retrieval_candidates(
    plan: RetrievalQueryPlan, hits: Sequence[RawChunkHit],
) -> tuple[RetrievalCandidate, ...]:
    """Validate every returned hit before budget truncation or cross-channel fusion."""

    channels: dict[str, list[VersionedRankedVideoChunkMatch]] = {
        channel: [] for channel in plan.policy.weights
    }
    seen: dict[str, tuple[VideoChunkRetrievalIdentity, int, int]] = {}
    source_matches: dict[str, tuple[LexicalField, ...]] = {}
    ranks: dict[str, set[int]] = {channel: set() for channel in channels}
    ids: dict[str, set[str]] = {channel: set() for channel in channels}
    for value in hits:
        hit = RawChunkHit.model_validate(value)
        validate_retrieval_identity(plan, hit.retrieval_identity)
        if not plan.policy.weights[hit.channel]:
            raise RetrievalConsistencyError("disabled recall channel returned hits")
        if (hit.rank > plan.policy.channel_top_k or hit.rank in ranks[hit.channel]
                or hit.video_chunk_id in ids[hit.channel]):
            raise RetrievalConsistencyError("duplicate or out-of-budget recall rank/Chunk")
        ranks[hit.channel].add(hit.rank)
        ids[hit.channel].add(hit.video_chunk_id)
        if len(ids[hit.channel]) > plan.policy.channel_top_k:
            raise RetrievalConsistencyError("recall channel exceeded its budget")
        signature = (hit.retrieval_identity, hit.start_seconds, hit.end_seconds)
        if hit.video_chunk_id in seen and seen[hit.video_chunk_id] != signature:
            raise RetrievalConsistencyError("channels disagree about Chunk identity or bounds")
        seen[hit.video_chunk_id] = signature
        if hit.channel == "lexical":
            source_matches[hit.video_chunk_id] = hit.lexical_matches
        channels[hit.channel].append(VersionedRankedVideoChunkMatch(
            video_chunk_id=hit.video_chunk_id, retrieval_identity=hit.retrieval_identity,
            rank=hit.rank, score=hit.score,
        ))
    weights = plan.policy.normalized_weights
    fused = fuse_versioned_video_chunk_rankings(
        channels["semantic"], channels["vision"], lexical_rankings=channels["lexical"],
        sparse_rankings=channels["sparse"], lexical_weight=weights["lexical"],
        text_weight=weights["semantic"], vision_weight=weights["vision"],
        sparse_weight=weights["sparse"], channel_top_k=plan.policy.channel_top_k,
        rrf_k=plan.policy.rrf_k,
    )
    return tuple(RetrievalCandidate(
        ranking=ranking, start_seconds=seen[ranking.video_chunk_id][1],
        end_seconds=seen[ranking.video_chunk_id][2],
        lexical_matches=source_matches.get(ranking.video_chunk_id, ()),
    ) for ranking in fused[:plan.policy.candidate_budget])
