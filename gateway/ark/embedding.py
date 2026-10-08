from __future__ import annotations

import base64
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arkruntime import Ark

from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.media import encode_media_file, read_video_file
from gateway.usage import ModelCallTracker, track_model_call


_MAX_INLINE_VIDEO_BYTES = 45 * 1024 * 1024
DOUBAO_MULTIMODAL_EMBEDDING_REVISION = "gateway-ark-embedding-v1"
_SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
}


class DoubaoMultimodalEmbedder:
    def __init__(
        self,
        model: str,
        *,
        output_dimensionality: int = 1024,
        fps: float = 1.0,
        request_timeout_seconds: float | None = None,
        client: Ark | None = None,
    ) -> None:
        if not 1 <= output_dimensionality <= 2048:
            raise ValueError("output_dimensionality must be between 1 and 2048")
        if not 0.2 <= fps <= 5.0:
            raise ValueError("fps must be between 0.2 and 5.0")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")

        self.model_name = model
        self.output_dimensionality = output_dimensionality
        self.fps = fps
        options: dict[str, object] = {"max_retries": 0}
        if request_timeout_seconds is not None:
            options["timeout"] = request_timeout_seconds
        self._client = client or Ark.volc(**options)

    def embed_video(self, video_path: str | Path) -> EmbeddingResult:
        path, video_bytes, mime_type = read_video_file(video_path)
        if mime_type not in _SUPPORTED_VIDEO_MIME_TYPES:
            raise ValueError(
                f"Doubao only accepts MP4, MOV, or AVI video: {path}"
            )
        if len(video_bytes) >= _MAX_INLINE_VIDEO_BYTES:
            raise ValueError(
                f"inline video must be smaller than 45 MiB: {path} "
                f"({len(video_bytes)} bytes)"
            )

        video_url = (
            f"data:{mime_type};base64,"
            f"{base64.b64encode(video_bytes).decode('ascii')}"
        )
        with track_model_call(
            provider="ark",
            model=self.model_name,
            operation="video_embedding",
        ) as call:
            call.raise_if_cancelled()
            unregister_cancel = _register_client_cancel(call, self._client)
            try:
                call.raise_if_cancelled()
                response = self._client.multimodal_embeddings.create(
                    model=self.model_name,
                    input=[
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": video_url,
                                **({"fps": self.fps} if self.supports_video_fps else {}),
                            },
                        }
                    ],
                    dimensions=self.output_dimensionality,
                    encoding_format="float",
                )
            finally:
                unregister_cancel()
            call.capture_response(response)
            return self._to_result(response, modality="video")

    @property
    def supports_video_fps(self) -> bool:
        """Only documented model revisions advertise configurable video sampling."""

        match = re.fullmatch(r"doubao-embedding-vision-(\d{6})", self.model_name)
        return bool(match and int(match.group(1)) >= 251215)

    def embed_image(self, image_path: str | Path) -> EmbeddingResult:
        """Embed one prepared still image; GIF and ordered-frame timing are unsupported."""

        image_url, size = encode_media_file(image_path, expected_kind="image")
        if size >= _MAX_INLINE_VIDEO_BYTES:
            raise ValueError("inline image must be smaller than 45 MiB")
        with track_model_call(
            provider="ark", model=self.model_name, operation="image_embedding",
        ) as call:
            call.raise_if_cancelled()
            unregister_cancel = _register_client_cancel(call, self._client)
            try:
                call.raise_if_cancelled()
                response = self._client.multimodal_embeddings.create(
                    model=self.model_name,
                    input=[{"type": "image_url", "image_url": {"url": image_url}}],
                    dimensions=self.output_dimensionality,
                    encoding_format="float",
                )
            finally:
                unregister_cancel()
            call.capture_response(response)
            return self._to_result(response, modality="image")

    def embed_text(self, text: str) -> EmbeddingResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("text must not be empty")

        with track_model_call(
            provider="ark",
            model=self.model_name,
            operation="text_embedding",
        ) as call:
            call.raise_if_cancelled()
            unregister_cancel = _register_client_cancel(call, self._client)
            try:
                call.raise_if_cancelled()
                response = self._client.multimodal_embeddings.create(
                    model=self.model_name,
                    input=[{"type": "text", "text": normalized_text}],
                    dimensions=self.output_dimensionality,
                    encoding_format="float",
                )
            finally:
                unregister_cancel()
            call.capture_response(response)
            return self._to_result(response, modality="text")

    def embed_sparse(self, text: str) -> SparseEmbeddingResult:
        """Request pure-text sparse indices from the selected multimodal model."""

        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("text must not be empty")
        with track_model_call(
            provider="ark", model=self.model_name, operation="text_embedding",
        ) as call:
            call.raise_if_cancelled()
            unregister_cancel = _register_client_cancel(call, self._client)
            try:
                call.raise_if_cancelled()
                response = self._client.multimodal_embeddings.create(
                    model=self.model_name,
                    input=[{"type": "text", "text": normalized_text}],
                    sparse_embedding={"type": "enabled"},
                    dimensions=self.output_dimensionality,
                    encoding_format="float",
                )
            finally:
                unregister_cancel()
            call.capture_response(response)
            entries = getattr(getattr(response, "data", None), "sparse_embedding", None)
            if not isinstance(entries, (list, tuple)):
                raise RuntimeError("embedding provider omitted requested sparse entries")
            weights = {}
            for entry in entries:
                index = getattr(entry, "index", None)
                value = getattr(entry, "value", None)
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise RuntimeError("embedding provider returned an invalid sparse index")
                if str(index) in weights:
                    raise RuntimeError("embedding provider returned duplicate sparse indices")
                weights[str(index)] = value
            return SparseEmbeddingResult(model=_response_model(response), weights=weights)

    def embed_query(self, query: str) -> EmbeddingResult:
        """Backward-compatible query-specific alias for text embedding."""

        if not query.strip():
            raise ValueError("query must not be empty")
        return self.embed_text(query)

    def _to_result(self, response: object, *, modality: str) -> EmbeddingResult:
        data = getattr(response, "data", None)
        values = getattr(data, "embedding", None)
        if not values or isinstance(values, str):
            raise RuntimeError("embedding provider returned an empty vector")

        return EmbeddingResult(
            model=_response_model(response),
            modality=modality,
            vector=list(values),
        )


def _response_model(response: object) -> str:
    model = getattr(response, "model", None)
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("embedding provider omitted its actual response model")
    return model


def _register_client_cancel(
    call: ModelCallTracker, client: Any
) -> Callable[[], None]:
    close = getattr(client, "close", None)
    return call.add_cancel_callback(close) if callable(close) else lambda: None
