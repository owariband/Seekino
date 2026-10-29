from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

if TYPE_CHECKING:
    from gateway.config import EmbeddingModelBinding, ModelSourceConfig


class EmbeddingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    modality: Literal["text", "image", "video"]
    vector: list[float] = Field(min_length=1)

    @computed_field
    @property
    def dimension(self) -> int:
        return len(self.vector)


class SparseEmbeddingResult(BaseModel):
    """Provider-neutral sparse indices and nonnegative activation weights."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, pattern=r"\S")
    weights: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]]


class EmbeddingModel(Protocol):
    """Configured encoder identity and its independently owned SDK client."""

    provider_name: str
    model_name: str
    encoder_model: str
    encoder_revision: str
    dimensions: int

    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_query(self, query: str) -> EmbeddingResult: ...

    def embed_image(self, image_path: str | Path) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...

    def embed_sparse(self, text: str) -> SparseEmbeddingResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class EmbeddingModelInfo:
    provider_name: str
    model_name: str
    encoder_model: str
    encoder_revision: str
    dimensions: int


def describe_embedding_model(
    binding: EmbeddingModelBinding, source: ModelSourceConfig,
) -> EmbeddingModelInfo:
    """Describe the configured encoder without allocating a client or reading keys."""
    from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
    from gateway.config import ArkModelSourceConfig, OllamaEmbeddingSourceConfig, LlamaCppEmbeddingSourceConfig

    if isinstance(source, OllamaEmbeddingSourceConfig):
        from gateway.local.embedding import ollama_encoder_revision

        revision = ollama_encoder_revision(binding.query_instruction)
    elif isinstance(source, LlamaCppEmbeddingSourceConfig):
        from gateway.local.llama_cpp_embedding import llama_cpp_encoder_revision

        revision = llama_cpp_encoder_revision(binding.query_instruction)
    elif isinstance(source, ArkModelSourceConfig):
        revision = DOUBAO_MULTIMODAL_EMBEDDING_REVISION
    else:
        raise TypeError(f"unsupported embedding adapter {source.adapter!r}")
    return EmbeddingModelInfo(
        provider_name=source.provider, model_name=binding.model,
        encoder_model=binding.encoder_model or binding.model,
        encoder_revision=revision,
        dimensions=binding.dimensions,
    )


class _OwnedEmbeddingModel:
    def __init__(self, delegate, client, *, binding, source, encoder_revision: str):
        self.provider_name = source.provider
        self.model_name = binding.model
        self.encoder_model = binding.encoder_model or binding.model
        self.encoder_revision = encoder_revision
        self.dimensions = binding.dimensions
        self._delegate = delegate
        self._client = client
        self._closed = False

    def _active(self):
        if self._closed:
            raise RuntimeError("Embedding model is closed")
        return self._delegate

    def embed_text(self, text: str) -> EmbeddingResult:
        return self._active().embed_text(text)

    def embed_query(self, query: str) -> EmbeddingResult:
        return self._active().embed_query(query)

    def embed_image(self, image_path: str | Path) -> EmbeddingResult:
        return self._active().embed_image(image_path)

    def embed_video(self, video_path: str | Path) -> EmbeddingResult:
        return self._active().embed_video(video_path)

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        return self._active().embed_sparse(text)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()


def build_embedding_model(
    binding: EmbeddingModelBinding,
    source: ModelSourceConfig,
    *,
    api_key: str | None,
) -> EmbeddingModel:
    """Construct the configured encoder without resolving names or credentials."""
    from arkruntime import Ark

    from gateway.ark.embedding import (
        DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        DoubaoMultimodalEmbedder,
    )
    from gateway.config import ArkModelSourceConfig, OllamaEmbeddingSourceConfig, LlamaCppEmbeddingSourceConfig

    if isinstance(source, OllamaEmbeddingSourceConfig):
        from gateway.local.embedding import OllamaTextEmbedder, ollama_encoder_revision

        if binding.input_mode != "text":
            raise ValueError("ollama_embedding requires input_mode=text")
        delegate = OllamaTextEmbedder(
            binding.model, base_url=source.base_url, dimensions=binding.dimensions,
            query_instruction=binding.query_instruction,
            request_timeout_seconds=binding.request_timeout_seconds or 600,
        )
        return _OwnedEmbeddingModel(
            delegate, delegate, binding=binding, source=source,
            encoder_revision=ollama_encoder_revision(binding.query_instruction),
        )

    if isinstance(source, LlamaCppEmbeddingSourceConfig):
        from gateway.local.llama_cpp_embedding import LlamaCppTextEmbedder, llama_cpp_encoder_revision

        if binding.input_mode != "text":
            raise ValueError("llama_cpp_embedding requires input_mode=text")
        delegate = LlamaCppTextEmbedder(
            binding.model, base_url=source.base_url, dimensions=binding.dimensions,
            query_instruction=binding.query_instruction,
            request_timeout_seconds=binding.request_timeout_seconds or 600,
        )
        return _OwnedEmbeddingModel(
            delegate, delegate, binding=binding, source=source,
            encoder_revision=llama_cpp_encoder_revision(binding.query_instruction),
        )

    if not isinstance(source, ArkModelSourceConfig):
        raise TypeError(f"unsupported embedding adapter {source.adapter!r}")
    options: dict[str, object] = {"api_key": api_key, "max_retries": 0}
    if binding.request_timeout_seconds is not None:
        options["timeout"] = binding.request_timeout_seconds
    client = Ark.volc(**options)
    try:
        delegate = DoubaoMultimodalEmbedder(
            binding.model,
            output_dimensionality=binding.dimensions,
            fps=binding.video_fps,
            request_timeout_seconds=binding.request_timeout_seconds,
            client=client,
        )
        return _OwnedEmbeddingModel(
            delegate, client, binding=binding, source=source,
            encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        )
    except BaseException:
        client.close()
        raise
