"""Input processing entry: validate query context, then prepare modality signals."""

from __future__ import annotations

from collections.abc import Sequence

from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.nlp.query import plan_text_query_signals
from engine.rag.vision.query import plan_visual_query_signals
from engine.rag.retrieval.contracts import (
    EventConstraint, GifQueryInput, ImageQueryInput, QueryEncoderBinding,
    RetrievalPolicy, RetrievalQueryPlan, RetrievalScope, TemporalConstraint,
)


def plan_retrieval_query(
    query: str | ImageQueryInput | GifQueryInput,
    *,
    scope: RetrievalScope,
    index_contract: ChunkIndexingContract,
    policy: RetrievalPolicy,
    encoder_bindings: Sequence[QueryEncoderBinding] = (),
    constraints: Sequence[EventConstraint] = (),
    temporal_constraints: Sequence[TemporalConstraint] = (),
) -> RetrievalQueryPlan:
    """Plan enabled signals; share an encoding only after both spaces are declared."""

    if isinstance(query, str):
        if not query.strip():
            raise ValueError("query must be nonblank")
    elif isinstance(query, ImageQueryInput):
        query = ImageQueryInput.model_validate(query)
    elif isinstance(query, GifQueryInput):
        query = GifQueryInput.model_validate(query)
    else:
        raise ValueError("query must be text, an image or a GIF")
    scope = RetrievalScope.model_validate(scope)
    policy = RetrievalPolicy.model_validate(policy)
    conditions = tuple(EventConstraint.model_validate(item) for item in constraints)
    temporal = tuple(TemporalConstraint.model_validate(item) for item in temporal_constraints)
    keys = {condition.key for condition in conditions}
    if len(keys) != len(conditions):
        raise ValueError("constraint keys must be unique")
    if any(item.left not in keys or item.right not in keys or item.left == item.right for item in temporal):
        raise ValueError("temporal constraints must reference two distinct condition keys")
    if isinstance(query, str):
        if any(QueryEncoderBinding.model_validate(item).query_modality != "text"
               for item in encoder_bindings):
            raise ValueError("text query requires text compatibility bindings")
        bindings, requests, sparse_request = plan_text_query_signals(
            query, index_contract=index_contract, policy=policy, encoder_bindings=encoder_bindings,
        )
    else:
        bindings, requests = plan_visual_query_signals(
            query, index_contract=index_contract, policy=policy, encoder_bindings=encoder_bindings,
        )
        policy = RetrievalPolicy.model_validate(policy.model_copy(update={
            "lexical_weight": 0.0, "semantic_weight": 0.0, "sparse_weight": 0.0,
        }))
        sparse_request = None
    return RetrievalQueryPlan(
        query=query, scope=scope, index_contract=index_contract, policy=policy,
        encoder_bindings=bindings,
        encoding_requests=requests if scope.tasks else (),
        sparse_request=sparse_request if scope.tasks else None,
        constraints=conditions, temporal_constraints=temporal,
    )
