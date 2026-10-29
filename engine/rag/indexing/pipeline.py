"""Storage-neutral Source → projection → encoding plan → index document."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from engine.parsev2.schemas import VideoParseResult
from engine.rag._validation import _nonblank, _sha256_json, _validate_bounds
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import (
    EmbeddingArtifact,
    EmbeddingRequest,
    EncoderContract,
    EncodingChannel,
    SparseEmbeddingArtifact,
    SparseEmbeddingRequest,
    SparseEncoderContract,
    compute_encoder_id,
    validate_embedding_artifact,
    validate_sparse_artifact,
)
from engine.rag.indexing.projection import (
    ChunkIndexProjection, SourceEnrichment, compute_index_projection_revision, project_chunk_index,
)
from engine.rag.vision.inputs import (
    OrderedFramesManifest,
    VideoClipManifest,
    VisualInputContract,
    VisualInputManifest,
    compute_visual_input_contract_id,
    plan_ordered_frame_timestamps,
)

INDEX_SOURCE_VERSION = "parse-v2-video-result-v1"

@dataclass(frozen=True, slots=True)
class ChunkIndexSource:
    """Formal Chunk facts and visual input; fact/enrichment changes advance the version."""

    tenant_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    result: VideoParseResult
    visual_input: VisualInputManifest | None
    enrichments: tuple[SourceEnrichment, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not _nonblank(value)
            for value in (
                self.tenant_id,
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
    """Caller keeps configuration fixed per generation and advances it for rule/model changes."""

    retrieval_contract_id: str
    semantic_encoder: EncoderContract
    vision_encoder: EncoderContract | None
    visual_input_contract: VisualInputContract | None
    description_language: str | None = None
    index_generation: int = 1
    sparse_encoder: SparseEncoderContract | None = None

    def __post_init__(self) -> None:
        if (self.vision_encoder is None) != (self.visual_input_contract is None):
            raise ValueError("vision encoder and visual input contract must be enabled together")
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
    def vision_input_contract_id(self) -> str | None:
        if self.visual_input_contract is None:
            return None
        return compute_visual_input_contract_id(self.visual_input_contract)

    @property
    def vision_encoder_id(self) -> str | None:
        if self.vision_encoder is None:
            return None
        return compute_encoder_id(
            "video",
            self.vision_encoder,
            visual_input_contract=self.visual_input_contract,
        )


@dataclass(frozen=True, slots=True)
class ChunkIndexPlan:
    tenant_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    projection_revision: str
    index_generation: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    projection: ChunkIndexProjection
    semantic_request: EmbeddingRequest | None
    vision_request: EmbeddingRequest | None
    sparse_encoder: SparseEncoderContract | None = None
    sparse_request: SparseEmbeddingRequest | None = None

    def __post_init__(self) -> None:
        # Each plan owns its requests, including plans copied with another version.
        # Results retain these objects, not a separate embedding identity.
        for name in ("semantic_request", "vision_request", "sparse_request"):
            request = getattr(self, name)
            if request is not None:
                object.__setattr__(self, name, replace(request))

    @property
    def encoding_requests(self) -> tuple[EmbeddingRequest, ...]:
        return tuple(request for request in (self.semantic_request, self.vision_request) if request is not None)


@dataclass(frozen=True, slots=True)
class ChunkIndexReceipt:
    """Persistable proof of the exact logical index build for one Chunk."""

    parse_task_id: str
    video_chunk_id: str
    content_version: int
    index_generation: int
    ordered_frames_manifest: OrderedFramesManifest | None = None

    def as_dict(self) -> dict[str, object]:
        receipt: dict[str, object] = {
            "parse_task_id": self.parse_task_id,
            "video_chunk_id": self.video_chunk_id,
            "content_version": self.content_version,
            "index_generation": self.index_generation,
        }
        if self.ordered_frames_manifest is not None:
            receipt["ordered_frames_manifest"] = self.ordered_frames_manifest.as_dict()
        return receipt


@dataclass(frozen=True, slots=True)
class VideoChunkIndexDocument:
    """Logical index document; physical IDs and versions belong to the store."""

    tenant_id: str
    series_id: str
    video_id: str
    video_chunk_id: str
    parse_task_id: str
    content_version: int
    index_generation: int
    chunk_start_seconds: int
    chunk_end_seconds: int
    projection: ChunkIndexProjection
    semantic_vector: tuple[float, ...] | None
    vision_vector: tuple[float, ...] | None
    sparse_token_weights: tuple[tuple[str, float], ...] | None = None
    ordered_frames_manifest: OrderedFramesManifest | None = None

    @property
    def receipt(self) -> ChunkIndexReceipt:
        return ChunkIndexReceipt(
            parse_task_id=self.parse_task_id,
            video_chunk_id=self.video_chunk_id,
            content_version=self.content_version,
            index_generation=self.index_generation,
            ordered_frames_manifest=self.ordered_frames_manifest,
        )

    @property
    def retrieval_identity(self) -> VideoChunkRetrievalIdentity:
        """Return the exact identity a storage hit must retain through fusion."""

        return VideoChunkRetrievalIdentity(
            tenant_id=self.tenant_id,
            series_id=self.series_id,
            video_id=self.video_id,
            parse_task_id=self.parse_task_id,
            content_version=self.content_version,
            index_generation=self.index_generation,
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
        semantic_request = EmbeddingRequest(
            channel="semantic",
            encoder=contract.semantic_encoder,
            semantic_text=semantic_text,
        )

    vision_request = EmbeddingRequest(
        channel="vision",
        encoder=contract.vision_encoder,
        visual_input=source.visual_input,
    ) if contract.vision_encoder is not None else None
    sparse_request = (
        SparseEmbeddingRequest(
            encoder=contract.sparse_encoder,
            text=semantic_text,
        )
        if contract.sparse_encoder is not None and semantic_text
        else None
    )
    return ChunkIndexPlan(
        tenant_id=source.tenant_id,
        series_id=source.series_id,
        video_id=source.video_id,
        video_chunk_id=source.video_chunk_id,
        parse_task_id=source.parse_task_id,
        content_version=source.content_version,
        projection_revision=compute_index_projection_revision(projection),
        index_generation=contract.index_generation,
        chunk_start_seconds=source.chunk_start_seconds,
        chunk_end_seconds=source.chunk_end_seconds,
        projection=projection,
        semantic_request=semantic_request,
        vision_request=vision_request,
        sparse_encoder=contract.sparse_encoder,
        sparse_request=sparse_request,
    )


def finalize_chunk_index(
    plan: ChunkIndexPlan,
    artifacts: Sequence[EmbeddingArtifact],
    *,
    sparse_artifact: SparseEmbeddingArtifact | None = None,
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
    ) if plan.vision_request is not None else None
    sparse_weights = None
    if (
        plan.sparse_request is None
        and plan.sparse_encoder is not None
        and plan.projection.semantic_text.strip()
    ):
        raise ValueError("sparse request is required by the index plan")
    if plan.sparse_request is not None:
        if (
            plan.sparse_request.encoder != plan.sparse_encoder
            or plan.sparse_request.text != plan.projection.semantic_text.strip()
        ):
            raise ValueError("sparse request differs from the index plan")
        if sparse_artifact is None:
            raise ValueError("sparse artifact is required by the index plan")
        sparse_weights = validate_sparse_artifact(plan.sparse_request, sparse_artifact)
    elif sparse_artifact is not None:
        raise ValueError("sparse artifact is not requested by the index plan")
    return VideoChunkIndexDocument(
        tenant_id=plan.tenant_id,
        series_id=plan.series_id,
        video_id=plan.video_id,
        video_chunk_id=plan.video_chunk_id,
        parse_task_id=plan.parse_task_id,
        content_version=plan.content_version,
        index_generation=plan.index_generation,
        chunk_start_seconds=plan.chunk_start_seconds,
        chunk_end_seconds=plan.chunk_end_seconds,
        projection=plan.projection,
        semantic_vector=semantic_vector,
        vision_vector=vision_vector,
        sparse_token_weights=sparse_weights,
        ordered_frames_manifest=(
            plan.vision_request.visual_input
            if plan.vision_request is not None
            and isinstance(plan.vision_request.visual_input, OrderedFramesManifest)
            and plan.vision_request.visual_input.sampling is not None
            else None
        ),
    )


def compute_source_revision(result: VideoParseResult) -> str:
    """Hash only canonical formal source content and its Source contract version."""

    validated = VideoParseResult.model_validate(result)
    return _sha256_json({
        "source_version": INDEX_SOURCE_VERSION,
        "content": validated.model_dump(mode="json"),
    })


def _validate_visual_input(
    source: ChunkIndexSource,
    contract: VisualInputContract | None,
) -> None:
    manifest = source.visual_input
    if contract is None:
        if manifest is not None:
            raise ValueError("text-only indexing must not declare visual input")
        return
    if manifest is None:
        raise ValueError("multimodal indexing requires visual input")
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
    if contract.sampling is not None:
        planned_times = plan_ordered_frame_timestamps(
            source.chunk_start_seconds, source.chunk_end_seconds, contract.sampling,
        )
        if tuple(frame.requested_at_seconds for frame in manifest.frames) != planned_times:
            raise ValueError("ordered frames do not match the sampling time plan")


__all__ = [
    "ChunkIndexPlan",
    "ChunkIndexReceipt",
    "ChunkIndexSource",
    "ChunkIndexingContract",
    "INDEX_SOURCE_VERSION",
    "VideoChunkIndexDocument",
    "compute_source_revision",
    "finalize_chunk_index",
    "plan_chunk_index",
]
