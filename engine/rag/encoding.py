"""Provider-neutral encoder contracts, artifacts, validation and vector reuse."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from engine.rag._validation import (
    _nonblank, _nonnegative_finite, _positive_finite, _sha256_json, is_sha256,
)
from engine.rag.vision.inputs import VisualInputContract, VisualInputManifest

DEFAULT_ENCODER_NORMALIZATION = "none"
DEFAULT_ENCODER_OUTPUT_CONTRACT = "float-vector-v1"
EncodingChannel = Literal["semantic", "vision"]

Modality = Literal["text", "video"]
VisualInputKind = Literal["video_clip", "ordered_frames"]
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
    """Exact input and encoder identity for query-vector cache reuse."""

    modality: Modality
    input_sha256: str
    provider: str
    model: str
    encoder_revision: str
    dimensions: int
    normalization: str
    output_contract: str
    encoder_id: str
    input_contract_id: str | None = None
    visual_input_kind: VisualInputKind | None = None
    video_fps: float | None = None
    mime_type: str | None = None
    max_height: int | None = None
    preprocess_revision: str | None = None
    sampling_contract_id: str | None = None

    def __post_init__(self) -> None:
        if self.modality not in ("text", "video"):
            raise ValueError("target modality must be text or video")
        if not is_sha256(self.input_sha256):
            raise ValueError("target input_sha256 must be lowercase SHA-256")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.provider,
                self.model,
                self.encoder_revision,
                self.normalization,
                self.output_contract,
                self.encoder_id,
            )
        ):
            raise ValueError("target encoder contract fields must be nonempty")
        if (
            isinstance(self.dimensions, bool)
            or not isinstance(self.dimensions, int)
            or self.dimensions <= 0
        ):
            raise ValueError("target dimensions must be a positive integer")

        if self.modality == "text":
            if any(
                value is not None
                for value in (
                    self.input_contract_id,
                    self.visual_input_kind,
                    self.video_fps,
                    self.mime_type,
                    self.max_height,
                    self.preprocess_revision,
                    self.sampling_contract_id,
                )
            ):
                raise ValueError("text target must not contain visual input settings")
            return

        if not _nonblank(self.input_contract_id):
            raise ValueError("video target requires input_contract_id")
        if self.visual_input_kind not in ("video_clip", "ordered_frames"):
            raise ValueError("video target requires a supported visual_input_kind")
        if not _nonblank(self.mime_type) or not _nonblank(self.preprocess_revision):
            raise ValueError("video target requires MIME type and preprocess revision")
        if self.visual_input_kind == "video_clip":
            if (
                not _positive_finite(self.video_fps)
                or isinstance(self.max_height, bool)
                or not isinstance(self.max_height, int)
                or self.max_height <= 0
            ):
                raise ValueError("video clip target requires finite fps and max_height")
            if self.sampling_contract_id is not None:
                raise ValueError("video clip target must not contain frame sampling settings")
        else:
            if not _nonblank(self.sampling_contract_id):
                raise ValueError("ordered frames target requires sampling_contract_id")
            if self.video_fps is not None or self.max_height is not None:
                raise ValueError("ordered frames target must not contain clip settings")


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
    """Accept only a vector whose input and complete encoder claims are proven."""

    if source_kind not in (
        "v1_native_artifact",
        "v2_index_cache",
        "import_parse_cache",
    ):
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")
    if source_kind == "import_parse_cache" and target.modality == "video":
        # The legacy import cache hashes path/stat metadata rather than media bytes.
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")

    input_sha256 = metadata.get("input_sha256")
    if not is_sha256(input_sha256):
        return EmbeddingReuseDecision(None, "UNPROVEN_INPUT")
    if input_sha256 != target.input_sha256:
        return EmbeddingReuseDecision(None, "INPUT_MISMATCH")

    expected = _encoder_claims(target)
    if target.modality == "text" and "encoder_id" not in metadata:
        # The previous V2 receipt stored every text encoder claim except the
        # derived ID.  For text there is no additional input contract hidden
        # behind that ID, so exact claims are sufficient to reconstruct it and
        # upgrade the receipt without paying for the same embedding again.
        expected.pop("encoder_id")
    for name in expected:
        value = metadata.get(name)
        if value is None or isinstance(value, bool):
            return EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")
        if isinstance(value, str) and not value.strip():
            return EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")
    if target.visual_input_kind == "video_clip" and not _positive_finite(
        metadata.get("video_fps")
    ):
        return EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")
    if any(metadata[name] != value for name, value in expected.items()):
        return EmbeddingReuseDecision(None, "ENCODER_MISMATCH")

    try:
        validated = validate_embedding_vector(vector, dimensions=target.dimensions)
    except ValueError:
        return EmbeddingReuseDecision(None, "INVALID_VECTOR")
    return EmbeddingReuseDecision(validated, None)


def embedding_receipt(
    target: EmbeddingReuseTarget,
    vector: Sequence[float],
) -> dict[str, object]:
    """Serialize the proof needed to reassess one representation later."""

    validated = validate_embedding_vector(vector, dimensions=target.dimensions)
    return {
        "input_sha256": target.input_sha256,
        **_encoder_claims(target),
        "vector": list(validated),
    }


def validate_embedding_vector(
    vector: Sequence[float] | None,
    *,
    dimensions: int,
) -> tuple[float, ...]:
    if vector is None or isinstance(vector, (str, bytes)):
        raise ValueError("embedding vector must be numeric")
    try:
        values = tuple(vector)
    except TypeError as exc:
        raise ValueError("embedding vector must be numeric") from exc
    if (
        len(values) != dimensions
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        )
        or not any(value != 0 for value in values)
    ):
        raise ValueError(
            f"embedding vector must contain {dimensions} finite, nonzero dimensions"
        )
    return tuple(float(value) for value in values)


def validate_sparse_weights(
    weights: Mapping[str, float],
) -> tuple[tuple[str, float], ...]:
    """Canonical nonnegative token weights; no activation is a valid result."""

    if not isinstance(weights, Mapping):
        raise ValueError("sparse weights must be a token-to-weight mapping")
    entries: list[tuple[str, float]] = []
    for token, weight in weights.items():
        try:
            valid_weight = _nonnegative_finite(weight)
        except OverflowError:
            valid_weight = False
        if not _nonblank(token) or not valid_weight:
            raise ValueError("sparse tokens must be nonblank with finite nonnegative weights")
        if weight > 0:
            entries.append((token, float(weight)))
    return tuple(sorted(entries))


def _encoder_claims(target: EmbeddingReuseTarget) -> dict[str, object]:
    claims: dict[str, object] = {
        "modality": target.modality,
        "provider": target.provider,
        "model": target.model,
        "encoder_revision": target.encoder_revision,
        "dimensions": target.dimensions,
        "normalization": target.normalization,
        "output_contract": target.output_contract,
        "encoder_id": target.encoder_id,
    }
    if target.modality == "video":
        claims.update(
            input_contract_id=target.input_contract_id,
            visual_input_kind=target.visual_input_kind,
            mime_type=target.mime_type,
            preprocess_revision=target.preprocess_revision,
        )
        if target.visual_input_kind == "video_clip":
            claims.update(
                video_fps=target.video_fps,
                max_height=target.max_height,
            )
        else:
            claims["sampling_contract_id"] = target.sampling_contract_id
    return claims


@dataclass(frozen=True, slots=True)
class EncoderContract:
    provider: str
    model: str
    encoder_revision: str
    dimensions: int
    normalization: str = DEFAULT_ENCODER_NORMALIZATION
    output_contract: str = DEFAULT_ENCODER_OUTPUT_CONTRACT

    def __post_init__(self) -> None:
        if any(
            not _nonblank(value)
            for value in (
                self.provider,
                self.model,
                self.encoder_revision,
                self.normalization,
                self.output_contract,
            )
        ):
            raise ValueError("encoder contract fields must be nonblank")
        if (
            isinstance(self.dimensions, bool)
            or not isinstance(self.dimensions, int)
            or self.dimensions <= 0
        ):
            raise ValueError("encoder dimensions must be a positive integer")

    def claims(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "encoder_revision": self.encoder_revision,
            "dimensions": self.dimensions,
            "normalization": self.normalization,
            "output_contract": self.output_contract,
        }


@dataclass(frozen=True, slots=True)
class SparseEncoderContract:
    provider: str
    model: str
    encoder_revision: str
    normalization: str = "none"
    output_contract: Literal["token-weight-v1"] = "token-weight-v1"

    def __post_init__(self) -> None:
        if any(not _nonblank(value) for value in self.claims().values()):
            raise ValueError("sparse encoder contract fields must be nonblank")
        if self.output_contract != "token-weight-v1":
            raise ValueError("unsupported sparse output contract")

    def claims(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "encoder_revision": self.encoder_revision,
            "normalization": self.normalization,
            "output_contract": self.output_contract,
        }


@dataclass(frozen=True, slots=True)
class SparseEmbeddingRequest:
    encoder: SparseEncoderContract
    text: str

    def __post_init__(self) -> None:
        if not _nonblank(self.text):
            raise ValueError("sparse request requires nonblank text")


@dataclass(frozen=True, slots=True)
class SparseEmbeddingArtifact:
    request: SparseEmbeddingRequest
    token_weights: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, SparseEmbeddingRequest):
            raise ValueError("sparse artifact requires its original request")
        entries = tuple(self.token_weights)
        weights = dict(entries)
        if len(weights) != len(entries):
            raise ValueError("sparse artifact must not contain duplicate tokens")
        object.__setattr__(self, "token_weights", validate_sparse_weights(weights))


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    channel: EncodingChannel
    encoder: EncoderContract
    semantic_text: str | None = None
    visual_input: VisualInputManifest | None = None

    def __post_init__(self) -> None:
        if self.channel == "semantic":
            if not _nonblank(self.semantic_text) or self.visual_input is not None:
                raise ValueError("semantic request requires only semantic_text")
        elif self.channel == "vision":
            if self.semantic_text is not None or self.visual_input is None:
                raise ValueError("vision request requires only visual_input")
        else:
            raise ValueError("embedding channel must be semantic or vision")


@dataclass(frozen=True, slots=True)
class EmbeddingArtifact:
    request: EmbeddingRequest
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, EmbeddingRequest):
            raise ValueError("embedding artifact requires its original request")
        object.__setattr__(self, "vector", tuple(self.vector))

    @property
    def channel(self) -> EncodingChannel:
        return self.request.channel


def accept_sparse_result(
    request: SparseEmbeddingRequest,
    *,
    model: str,
    weights: Mapping[str, float],
) -> SparseEmbeddingArtifact:
    if model != request.encoder.model:
        raise ValueError("sparse embedding response model differs from its request")
    return SparseEmbeddingArtifact(
        request=request,
        token_weights=validate_sparse_weights(weights),
    )


def validate_sparse_artifact(
    request: SparseEmbeddingRequest,
    artifact: SparseEmbeddingArtifact,
) -> tuple[tuple[str, float], ...]:
    if artifact.request is not request:
        raise ValueError("sparse artifact belongs to a different plan request")
    return artifact.token_weights


def accept_embedding_result(
    request: EmbeddingRequest,
    *,
    modality: str,
    model: str,
    vector: Sequence[float],
) -> EmbeddingArtifact:
    """Validate an external encoder response before it becomes an artifact."""

    expected_modality = "text" if request.channel == "semantic" else "video"
    if modality != expected_modality or model != request.encoder.model:
        raise ValueError(f"{request.channel} embedding response contract differs")
    validated = validate_embedding_vector(
        vector,
        dimensions=request.encoder.dimensions,
    )
    return EmbeddingArtifact(
        request=request,
        vector=validated,
    )


def validate_embedding_artifact(
    request: EmbeddingRequest,
    artifact: EmbeddingArtifact,
) -> tuple[float, ...]:
    """Validate a result against the request owned by the outer plan."""

    if artifact.channel != request.channel:
        raise ValueError("embedding artifact channel differs from its request")
    if artifact.request is not request:
        raise ValueError("embedding artifact belongs to a different plan request")
    return validate_embedding_vector(
        artifact.vector,
        dimensions=request.encoder.dimensions,
    )


def compute_encoder_id(
    modality: Literal["text", "video"],
    encoder: EncoderContract,
    *,
    visual_input_contract: VisualInputContract | None = None,
) -> str:
    if modality == "text" and visual_input_contract is not None:
        raise ValueError("text encoder ID must not include a visual input contract")
    if modality == "video" and visual_input_contract is None:
        raise ValueError("video encoder ID requires a visual input contract")
    payload = encoder.claims()
    if visual_input_contract is not None:
        payload.update(visual_input_contract.claims())
    return f"{modality}:sha256:{_sha256_json(payload)}"


__all__ = [
    "EmbeddingReuseDecision",
    "EmbeddingReuseTarget",
    "Modality",
    "ReuseGapReason",
    "SourceKind",
    "VisualInputKind",
    "assess_embedding_reuse",
    "embedding_receipt",
    "is_sha256",
    "validate_embedding_vector",
    "validate_sparse_weights",
    "EncoderContract",
    "SparseEncoderContract",
    "SparseEmbeddingRequest",
    "SparseEmbeddingArtifact",
    "EmbeddingRequest",
    "EmbeddingArtifact",
    "accept_sparse_result",
    "validate_sparse_artifact",
    "accept_embedding_result",
    "validate_embedding_artifact",
    "compute_encoder_id",
    "EncodingChannel",
    "DEFAULT_ENCODER_NORMALIZATION",
    "DEFAULT_ENCODER_OUTPUT_CONTRACT"
]
