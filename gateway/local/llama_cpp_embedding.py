"""Native llama.cpp text embeddings with an explicit query/document encoding contract."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import httpx

from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.usage import track_model_call


def llama_cpp_encoder_revision(query_instruction: str | None) -> str:
    contract = {
        "protocol": "llama-cpp-openai-embeddings-last-pooling-v1", "normalization": "l2",
        "truncate": False, "document_template": "{text}",
        "query_template": "Instruct: {instruction}\nQuery: {text}" if query_instruction else "{text}",
        "query_instruction": query_instruction,
    }
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    return f"gateway-llama-cpp-embedding-v1:{digest}"


class LlamaCppEmbeddingError(RuntimeError):
    """The local encoder returned an invalid response or incompatible vectors."""


class LlamaCppTextEmbedder:
    provider_name = "llama_cpp"

    def __init__(
        self, model: str, *, base_url: str, dimensions: int,
        request_timeout_seconds: float = 600, query_instruction: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not model.strip() or not base_url.strip():
            raise ValueError("model and base_url must not be empty")
        if type(dimensions) is not int or not 1 <= dimensions <= 2048:
            raise ValueError("dimensions must be between 1 and 2048")
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive and finite")
        if query_instruction is not None and not query_instruction.strip():
            raise ValueError("query_instruction must not be blank")
        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self.query_instruction = query_instruction
        self._client = client or httpx.Client(timeout=request_timeout_seconds, trust_env=False)

    def embed_text(self, text: str) -> EmbeddingResult:
        return self._embed(text, query=False)

    def embed_query(self, query: str) -> EmbeddingResult:
        return self._embed(query, query=True)

    def _embed(self, text: str, *, query: bool) -> EmbeddingResult:
        text = text.strip()
        if not text:
            raise ValueError("text must not be empty")
        if query and self.query_instruction is not None:
            text = f"Instruct: {self.query_instruction}\nQuery: {text}"
        with track_model_call(
            provider=self.provider_name, model=self.model_name, operation="text_embedding",
        ) as call:
            call.raise_if_cancelled()
            unregister = call.add_cancel_callback(self._client.close)
            try:
                call.raise_if_cancelled()
                response = self._client.post(f"{self.base_url}/v1/embeddings", json={
                    "model": self.model_name, "input": [text],
                    "encoding_format": "float", "truncate": False,
                })
                response.raise_for_status()
                call.raise_if_cancelled()
                try:
                    payload = response.json()
                except ValueError as error:
                    raise LlamaCppEmbeddingError("llama.cpp returned invalid embedding JSON") from error
                if not isinstance(payload, dict):
                    raise LlamaCppEmbeddingError("llama.cpp returned an invalid embedding envelope")
                model = payload.get("model")
                # Preserve the actual identity; an unrelated model must not populate this index.
                call.capture_response({"model": model, "headers": response.headers})
                if model != self.model_name:
                    raise LlamaCppEmbeddingError("llama.cpp response model differs from the configured model")
                usage = payload.get("usage")
                tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                if type(tokens) is int and tokens >= 0:
                    call.capture_response({"usage": {
                        "input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens,
                    }})
                embeddings = payload.get("data")
                if not isinstance(embeddings, list) or len(embeddings) != 1:
                    raise LlamaCppEmbeddingError("llama.cpp must return exactly one embedding")
                item = embeddings[0]
                if not isinstance(item, dict) or item.get("index") != 0:
                    raise LlamaCppEmbeddingError("llama.cpp returned an invalid embedding index")
                vector = item.get("embedding")
                if not isinstance(vector, list) or len(vector) != self.dimensions:
                    raise LlamaCppEmbeddingError("llama.cpp embedding dimensions differ from the configured dimensions")
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                    raise LlamaCppEmbeddingError("llama.cpp embedding must contain finite numbers")
                norm = math.hypot(*vector)
                if not math.isfinite(norm) or norm == 0:
                    raise LlamaCppEmbeddingError("llama.cpp returned a zero or invalid embedding")
                return EmbeddingResult(
                    model=model, modality="text", vector=[value / norm for value in vector],
                )
            finally:
                unregister()

    def embed_image(self, image_path: str | Path) -> EmbeddingResult:
        raise ValueError("llama_cpp_embedding only supports text; image embedding is disabled")

    def embed_video(self, video_path: str | Path) -> EmbeddingResult:
        raise ValueError("llama_cpp_embedding only supports text; video embedding is disabled")

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        raise ValueError("llama_cpp_embedding does not provide sparse embeddings")

    def close(self) -> None:
        self._client.close()
