"""Verify an MCP source reference at evidence read and media download time."""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.rag.contracts import VideoChunkEvidence, VideoChunkRetrievalIdentity
from engine.rag.indexing.events import compute_event_id

from .video_parse_errors import VideoParseConflictError
from .video_parse_persistence import VideoParsePersistenceService


class StaleEvidenceError(ValueError):
    pass


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    identity: VideoChunkRetrievalIdentity
    video_chunk_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeline_index: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, evidence_ref: str) -> EvidenceRef:
        try:
            if not isinstance(evidence_ref, str) or not 1 <= len(evidence_ref) <= 4096:
                raise ValueError("invalid reference size")
            raw = base64.b64decode(evidence_ref, altchars=b"-_", validate=True)
            return cls.model_validate_json(raw)
        except (ValueError, binascii.Error, ValidationError) as error:
            raise ValueError("INVALID_EVIDENCE_REF: Use an evidence_ref returned by retrieve or read_evidence.") from error

    def resolve(self, persistence: VideoParsePersistenceService, *, tenant_id: str) -> VideoChunkEvidence:
        evidence = published_chunk(persistence, self.video_chunk_id, self.identity, tenant_id=tenant_id)
        self.validate_event(evidence)
        if self.offset >= len(evidence.all_time_events):
            raise ValueError("INVALID_EVIDENCE_REF: Context offset is outside this Chunk.")
        return evidence

    def validate_event(self, evidence: VideoChunkEvidence) -> None:
        identity = self.identity
        if (self.timeline_index >= len(evidence.all_time_events)
                or self.event_id != compute_event_id(identity.parse_task_id, self.video_chunk_id,
                                                    identity.content_version, self.timeline_index)):
            raise ValueError("INVALID_EVIDENCE_REF: Event identity does not match its source Timeline.")


def published_chunk(persistence: VideoParsePersistenceService, chunk_id: str, identity: VideoChunkRetrievalIdentity,
                    *, tenant_id: str) -> VideoChunkEvidence:
    if identity.tenant_id != tenant_id:
        raise ValueError("EVIDENCE_SCOPE_MISMATCH: Evidence is outside this local library.")
    try:
        hydrated = persistence.hydrate_retrieval_candidates([(chunk_id, identity)])
    except (VideoParseConflictError, ValueError) as error:
        raise StaleEvidenceError("EVIDENCE_STALE: Source changed or is no longer available; retrieve again.") from error
    if len(hydrated) != 1:
        raise StaleEvidenceError("EVIDENCE_STALE: Source is no longer available; retrieve again.")
    evidence = hydrated[0].evidence
    if evidence_identity(evidence, tenant_id) != identity or evidence.video_chunk_id != chunk_id:
        raise StaleEvidenceError("EVIDENCE_STALE: Source identity changed; retrieve again.")
    return evidence


def evidence_identity(evidence: VideoChunkEvidence, tenant_id: str) -> VideoChunkRetrievalIdentity:
    if evidence.tenant_id != tenant_id:
        raise ValueError("EVIDENCE_SCOPE_MISMATCH: Evidence is outside this local library.")
    provenance = evidence.parse_provenance or {}
    try:
        return VideoChunkRetrievalIdentity(
            tenant_id=tenant_id, series_id=evidence.series_id, video_id=evidence.video_id,
            parse_task_id=provenance["parseTaskId"], content_version=provenance["contentVersion"],
            index_generation=provenance["indexGeneration"],
        )
    except (KeyError, ValidationError) as error:
        raise StaleEvidenceError("EVIDENCE_STALE: Source lacks a formal version identity; retrieve again.") from error
