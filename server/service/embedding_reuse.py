"""Decide whether a proven embedding can serve one new Chunk index build."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Modality = Literal["text", "video"]
SourceKind = Literal[
    "v1_native_artifact",
    "v2_index_cache",
    "import_parse_cache",
    "unknown",
]
ReuseGapReason = Literal[
    "UNPROVEN_INPUT",
    "INPUT_MISMATCH",
    "UNPROVEN_ENCODER",
    "ENCODER_MISMATCH",
    "INVALID_VECTOR",
]


@dataclass(frozen=True, slots=True)
class EmbeddingReuseTarget:
    """The input and encoder request required by the new index build.

    input_sha256 covers text after Gateway whitespace stripping or actual
    video bytes; the caller derives it from the new build's fixed input.
    """

    modality: Modality
    input_sha256: str
    provider: str
    model: str
    encoder_revision: str
    dimensions: int
    normalization: str
    output_contract: str
    video_fps: float | None = None
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if self.modality not in ("text", "video"):
            raise ValueError("target modality must be text or video")
        if not _is_sha256(self.input_sha256):
            raise ValueError("target input_sha256 must be lowercase SHA-256")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.provider, self.model, self.encoder_revision,
                self.normalization, self.output_contract,
            )
        ):
            raise ValueError("target encoder contract fields must be nonempty")
        if isinstance(self.dimensions, bool) or not isinstance(self.dimensions, int) or self.dimensions <= 0:
            raise ValueError("target dimensions must be a positive integer")
        if self.modality == "video":
            if (
                isinstance(self.video_fps, bool)
                or not isinstance(self.video_fps, (int, float))
                or not math.isfinite(self.video_fps)
                or self.video_fps <= 0
                or not isinstance(self.mime_type, str)
                or not self.mime_type.strip()
            ):
                raise ValueError("video target requires finite fps and MIME type")
        elif self.video_fps is not None or self.mime_type is not None:
            raise ValueError("text target must not contain video input settings")


@dataclass(frozen=True, slots=True)
class EmbeddingReuseDecision:
    vector: tuple[float, ...] | None
    gap: ReuseGapReason | None


def assess_embedding_reuse(
    target: EmbeddingReuseTarget,
    *,
    source_kind: SourceKind,
    metadata: Mapping[str, object],
    vector: Sequence[float] | None,
) -> EmbeddingReuseDecision:
    """Compare verified input and encoder claims without reading or invoking anything.

    The caller must prove source_kind from trusted persistence provenance. A
    MySQL JSON shape alone cannot establish that an Artifact was produced by
    the native V1 path. For native video, input_sha256 means media byte SHA;
    import-parse video caches instead hash path and stat data.
    """

    if source_kind not in (
        "v1_native_artifact", "v2_index_cache", "import_parse_cache"
    ):
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")
    if source_kind == "import_parse_cache" and target.modality == "video":
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")
    input_sha256 = metadata.get("input_sha256")
    if not _is_sha256(input_sha256):
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")
    if input_sha256 != target.input_sha256:
        return EmbeddingReuseDecision(None, "INPUT_MISMATCH")

    fields = (
        "modality", "provider", "model", "encoder_revision", "dimensions",
        "normalization", "output_contract",
    )
    if target.modality == "video":
        fields += ("video_fps", "mime_type")
    if any(
        metadata.get(name) is None or isinstance(metadata.get(name), bool)
        or (isinstance(metadata.get(name), str) and not metadata[name].strip())
        for name in fields
    ):
        return EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")
    if target.modality == "video" and (
        not isinstance(metadata["video_fps"], (int, float))
        or not math.isfinite(metadata["video_fps"])
    ):
        return EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")
    expected = {
        "modality": target.modality,
        "provider": target.provider,
        "model": target.model,
        "encoder_revision": target.encoder_revision,
        "dimensions": target.dimensions,
        "normalization": target.normalization,
        "output_contract": target.output_contract,
    }
    if target.modality == "video":
        expected.update(video_fps=target.video_fps, mime_type=target.mime_type)
    if any(metadata[name] != value for name, value in expected.items()):
        return EmbeddingReuseDecision(None, "ENCODER_MISMATCH")

    if vector is None or isinstance(vector, (str, bytes)):
        return EmbeddingReuseDecision(None, "INVALID_VECTOR")
    try:
        values = tuple(vector)
    except TypeError:
        return EmbeddingReuseDecision(None, "INVALID_VECTOR")
    if (
        len(values) != target.dimensions
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        )
        or not any(value != 0 for value in values)
    ):
        return EmbeddingReuseDecision(None, "INVALID_VECTOR")
    return EmbeddingReuseDecision(tuple(float(value) for value in values), None)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
