"""Storage- and provider-neutral Parse V2 Chunk indexing strategy."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from engine.parsev2.schemas import VideoParseResult
from engine.retrieval.contracts import VideoChunkRetrievalIdentity
from engine.retrieval.embedding_reuse import (
    EmbeddingReuseTarget,
    SourceKind,
    assess_embedding_reuse,
    is_sha256,
    validate_embedding_vector,
)
from engine.retrieval.index_projection import (
    ChunkIndexProjection,
    SourceEnrichment,
    compute_index_projection_revision,
    project_chunk_index,
)

INDEX_SOURCE_VERSION = "parse-v2-video-result-v1"
DEFAULT_ENCODER_NORMALIZATION = "none"
DEFAULT_ENCODER_OUTPUT_CONTRACT = "float-vector-v1"
VIDEO_CLIP_PREPROCESS_REVISION = "ffmpeg-h264-crf23-scale-cap-v1"

EncodingChannel = Literal["semantic", "vision"]


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
class VideoClipInputContract:
    """Versioned preprocessing requested for one Chunk video segment."""

    fps: float
    max_height: int
    mime_type: str
    preprocess_revision: str
    kind: Literal["video_clip"] = "video_clip"

    def __post_init__(self) -> None:
        if self.kind != "video_clip":
            raise ValueError("video clip input kind must be video_clip")
        if not _positive_finite(self.fps):
            raise ValueError("video clip fps must be positive and finite")
        object.__setattr__(self, "fps", float(self.fps))
        if (
            isinstance(self.max_height, bool)
            or not isinstance(self.max_height, int)
            or self.max_height <= 0
        ):
            raise ValueError("video clip max_height must be a positive integer")
        if not _nonblank(self.mime_type) or not _nonblank(self.preprocess_revision):
            raise ValueError("video clip MIME type and preprocess revision must be nonblank")

    def claims(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "video_fps": self.fps,
            "max_height": self.max_height,
            "mime_type": self.mime_type,
            "preprocess_revision": self.preprocess_revision,
        }


@dataclass(frozen=True, slots=True)
class OrderedFramesInputContract:
    """Named sampling and preprocessing contract for a time-ordered frame set."""

    sampling_contract_id: str
    mime_type: str
    preprocess_revision: str
    kind: Literal["ordered_frames"] = "ordered_frames"

    def __post_init__(self) -> None:
        if self.kind != "ordered_frames":
            raise ValueError("ordered frames input kind must be ordered_frames")
        if any(
            not _nonblank(value)
            for value in (
                self.sampling_contract_id,
                self.mime_type,
                self.preprocess_revision,
            )
        ):
            raise ValueError("ordered frames input contract fields must be nonblank")

    def claims(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "sampling_contract_id": self.sampling_contract_id,
            "mime_type": self.mime_type,
            "preprocess_revision": self.preprocess_revision,
        }


VisualInputContract: TypeAlias = VideoClipInputContract | OrderedFramesInputContract


@dataclass(frozen=True, slots=True)
class VideoClipManifest:
    """A prepared segment; input_sha256 is the digest of its actual bytes."""

    input_sha256: str
    start_seconds: int
    end_seconds: int
    fps: float
    max_height: int
    mime_type: str
    preprocess_revision: str
    kind: Literal["video_clip"] = "video_clip"

    def __post_init__(self) -> None:
        if self.kind != "video_clip":
            raise ValueError("video clip manifest kind must be video_clip")
        if not is_sha256(self.input_sha256):
            raise ValueError("video clip input_sha256 must be lowercase SHA-256")
        _validate_bounds(self.start_seconds, self.end_seconds)
        contract = VideoClipInputContract(
            fps=self.fps,
            max_height=self.max_height,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )
        object.__setattr__(self, "fps", contract.fps)

    @property
    def input_contract(self) -> VideoClipInputContract:
        return VideoClipInputContract(
            fps=self.fps,
            max_height=self.max_height,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )


@dataclass(frozen=True, slots=True)
class OrderedFrame:
    """One prepared frame with its absolute source-video time."""

    ordinal: int
    at_seconds: float
    input_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ValueError("frame ordinal must be a nonnegative integer")
        if not _nonnegative_finite(self.at_seconds):
            raise ValueError("frame at_seconds must be nonnegative and finite")
        object.__setattr__(self, "at_seconds", float(self.at_seconds))
        if not is_sha256(self.input_sha256):
            raise ValueError("frame input_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class OrderedFramesManifest:
    """Realized ordered frames without storage paths or provider request types."""

    frames: tuple[OrderedFrame, ...]
    sampling_contract_id: str
    mime_type: str
    preprocess_revision: str
    kind: Literal["ordered_frames"] = "ordered_frames"

    def __post_init__(self) -> None:
        if self.kind != "ordered_frames":
            raise ValueError("ordered frames manifest kind must be ordered_frames")
        frames = tuple(self.frames)
        object.__setattr__(self, "frames", frames)
        if not frames:
            raise ValueError("ordered frames manifest must contain at least one frame")
        if [frame.ordinal for frame in frames] != list(range(len(frames))):
            raise ValueError("frame ordinals must be consecutive and ordered")
        if any(
            current.at_seconds < previous.at_seconds
            for previous, current in zip(frames, frames[1:])
        ):
            raise ValueError("frames must be ordered by at_seconds")
        OrderedFramesInputContract(
            sampling_contract_id=self.sampling_contract_id,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )

    @property
    def input_contract(self) -> OrderedFramesInputContract:
        return OrderedFramesInputContract(
            sampling_contract_id=self.sampling_contract_id,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )


VisualInputManifest: TypeAlias = VideoClipManifest | OrderedFramesManifest


@dataclass(frozen=True, slots=True)
class ChunkIndexSource:
    """Formal Chunk facts plus a realized visual input, with no DB or path DTOs."""

    workspace_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    result: VideoParseResult
    visual_input: VisualInputManifest
    enrichments: tuple[SourceEnrichment, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not _nonblank(value)
            for value in (
                self.workspace_id,
                self.series_id,
                self.video_id,
                self.video_chunk_id,
                self.parse_task_id,
            )
        ):
            raise ValueError("Chunk index source identity fields must be nonblank")
        if (
            isinstance(self.content_version, bool)
            or not isinstance(self.content_version, int)
            or self.content_version <= 0
        ):
            raise ValueError("content_version must be a positive integer")
        _validate_bounds(self.chunk_start_seconds, self.chunk_end_seconds)
        object.__setattr__(self, "result", VideoParseResult.model_validate(self.result))
        object.__setattr__(self, "enrichments", tuple(self.enrichments))


@dataclass(frozen=True, slots=True)
class ChunkIndexingContract:
    retrieval_contract_id: str
    semantic_encoder: EncoderContract
    vision_encoder: EncoderContract
    visual_input_contract: VisualInputContract
    description_language: str | None = None
    index_generation: int = 1

    def __post_init__(self) -> None:
        if not _nonblank(self.retrieval_contract_id):
            raise ValueError("retrieval_contract_id must be nonblank")
        if self.description_language is not None and not _nonblank(
            self.description_language
        ):
            raise ValueError("description_language must be nonblank when provided")
        if (
            isinstance(self.index_generation, bool)
            or not isinstance(self.index_generation, int)
            or self.index_generation <= 0
        ):
            raise ValueError("index_generation must be a positive integer")

    @property
    def semantic_encoder_id(self) -> str:
        return compute_encoder_id("text", self.semantic_encoder)

    @property
    def vision_input_contract_id(self) -> str:
        return compute_visual_input_contract_id(self.visual_input_contract)

    @property
    def vision_encoder_id(self) -> str:
        return compute_encoder_id(
            "video",
            self.vision_encoder,
            visual_input_contract=self.visual_input_contract,
        )


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    channel: EncodingChannel
    target: EmbeddingReuseTarget
    semantic_text: str | None = None
    visual_input: VisualInputManifest | None = None

    def __post_init__(self) -> None:
        if self.channel == "semantic":
            if self.target.modality != "text":
                raise ValueError("semantic request requires a text target")
            if not _nonblank(self.semantic_text) or self.visual_input is not None:
                raise ValueError("semantic request requires only semantic_text")
        elif self.channel == "vision":
            if self.target.modality != "video":
                raise ValueError("vision request requires a video target")
            if self.semantic_text is not None or self.visual_input is None:
                raise ValueError("vision request requires only visual_input")
        else:
            raise ValueError("embedding channel must be semantic or vision")


@dataclass(frozen=True, slots=True)
class EmbeddingArtifact:
    channel: EncodingChannel
    input_sha256: str
    encoder_id: str
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.channel not in ("semantic", "vision"):
            raise ValueError("embedding channel must be semantic or vision")
        if not is_sha256(self.input_sha256):
            raise ValueError("artifact input_sha256 must be lowercase SHA-256")
        if not _nonblank(self.encoder_id):
            raise ValueError("artifact encoder_id must be nonblank")
        object.__setattr__(self, "vector", tuple(self.vector))


@dataclass(frozen=True, slots=True)
class ChunkIndexPlan:
    workspace_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    source_revision: str
    projection_revision: str
    retrieval_contract_id: str
    index_generation: int
    semantic_encoder_id: str
    vision_encoder_id: str
    chunk_start_seconds: int
    chunk_end_seconds: int
    projection: ChunkIndexProjection
    semantic_request: EmbeddingRequest | None
    vision_request: EmbeddingRequest

    @property
    def encoding_requests(self) -> tuple[EmbeddingRequest, ...]:
        if self.semantic_request is None:
            return (self.vision_request,)
        return (self.semantic_request, self.vision_request)


@dataclass(frozen=True, slots=True)
class ChunkIndexReceipt:
    """Persistable proof of the exact logical index build for one Chunk."""

    parse_task_id: str
    video_chunk_id: str
    content_version: int
    source_revision: str
    projection_revision: str
    retrieval_contract_id: str
    semantic_encoder_id: str
    vision_encoder_id: str
    index_generation: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "parse_task_id": self.parse_task_id,
            "video_chunk_id": self.video_chunk_id,
            "content_version": self.content_version,
            "source_revision": self.source_revision,
            "projection_revision": self.projection_revision,
            "retrieval_contract_id": self.retrieval_contract_id,
            "semantic_encoder_id": self.semantic_encoder_id,
            "vision_encoder_id": self.vision_encoder_id,
            "index_generation": self.index_generation,
        }


@dataclass(frozen=True, slots=True)
class VideoChunkIndexDocument:
    """Logical index document; physical IDs and versions belong to the store."""

    workspace_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    source_revision: str
    projection_revision: str
    retrieval_contract_id: str
    index_generation: int
    semantic_encoder_id: str
    vision_encoder_id: str
    semantic_input_sha256: str | None
    vision_input_sha256: str
    chunk_start_seconds: int
    chunk_end_seconds: int
    projection: ChunkIndexProjection
    semantic_vector: tuple[float, ...] | None
    vision_vector: tuple[float, ...]

    @property
    def receipt(self) -> ChunkIndexReceipt:
        return ChunkIndexReceipt(
            parse_task_id=self.parse_task_id,
            video_chunk_id=self.video_chunk_id,
            content_version=self.content_version,
            source_revision=self.source_revision,
            projection_revision=self.projection_revision,
            retrieval_contract_id=self.retrieval_contract_id,
            semantic_encoder_id=self.semantic_encoder_id,
            vision_encoder_id=self.vision_encoder_id,
            index_generation=self.index_generation,
        )

    @property
    def retrieval_identity(self) -> VideoChunkRetrievalIdentity:
        """Return the exact identity a storage hit must retain through fusion."""

        return VideoChunkRetrievalIdentity(
            workspace_id=self.workspace_id,
            series_id=self.series_id,
            video_id=self.video_id,
            parse_task_id=self.parse_task_id,
            content_version=self.content_version,
            index_generation=self.index_generation,
            source_revision=self.source_revision,
            projection_revision=self.projection_revision,
            retrieval_contract_id=self.retrieval_contract_id,
            semantic_encoder_id=self.semantic_encoder_id,
            vision_encoder_id=self.vision_encoder_id,
        )


def plan_chunk_index(
    source: ChunkIndexSource,
    contract: ChunkIndexingContract,
) -> ChunkIndexPlan:
    """Project formal facts and create exact requests without performing I/O."""

    _validate_visual_input(source, contract.visual_input_contract)
    projection = project_chunk_index(
        source.result,
        chunk_start_seconds=source.chunk_start_seconds,
        chunk_end_seconds=source.chunk_end_seconds,
        description_language=contract.description_language,
        enrichments=source.enrichments,
    )

    semantic_text = projection.semantic_text.strip()
    semantic_request: EmbeddingRequest | None = None
    if semantic_text:
        semantic_input_sha256 = hashlib.sha256(semantic_text.encode("utf-8")).hexdigest()
        semantic_request = EmbeddingRequest(
            channel="semantic",
            target=_target(
                modality="text",
                input_sha256=semantic_input_sha256,
                encoder=contract.semantic_encoder,
                encoder_id=contract.semantic_encoder_id,
            ),
            semantic_text=semantic_text,
        )

    vision_request = EmbeddingRequest(
        channel="vision",
        target=_visual_target(source.visual_input, contract),
        visual_input=source.visual_input,
    )
    return ChunkIndexPlan(
        workspace_id=source.workspace_id,
        series_id=source.series_id,
        video_id=source.video_id,
        video_chunk_id=source.video_chunk_id,
        parse_task_id=source.parse_task_id,
        content_version=source.content_version,
        source_revision=compute_source_revision(source.result),
        projection_revision=compute_index_projection_revision(projection),
        retrieval_contract_id=contract.retrieval_contract_id,
        index_generation=contract.index_generation,
        semantic_encoder_id=contract.semantic_encoder_id,
        vision_encoder_id=contract.vision_encoder_id,
        chunk_start_seconds=source.chunk_start_seconds,
        chunk_end_seconds=source.chunk_end_seconds,
        projection=projection,
        semantic_request=semantic_request,
        vision_request=vision_request,
    )


def finalize_chunk_index(
    plan: ChunkIndexPlan,
    artifacts: Sequence[EmbeddingArtifact],
) -> VideoChunkIndexDocument:
    """Join exact encoding artifacts to a plan and emit one logical document."""

    if compute_index_projection_revision(plan.projection) != plan.projection_revision:
        raise ValueError("index projection changed after planning")
    by_channel: dict[EncodingChannel, EmbeddingArtifact] = {}
    for artifact in artifacts:
        if artifact.channel in by_channel:
            raise ValueError(f"duplicate {artifact.channel} embedding artifact")
        by_channel[artifact.channel] = artifact

    expected = {request.channel for request in plan.encoding_requests}
    if set(by_channel) != expected:
        raise ValueError("embedding artifacts do not match the index plan")
    semantic_vector = (
        validate_embedding_artifact(plan.semantic_request, by_channel["semantic"])
        if plan.semantic_request is not None
        else None
    )
    vision_vector = validate_embedding_artifact(
        plan.vision_request,
        by_channel["vision"],
    )
    return VideoChunkIndexDocument(
        workspace_id=plan.workspace_id,
        series_id=plan.series_id,
        video_id=plan.video_id,
        video_chunk_id=plan.video_chunk_id,
        parse_task_id=plan.parse_task_id,
        content_version=plan.content_version,
        source_revision=plan.source_revision,
        projection_revision=plan.projection_revision,
        retrieval_contract_id=plan.retrieval_contract_id,
        index_generation=plan.index_generation,
        semantic_encoder_id=plan.semantic_encoder_id,
        vision_encoder_id=plan.vision_encoder_id,
        semantic_input_sha256=(
            plan.semantic_request.target.input_sha256
            if plan.semantic_request is not None
            else None
        ),
        vision_input_sha256=plan.vision_request.target.input_sha256,
        chunk_start_seconds=plan.chunk_start_seconds,
        chunk_end_seconds=plan.chunk_end_seconds,
        projection=plan.projection,
        semantic_vector=semantic_vector,
        vision_vector=vision_vector,
    )


def accept_embedding_result(
    request: EmbeddingRequest,
    *,
    modality: str,
    model: str,
    vector: Sequence[float],
) -> EmbeddingArtifact:
    """Validate an external encoder response before it becomes an artifact."""

    if modality != request.target.modality or model != request.target.model:
        raise ValueError(f"{request.channel} embedding response contract differs")
    validated = validate_embedding_vector(
        vector,
        dimensions=request.target.dimensions,
    )
    return EmbeddingArtifact(
        channel=request.channel,
        input_sha256=request.target.input_sha256,
        encoder_id=request.target.encoder_id,
        vector=validated,
    )


def validate_embedding_artifact(
    request: EmbeddingRequest,
    artifact: EmbeddingArtifact,
) -> tuple[float, ...]:
    """Validate a cached or persisted artifact against one exact request."""

    if artifact.channel != request.channel:
        raise ValueError("embedding artifact channel differs from its request")
    if artifact.input_sha256 != request.target.input_sha256:
        raise ValueError("embedding artifact input differs from its request")
    if artifact.encoder_id != request.target.encoder_id:
        raise ValueError("embedding artifact encoder differs from its request")
    return validate_embedding_vector(
        artifact.vector,
        dimensions=request.target.dimensions,
    )


def reuse_embedding_artifact(
    request: EmbeddingRequest,
    *,
    source_kind: SourceKind,
    metadata: Mapping[str, object],
    vector: Sequence[float] | None,
) -> EmbeddingArtifact | None:
    """Return a plan-bound artifact only when persisted proof is compatible."""

    decision = assess_embedding_reuse(
        request.target,
        source_kind=source_kind,
        metadata=metadata,
        vector=vector,
    )
    if decision.vector is None:
        return None
    return EmbeddingArtifact(
        channel=request.channel,
        input_sha256=request.target.input_sha256,
        encoder_id=request.target.encoder_id,
        vector=decision.vector,
    )


def compute_source_revision(result: VideoParseResult) -> str:
    """Hash only canonical formal source content and its Source contract version."""

    validated = VideoParseResult.model_validate(result)
    return _sha256_json({
        "source_version": INDEX_SOURCE_VERSION,
        "content": validated.model_dump(mode="json"),
    })


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


def compute_visual_input_contract_id(contract: VisualInputContract) -> str:
    return f"{contract.kind}:sha256:{_sha256_json(contract.claims())}"


def compute_visual_input_revision(manifest: VisualInputManifest) -> str:
    """Return the exact encoder input identity for clip bytes or ordered frames."""

    if isinstance(manifest, VideoClipManifest):
        return manifest.input_sha256
    return _sha256_json({
        "kind": manifest.kind,
        "sampling_contract_id": manifest.sampling_contract_id,
        "mime_type": manifest.mime_type,
        "preprocess_revision": manifest.preprocess_revision,
        "frames": [
            {
                "ordinal": frame.ordinal,
                "at_seconds": frame.at_seconds,
                "input_sha256": frame.input_sha256,
            }
            for frame in manifest.frames
        ],
    })


def _target(
    *,
    modality: Literal["text", "video"],
    input_sha256: str,
    encoder: EncoderContract,
    encoder_id: str,
) -> EmbeddingReuseTarget:
    return EmbeddingReuseTarget(
        modality=modality,
        input_sha256=input_sha256,
        provider=encoder.provider,
        model=encoder.model,
        encoder_revision=encoder.encoder_revision,
        dimensions=encoder.dimensions,
        normalization=encoder.normalization,
        output_contract=encoder.output_contract,
        encoder_id=encoder_id,
    )


def _visual_target(
    manifest: VisualInputManifest,
    contract: ChunkIndexingContract,
) -> EmbeddingReuseTarget:
    common = {
        "modality": "video",
        "input_sha256": compute_visual_input_revision(manifest),
        "provider": contract.vision_encoder.provider,
        "model": contract.vision_encoder.model,
        "encoder_revision": contract.vision_encoder.encoder_revision,
        "dimensions": contract.vision_encoder.dimensions,
        "normalization": contract.vision_encoder.normalization,
        "output_contract": contract.vision_encoder.output_contract,
        "encoder_id": contract.vision_encoder_id,
        "input_contract_id": contract.vision_input_contract_id,
        "visual_input_kind": manifest.kind,
        "mime_type": manifest.mime_type,
        "preprocess_revision": manifest.preprocess_revision,
    }
    if isinstance(manifest, VideoClipManifest):
        return EmbeddingReuseTarget(
            **common,
            video_fps=manifest.fps,
            max_height=manifest.max_height,
        )
    return EmbeddingReuseTarget(
        **common,
        sampling_contract_id=manifest.sampling_contract_id,
    )


def _validate_visual_input(
    source: ChunkIndexSource,
    contract: VisualInputContract,
) -> None:
    manifest = source.visual_input
    if manifest.input_contract != contract:
        raise ValueError("visual input manifest differs from the indexing contract")
    if isinstance(manifest, VideoClipManifest):
        if (
            manifest.start_seconds != source.chunk_start_seconds
            or manifest.end_seconds != source.chunk_end_seconds
        ):
            raise ValueError("video clip manifest must cover the exact Chunk range")
        return
    if any(
        frame.at_seconds < source.chunk_start_seconds
        or frame.at_seconds >= source.chunk_end_seconds
        for frame in manifest.frames
    ):
        raise ValueError("ordered frame time falls outside the half-open Chunk range")


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_bounds(start_seconds: int, end_seconds: int) -> None:
    if (
        isinstance(start_seconds, bool)
        or not isinstance(start_seconds, int)
        or start_seconds < 0
        or isinstance(end_seconds, bool)
        or not isinstance(end_seconds, int)
        or end_seconds <= start_seconds
    ):
        raise ValueError("Chunk bounds must be increasing nonnegative integer seconds")


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_finite(value: object) -> bool:
    return _nonnegative_finite(value) and value > 0  # type: ignore[operator]


def _nonnegative_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


__all__ = [
    "ChunkIndexPlan",
    "ChunkIndexReceipt",
    "ChunkIndexSource",
    "ChunkIndexingContract",
    "DEFAULT_ENCODER_NORMALIZATION",
    "DEFAULT_ENCODER_OUTPUT_CONTRACT",
    "EmbeddingArtifact",
    "EmbeddingRequest",
    "EncoderContract",
    "EncodingChannel",
    "INDEX_SOURCE_VERSION",
    "OrderedFrame",
    "OrderedFramesInputContract",
    "OrderedFramesManifest",
    "VideoChunkIndexDocument",
    "VideoClipInputContract",
    "VideoClipManifest",
    "VIDEO_CLIP_PREPROCESS_REVISION",
    "VisualInputContract",
    "VisualInputManifest",
    "accept_embedding_result",
    "compute_encoder_id",
    "compute_source_revision",
    "compute_visual_input_contract_id",
    "compute_visual_input_revision",
    "finalize_chunk_index",
    "plan_chunk_index",
    "reuse_embedding_artifact",
    "validate_embedding_artifact",
]
