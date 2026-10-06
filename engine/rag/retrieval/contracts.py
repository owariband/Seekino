"""Query signals, trusted scope, candidates and formal evidence contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import VideoParseResult
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import (
    EmbeddingReuseTarget, EncoderContract, SparseEmbeddingRequest, compute_encoder_id,
)
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.indexing.projection import LexicalField, SourceEnrichment
from engine.rag.retrieval.fusion import (
    VersionedFusedVideoChunkRanking, normalize_hybrid_retrieval_weights,
)

RecallChannel = Literal["lexical", "semantic", "vision", "sparse"]
_NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
_PositiveInt = Annotated[int, Field(gt=0, strict=True)]
_Weight = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class RetrievalConsistencyError(ValueError):
    """Returned index/fact data cannot substantiate the pinned query identity."""


class _QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class SelectedVideoTask(_QueryModel):
    """Trusted selection supplied by the caller, never inferred from ES hits."""

    series_id: _NonBlank
    video_id: _NonBlank
    parse_task_id: _NonBlank
    status: Literal["COMPLETE"]


class RetrievalScope(_QueryModel):
    workspace_id: _NonBlank
    tasks: tuple[SelectedVideoTask, ...]

    @model_validator(mode="after")
    def unique_videos(self) -> RetrievalScope:
        if len({task.video_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("scope must select exactly one COMPLETE task per Video")
        if len({task.parse_task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("scope contains duplicate parse_task_id")
        return self


class RetrievalPolicy(_QueryModel):
    """Caller-supplied budgets/weights; this pipeline does not choose new defaults."""

    lexical_weight: _Weight
    semantic_weight: _Weight
    vision_weight: _Weight
    sparse_weight: _Weight = 0.0
    channel_top_k: _PositiveInt
    candidate_budget: _PositiveInt
    top_k: _PositiveInt
    rrf_k: _PositiveInt

    @model_validator(mode="after")
    def validate_policy(self) -> RetrievalPolicy:
        if not any(self.weights.values()):
            raise ValueError("at least one recall channel must be enabled")
        if self.candidate_budget < self.top_k:
            raise ValueError("candidate_budget must cover top_k")
        return self

    @property
    def weights(self) -> dict[RecallChannel, float]:
        return dict(zip(
            ("lexical", "semantic", "vision", "sparse"),
            (self.lexical_weight, self.semantic_weight, self.vision_weight, self.sparse_weight),
        ))

    @property
    def normalized_weights(self) -> dict[RecallChannel, float]:
        if not self.sparse_weight:
            weights = normalize_hybrid_retrieval_weights(
                self.lexical_weight, self.semantic_weight, self.vision_weight,
            )
            return dict(zip(("lexical", "semantic", "vision", "sparse"), (*weights, 0.0)))
        scale = max(self.weights.values())
        scaled = {channel: weight / scale for channel, weight in self.weights.items()}
        total = sum(scaled.values())
        return {channel: weight / total for channel, weight in scaled.items()}


class EventConstraint(_QueryModel):
    """An explicit literal condition on one real event, not inferred query intent."""

    key: _NonBlank
    field: Literal["dialogue", "screen_text", "person_names", "event_description"]
    text: _NonBlank
    match: Literal["exact", "contains"] = "exact"
    speaker: _NonBlank | None = None
    language: _NonBlank | None = None
    role: Literal["ORIGINAL", "TRANSLATION"] | None = None
    event_type: Literal[
        "SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"
    ] | None = None
    visible_person: _NonBlank | None = None
    include_context: bool = False

    @model_validator(mode="after")
    def validate_dialogue_conditions(self) -> EventConstraint:
        if self.field != "dialogue" and (self.speaker is not None or self.role is not None):
            raise ValueError("speaker/role conditions require dialogue")
        return self


class TemporalConstraint(_QueryModel):
    left: _NonBlank
    right: _NonBlank
    relation: Literal["before", "overlap"]


class QueryEncoderBinding(_QueryModel):
    """Declared query/document compatibility, whose protocol proof is external."""

    channel: Literal["semantic", "vision"]
    document_encoder_id: _NonBlank
    query_encoder: EncoderContract
    compatibility_provenance: _NonBlank


@dataclass(frozen=True, slots=True)
class QueryEncodingRequest:
    channels: tuple[Literal["semantic", "vision"], ...]
    text: str
    encoder: EncoderContract

    @property
    def target(self) -> EmbeddingReuseTarget:
        return EmbeddingReuseTarget(
            modality="text", input_sha256=hashlib.sha256(self.text.encode()).hexdigest(),
            encoder_id=compute_encoder_id("text", self.encoder), **self.encoder.claims(),
        )


class QueryVectorArtifact(_QueryModel):
    input_sha256: _NonBlank
    encoder_id: _NonBlank
    vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]


@dataclass(frozen=True, slots=True)
class RetrievalQueryPlan:
    query: str
    scope: RetrievalScope
    index_contract: ChunkIndexingContract
    policy: RetrievalPolicy
    encoder_bindings: tuple[QueryEncoderBinding, ...]
    encoding_requests: tuple[QueryEncodingRequest, ...]
    sparse_request: SparseEmbeddingRequest | None
    constraints: tuple[EventConstraint, ...]
    temporal_constraints: tuple[TemporalConstraint, ...]


@dataclass(frozen=True, slots=True)
class RecallRequest:
    """Store-neutral expression; the adapter chooses physical mapping and DSL."""

    channel: RecallChannel
    scope: RetrievalScope
    retrieval_contract_id: str
    index_generation: int
    limit: int
    document_encoder_id: str | None = None
    text: str | None = None
    vector: tuple[float, ...] | None = None
    token_weights: tuple[tuple[str, float], ...] | None = None


class RawChunkHit(_QueryModel):
    channel: RecallChannel
    video_chunk_id: _NonBlank
    retrieval_identity: VideoChunkRetrievalIdentity
    rank: _PositiveInt
    score: float = Field(allow_inf_nan=False, strict=True)
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: _PositiveInt
    lexical_matches: tuple[LexicalField, ...] = ()

    @model_validator(mode="after")
    def validate_hit(self) -> RawChunkHit:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("hit bounds must be increasing")
        if self.channel != "lexical" and self.lexical_matches:
            raise ValueError("only lexical hits carry matched lexical sources")
        return self


class RetrievalCandidate(_QueryModel):
    ranking: VersionedFusedVideoChunkRanking
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: _PositiveInt
    lexical_matches: tuple[LexicalField, ...] = ()


class ChunkRetrievalFacts(_QueryModel):
    """Formal V2 result and projection inputs, read by the external fact loader."""

    video_chunk_id: _NonBlank
    retrieval_identity: VideoChunkRetrievalIdentity
    sequence_no: int = Field(ge=0, strict=True)
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: _PositiveInt
    result: VideoParseResult
    description_language: _NonBlank | None = None
    enrichments: tuple[SourceEnrichment, ...] = ()

    @model_validator(mode="after")
    def validate_bounds(self) -> ChunkRetrievalFacts:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("fact bounds must be increasing")
        return self


class VerifiedSourceEvidence(_QueryModel):
    """Source ownership and granularity survive fusion and future precision scoring."""

    video_chunk_id: _NonBlank
    retrieval_identity: VideoChunkRetrievalIdentity
    context: Literal["self", "before", "after"]
    granularity: Literal["event", "chunk"]
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: _PositiveInt
    source: LexicalField | None = None
    constraint_key: str | None = None


class VerifiedRetrievalCandidate(_QueryModel):
    candidate: RetrievalCandidate
    evidence: tuple[VerifiedSourceEvidence, ...]


class RejectedRetrievalCandidate(_QueryModel):
    video_chunk_id: _NonBlank
    message: _NonBlank


class RetrievalEvidenceResult(_QueryModel):
    accepted: tuple[VerifiedRetrievalCandidate, ...]
    rejected: tuple[RejectedRetrievalCandidate, ...]

    @property
    def rejected_chunk_ids(self) -> tuple[str, ...]:
        return tuple(item.video_chunk_id for item in self.rejected)


def validate_retrieval_identity(plan: RetrievalQueryPlan, identity: VideoChunkRetrievalIdentity) -> None:
    task = next((item for item in plan.scope.tasks if item.video_id == identity.video_id), None)
    contract = plan.index_contract
    sparse_id = contract.sparse_encoder.encoder_id if contract.sparse_encoder else None
    if (task is None or identity.workspace_id != plan.scope.workspace_id
            or identity.series_id != task.series_id or identity.parse_task_id != task.parse_task_id
            or identity.retrieval_contract_id != contract.retrieval_contract_id
            or identity.index_generation != contract.index_generation
            or identity.semantic_encoder_id != contract.semantic_encoder_id
            or identity.vision_encoder_id != contract.vision_encoder_id
            or identity.sparse_encoder_id != sparse_id):
        raise RetrievalConsistencyError("identity falls outside trusted scope or active index contract")
