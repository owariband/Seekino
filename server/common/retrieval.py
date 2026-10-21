from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from engine.rag import EncoderContract
from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import RetrievalPolicy
from gateway.config import EmbeddingModelBinding, GatewayConfig, ModelBinding
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.multimodal import adapter_capabilities
from gateway.rerank import RerankScore
from gateway.tracing import LangfuseTracing

if TYPE_CHECKING:
    from gateway.model_factory import GatewayClient

from ..config.parse_profile import ParseEmbeddingConfig
from ..config.retrieval import RetrievalConfig
from ..service.video_parse_persistence import VideoParsePersistenceService
from ..service.retrieval_rerank import StructuredTextReranker
from ..service.video_retrieval import VideoParseRetrievalService
from ..service.video_event_retrieval import VideoEventRetrievalService
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore


def validate_rerank_source(
    gateway_config: GatewayConfig, retrieval_config: RetrievalConfig,
) -> ModelBinding | None:
    if not retrieval_config.rerank_enabled:
        return None
    binding = gateway_config.resolve_model_binding("rerank")
    source = gateway_config.source(binding.source)
    if binding.kind != "rerank" or not adapter_capabilities(source.adapter).rerank:
        raise ValueError("configured model binding does not support reranking")
    return binding


def build_retrieval_service(
    persistence: VideoParsePersistenceService, store: ElasticsearchVideoChunkStore, *,
    gateway_config: GatewayConfig, embedding_config: ParseEmbeddingConfig,
    retrieval_config: RetrievalConfig, index_generation: int,
    request_timeout_seconds: float, max_output_tokens: int | None = None,
    exact: bool = False, tracing: LangfuseTracing | None = None,
    gateway_client: GatewayClient | None = None,
) -> tuple[VideoParseRetrievalService, EncoderContract]:
    from gateway.model_factory import GatewayClient

    client = gateway_client if gateway_client is not None else GatewayClient(gateway_config)
    encoder, embedder = _build_query_embedding(
        client, embedding_config, request_timeout_seconds=request_timeout_seconds,
    )
    rerank_binding = validate_rerank_source(gateway_config, retrieval_config)
    reranker = None
    if rerank_binding is not None:
        updates = {"request_timeout_seconds": min(
            request_timeout_seconds, rerank_binding.request_timeout_seconds,
        )}
        if max_output_tokens is not None:
            updates["max_output_tokens"] = min(max_output_tokens, rerank_binding.max_output_tokens)
        reranker = StructuredTextReranker(_LazyTextRerankModel(
            gateway_client=client, binding=rerank_binding.model_copy(update=updates),
        ))
    service = VideoParseRetrievalService(
        persistence, store, embedder, semantic_encoder=encoder,
        expected_index_generation=index_generation,
        expected_embedding_source=embedding_config.source,
        channel_top_k=retrieval_config.channel_top_k, rrf_k=retrieval_config.rrf_k,
        lexical_weight=retrieval_config.lexical_weight,
        text_weight=retrieval_config.text_weight, vision_weight=retrieval_config.vision_weight,
        candidate_budget=retrieval_config.candidate_budget,
        sparse_weight=retrieval_config.sparse_weight, reranker=reranker,
        exact=exact, tracing=tracing,
    )
    return service, encoder


def build_event_retrieval_service(
    persistence: VideoParsePersistenceService, store: ElasticsearchVideoEventStore, *,
    gateway_config: GatewayConfig, embedding_config: ParseEmbeddingConfig,
    retrieval_config: RetrievalConfig, contract: EventIndexingContract,
    request_timeout_seconds: float, exact: bool = False,
    tracing: LangfuseTracing | None = None, gateway_client: GatewayClient | None = None,
) -> VideoEventRetrievalService:
    """Reuse the configured multimodal query binding without initializing its index."""
    from gateway.model_factory import GatewayClient

    client = gateway_client if gateway_client is not None else GatewayClient(gateway_config)
    encoder, embedder = _build_query_embedding(
        client, embedding_config, request_timeout_seconds=request_timeout_seconds,
    )
    if contract.semantic_encoder != encoder or contract.vision_encoder != encoder:
        raise ValueError("Event encoders differ from the declared shared multimodal query binding")
    active = sum(value > 0 for value in (
        retrieval_config.lexical_weight, retrieval_config.text_weight, retrieval_config.vision_weight,
    ))
    policy = RetrievalPolicy(
        lexical_weight=retrieval_config.lexical_weight, semantic_weight=retrieval_config.text_weight,
        vision_weight=retrieval_config.vision_weight, channel_top_k=retrieval_config.channel_top_k,
        candidate_budget=retrieval_config.candidate_budget or retrieval_config.channel_top_k * active,
        rrf_k=retrieval_config.rrf_k, top_k=1,
    )
    return VideoEventRetrievalService(
        persistence, store, embedder,
        contract=contract, policy=policy, exact=exact, tracing=tracing,
    )


def _build_query_embedding(
    client: GatewayClient, config: ParseEmbeddingConfig, *, request_timeout_seconds: float,
) -> tuple[EncoderContract, _LazyQueryEmbedder]:
    binding = EmbeddingModelBinding(
        source=config.source, model=config.model, encoder_model=config.encoder_model,
        dimensions=config.dimensions, video_fps=config.video_fps,
        request_timeout_seconds=min(
            request_timeout_seconds, config.request_timeout_seconds or request_timeout_seconds,
        ), kind="embedding",
    )
    metadata = client.describe_embedding(binding)
    encoder = EncoderContract(
        provider=metadata.provider_name, model=metadata.encoder_model,
        encoder_revision=metadata.encoder_revision, dimensions=metadata.dimensions,
    )
    return encoder, _LazyQueryEmbedder(gateway_client=client, binding=binding)


class _LazyQueryEmbedder:
    """Obtain and close one configured Gateway model per embedding call."""

    def __init__(
        self, *, gateway_client: GatewayClient, binding: EmbeddingModelBinding,
    ) -> None:
        self._gateway_client = gateway_client
        self._binding = binding

    def embed_query(self, query: str) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_query", query))

    def embed_image(self, image: Path) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_image", image))

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        return cast(SparseEmbeddingResult, self._embed("embed_sparse", text))

    def _embed(
        self, method: Literal["embed_query", "embed_image", "embed_sparse"], value: str | Path,
    ) -> EmbeddingResult | SparseEmbeddingResult:
        model = self._gateway_client.create_embedding_model(self._binding)
        try:
            return getattr(model, method)(value)
        finally:
            model.close()


class _LazyTextRerankModel:
    """Use the selected text model with a client owned by each scoring call."""

    def __init__(
        self, *, gateway_client: GatewayClient, binding: ModelBinding,
    ) -> None:
        self.model_name = binding.model
        self.provider_name = gateway_client.config.source(binding.source).provider
        self._gateway_client = gateway_client
        self._binding = binding

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[RerankScore, ...]:
        model = self._gateway_client.create_rerank_model(self._binding)
        try:
            return model.rerank(query, documents)
        finally:
            model.close()
