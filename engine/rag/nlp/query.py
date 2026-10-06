"""Text input → compatible encoding signals; the caller executes model requests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from engine.rag.encoding import (
    SourceKind, SparseEmbeddingRequest, assess_embedding_reuse, compute_encoder_id,
    validate_embedding_vector,
)
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.retrieval.contracts import (
    QueryEncoderBinding, QueryEncodingRequest, QueryVectorArtifact, RetrievalPolicy,
)


def plan_text_query_signals(
    query: str,
    *,
    index_contract: ChunkIndexingContract,
    policy: RetrievalPolicy,
    encoder_bindings: Sequence[QueryEncoderBinding],
) -> tuple[tuple[QueryEncoderBinding, ...], tuple[QueryEncodingRequest, ...], SparseEmbeddingRequest | None]:
    """Prepare text signals using the validated query policy and encoder bindings."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be nonblank")
    bindings: dict[str, QueryEncoderBinding] = {}
    for value in encoder_bindings:
        binding = QueryEncoderBinding.model_validate(value)
        if binding.channel in bindings or not policy.weights[binding.channel]:
            raise ValueError("encoder bindings must be unique and belong to enabled channels")
        bindings[binding.channel] = binding
    requests: dict[str, QueryEncodingRequest] = {}
    for channel in ("semantic", "vision"):
        if not policy.weights[channel]:
            continue
        binding = bindings.get(channel)
        if binding is None:
            raise ValueError(f"enabled {channel} channel requires query/document compatibility")
        document_encoder = getattr(index_contract, f"{channel}_encoder")
        if binding.document_encoder_id != getattr(index_contract, f"{channel}_encoder_id"):
            raise ValueError("query binding targets a different document encoder")
        if any(getattr(binding.query_encoder, name) != getattr(document_encoder, name)
               for name in ("dimensions", "normalization", "output_contract")):
            raise ValueError("query/document vector output contracts differ")
        request_id = compute_encoder_id("text", binding.query_encoder)
        previous = requests.get(request_id)
        requests[request_id] = QueryEncodingRequest(
            channels=(*previous.channels, channel) if previous else (channel,),
            text=query, encoder=binding.query_encoder,
        )
    sparse_request = None
    if policy.sparse_weight:
        if index_contract.sparse_encoder is None:
            raise ValueError("enabled Sparse requires an indexing Sparse encoder")
        sparse_request = SparseEmbeddingRequest(
            encoder=index_contract.sparse_encoder,
            input_sha256=hashlib.sha256(query.encode()).hexdigest(), text=query,
        )
    return tuple(bindings.values()), tuple(requests.values()), sparse_request


def accept_query_vector(
    request: QueryEncodingRequest, *, model: str, vector: Sequence[float],
) -> QueryVectorArtifact:
    if model != request.encoder.model:
        raise ValueError("query encoding response model differs from request")
    return QueryVectorArtifact(
        input_sha256=request.target.input_sha256, encoder_id=request.target.encoder_id,
        vector=validate_embedding_vector(vector, dimensions=request.encoder.dimensions),
    )


def reuse_query_vector(
    request: QueryEncodingRequest, *, source_kind: SourceKind,
    metadata: Mapping[str, object], vector: Sequence[float] | None,
) -> QueryVectorArtifact | None:
    decision = assess_embedding_reuse(
        request.target, source_kind=source_kind, metadata=metadata, vector=vector,
    )
    if decision.vector is None:
        return None
    return accept_query_vector(request, model=request.encoder.model, vector=decision.vector)
