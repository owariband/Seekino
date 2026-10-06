"""Storage-neutral Source → projection → encoding plan → index document."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from engine.parsev2.schemas import VideoParseResult
from engine.rag._validation import _nonblank, _sha256_json, _validate_bounds
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import (
    EmbeddingArtifact,
    EmbeddingRequest,
    EmbeddingReuseTarget,
    EncoderContract,
    EncodingChannel,
    SparseEmbeddingArtifact,
    SparseEmbeddingRequest,
    SparseEncoderContract,
    _target,
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
    compute_visual_input_revision,
    plan_ordered_frame_timestamps,
)

INDEX_SOURCE_VERSION = "parse-v2-video-result-v1"

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
    sparse_encoder: SparseEncoderContract | None = None

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
    sparse_encoder_id: str | None = None
    sparse_request: SparseEmbeddingRequest | None = None

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
    sparse_encoder_id: str | None = None
    ordered_frames_manifest: OrderedFramesManifest | None = None

    def as_dict(self) -> dict[str, object]:
        receipt: dict[str, object] = {
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
        if self.sparse_encoder_id is not None:
            receipt["sparse_encoder_id"] = self.sparse_encoder_id
        if self.ordered_frames_manifest is not None:
            receipt["ordered_frames_manifest"] = self.ordered_frames_manifest.as_dict()
        return receipt


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
    sparse_encoder_id: str | None = None
    sparse_input_sha256: str | None = None
    sparse_token_weights: tuple[tuple[str, float], ...] | None = None
    ordered_frames_manifest: OrderedFramesManifest | None = None

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
            sparse_encoder_id=self.sparse_encoder_id,
            ordered_frames_manifest=self.ordered_frames_manifest,
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
            sparse_encoder_id=self.sparse_encoder_id,
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
    sparse_request = (
        SparseEmbeddingRequest(
            encoder=contract.sparse_encoder,
            input_sha256=hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
            text=semantic_text,
        )
        if contract.sparse_encoder is not None and semantic_text
        else None
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
        sparse_encoder_id=(
            contract.sparse_encoder.encoder_id if contract.sparse_encoder is not None else None
        ),
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
    )
    sparse_weights = None
    if (
        plan.sparse_request is None
        and plan.sparse_encoder_id is not None
        and plan.projection.semantic_text.strip()
    ):
        raise ValueError("sparse request is required by the index plan")
    if plan.sparse_request is not None:
        if (
            plan.sparse_request.encoder.encoder_id != plan.sparse_encoder_id
            or plan.sparse_request.text != plan.projection.semantic_text.strip()
        ):
            raise ValueError("sparse request differs from the index plan")
        if sparse_artifact is None:
            raise ValueError("sparse artifact is required by the index plan")
        sparse_weights = validate_sparse_artifact(plan.sparse_request, sparse_artifact)
    elif sparse_artifact is not None:
        raise ValueError("sparse artifact is not requested by the index plan")
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
        sparse_encoder_id=plan.sparse_encoder_id,
        sparse_input_sha256=(
            plan.sparse_request.input_sha256 if plan.sparse_request is not None else None
        ),
        sparse_token_weights=sparse_weights,
        ordered_frames_manifest=(
            plan.vision_request.visual_input
            if isinstance(plan.vision_request.visual_input, OrderedFramesManifest)
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
