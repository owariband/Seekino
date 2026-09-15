from __future__ import annotations

import base64
from pathlib import Path

from arkruntime import Ark

from ._video_file import read_video_file
from .schemas import EmbeddingResult
from .usage import track_model_call

_MAX_INLINE_VIDEO_BYTES = 45 * 1024 * 1024
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
        client: Ark | None = None,
    ) -> None:
        if not 1 <= output_dimensionality <= 2048:
            raise ValueError("output_dimensionality must be between 1 and 2048")
        if not 0.2 <= fps <= 5.0:
            raise ValueError("fps must be between 0.2 and 5.0")

        self.model_name = model
        self.output_dimensionality = output_dimensionality
        self.fps = fps
        self._client = client or Ark.volc(max_retries=0)

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
        with track_model_call(provider="ark", model=self.model_name, operation="video_embedding") as call:
            response = self._client.multimodal_embeddings.create(
                model=self.model_name,
                input=[
                    {
                        "type": "video_url",
                        "video_url": {"url": video_url, "fps": self.fps},
                    }
                ],
                dimensions=self.output_dimensionality,
                encoding_format="float",
            )
            call.capture_response(response)
            return self._to_result(response, modality="video")

    def embed_text(self, text: str) -> EmbeddingResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("text must not be empty")

        with track_model_call(provider="ark", model=self.model_name, operation="text_embedding") as call:
            response = self._client.multimodal_embeddings.create(
                model=self.model_name,
                input=[{"type": "text", "text": normalized_text}],
                dimensions=self.output_dimensionality,
                encoding_format="float",
            )
            call.capture_response(response)
            return self._to_result(response, modality="text")

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
            model=self.model_name,
            modality=modality,
            vector=list(values),
        )
