"""Prepared image/GIF input → one visual request; callers execute the model."""

from __future__ import annotations

from collections.abc import Sequence

from engine.rag.encoding import validate_embedding_vector
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.retrieval.contracts import (
    GifQueryInput, ImageQueryInput, QueryEncoderBinding, QueryVectorArtifact,
    RetrievalPolicy, VisualQueryEncodingRequest,
)


def plan_visual_query_signals(
    query: ImageQueryInput | GifQueryInput,
    *,
    index_contract: ChunkIndexingContract,
    policy: RetrievalPolicy,
    encoder_bindings: Sequence[QueryEncoderBinding],
) -> tuple[tuple[QueryEncoderBinding, ...], tuple[VisualQueryEncodingRequest, ...]]:
    """Prepare the complete visual input using its declared compatible encoder."""

    if isinstance(query, ImageQueryInput):
        query = ImageQueryInput.model_validate(query)
    elif isinstance(query, GifQueryInput):
        query = GifQueryInput.model_validate(query)
    else:
        raise ValueError("visual query requires an image or gif input")
    if index_contract.vision_encoder is None:
        raise ValueError("text-only index does not support visual queries")
    if policy.vision_weight <= 0:
        raise ValueError("visual query requires an enabled vision channel")
    binding = None
    for value in encoder_bindings:
        candidate = QueryEncoderBinding.model_validate(value)
        if candidate.channel != "vision":
            continue
        if binding is not None:
            raise ValueError("vision encoder binding must be unique")
        binding = candidate
    if binding is None:
        raise ValueError("vision query requires query/document compatibility")
    if binding.query_modality != query.kind:
        raise ValueError("vision query modality differs from its encoder binding")
    if binding.document_encoder_id != index_contract.vision_encoder_id:
        raise ValueError("query binding targets a different document encoder")
    if any(getattr(binding.query_encoder, name) != getattr(index_contract.vision_encoder, name)
           for name in ("dimensions", "normalization", "output_contract")):
        raise ValueError("query/document vector output contracts differ")
    request = VisualQueryEncodingRequest(
        channels=("vision",), visual_input=query, encoder=binding.query_encoder,
    )
    return (binding,), (request,)


def accept_visual_query_vector(
    request: VisualQueryEncodingRequest, *, model: str, vector: Sequence[float],
) -> QueryVectorArtifact:
    if model != request.encoder.model:
        raise ValueError("visual query encoding response model differs from request")
    return QueryVectorArtifact(
        input_sha256=request.input_sha256, encoder_id=request.encoder_id,
        vector=validate_embedding_vector(vector, dimensions=request.encoder.dimensions),
    )
