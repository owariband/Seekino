from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypeVar, cast

from arkruntime import Ark
from engine.rag import EncoderContract
from gateway.config import ArkModelSourceConfig, GatewayConfig, OpenAIModelSourceConfig
from gateway.constants import DEEPSEEK_PROVIDER, OPENAI_DEEPSEEK_PROFILE
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.model_sources import ModelReference, ark_options, source_key
from gateway.multimodal import MultimodalInput
from gateway.tracing import LangfuseTracing
from pydantic import BaseModel

from ..config.parse_profile import ParseEmbeddingConfig
from ..config.retrieval import RetrievalConfig
from ..repository.video_parse import VideoParseRepository
from ..service.retrieval_rerank import StructuredTextReranker
from ..service.video_retrieval import VideoParseRetrievalService
from ..storage.elasticsearch import ElasticsearchVideoChunkStore

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


def validate_rerank_source(
    gateway_config: GatewayConfig, retrieval_config: RetrievalConfig,
) -> OpenAIModelSourceConfig | None:
    if not retrieval_config.rerank_enabled:
        return None
    source = gateway_config.model_sources[gateway_config.agent_model.source]
    if (not isinstance(source, OpenAIModelSourceConfig)
            or source.provider != DEEPSEEK_PROVIDER
            or source.profile != OPENAI_DEEPSEEK_PROFILE):
        raise ValueError("structured text reranking requires a DeepSeek model source")
    return source


def build_retrieval_service(
    repository: VideoParseRepository, store: ElasticsearchVideoChunkStore, *,
    gateway_config: GatewayConfig, embedding_config: ParseEmbeddingConfig,
    retrieval_config: RetrievalConfig, index_generation: int,
    request_timeout_seconds: float, max_output_tokens: int | None = None,
    exact: bool = False, tracing: LangfuseTracing | None = None,
) -> tuple[VideoParseRetrievalService, EncoderContract]:
    from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION

    source = gateway_config.model_sources.get(embedding_config.source)
    if not isinstance(source, ArkModelSourceConfig):
        raise ValueError("Video Parse V2 embedding requires an Ark source")
    encoder = EncoderContract(
        provider=source.provider,
        model=ModelReference.resolve(
            embedding_config.model, encoder_model=embedding_config.encoder_model,
        ).model,
        encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        dimensions=embedding_config.dimensions,
    )
    embedder = _LazyQueryEmbedder(
        model=embedding_config.model, dimensions=embedding_config.dimensions,
        fps=embedding_config.video_fps, source=source,
        request_timeout_seconds=request_timeout_seconds,
    )
    rerank_source = validate_rerank_source(gateway_config, retrieval_config)
    reranker = None
    if rerank_source is not None:
        model = gateway_config.agent_model
        reranker = StructuredTextReranker(_LazyTextRerankModel(
            source=rerank_source, model=model.model,
            max_output_tokens=(model.max_output_tokens if max_output_tokens is None
                               else min(max_output_tokens, model.max_output_tokens)),
            request_timeout_seconds=min(request_timeout_seconds, model.request_timeout_seconds),
        ))
    service = VideoParseRetrievalService(
        repository, store, embedder, semantic_encoder=encoder,
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


class _LazyQueryEmbedder:
    """Create one short-lived SDK client for each query embedding call."""

    def __init__(
        self, *, model: str, dimensions: int, fps: float,
        request_timeout_seconds: float,
        source: ArkModelSourceConfig | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._fps = fps
        self._request_timeout_seconds = request_timeout_seconds
        self._source = source

    def embed_query(self, query: str) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_query", query))

    def embed_image(self, image: Path) -> EmbeddingResult:
        return cast(EmbeddingResult, self._embed("embed_image", image))

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        return cast(SparseEmbeddingResult, self._embed("embed_sparse", text))

    def _embed(
        self, method: Literal["embed_query", "embed_image", "embed_sparse"], value: str | Path,
    ) -> EmbeddingResult | SparseEmbeddingResult:
        from gateway.ark.embedding import DoubaoMultimodalEmbedder

        options = ark_options(self._source) if self._source is not None else {}
        client = Ark.volc(
            timeout=self._request_timeout_seconds, max_retries=0, **options,
        )
        try:
            delegate = DoubaoMultimodalEmbedder(
                self._model, output_dimensionality=self._dimensions,
                fps=self._fps, client=client,
            )
            return getattr(delegate, method)(value)
        finally:
            client.close()


class _LazyTextRerankModel:
    """Use the selected text model with a client owned by each scoring call."""

    provider_name = DEEPSEEK_PROVIDER

    def __init__(
        self, *, source: OpenAIModelSourceConfig, model: str,
        max_output_tokens: int, request_timeout_seconds: float,
    ) -> None:
        self.model_name = model
        self._source = source
        self._max_output_tokens = max_output_tokens
        self._request_timeout_seconds = request_timeout_seconds

    def generate(
        self, inputs: Sequence[MultimodalInput], *, schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel:
        from gateway.openai.multimodal import DeepSeekMultimodalModel

        delegate = DeepSeekMultimodalModel(
            self.model_name,
            base_url=self._source.base_url,
            api_key=source_key(self._source),
            max_output_tokens=self._max_output_tokens,
            request_timeout_seconds=self._request_timeout_seconds,
        )
        try:
            return delegate.generate(inputs, schema_name=schema_name, result_type=result_type)
        finally:
            delegate.close()
