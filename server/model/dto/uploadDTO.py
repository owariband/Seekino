from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from ...schema import ContractModel
from ..upload import UploadSessionStatus


@dataclass(frozen=True, slots=True)
class UploadSegmentState:
    segment_number: int
    start_byte: int
    end_byte_exclusive: int
    actual_size_bytes: int
    checksum_sha256: str
    artifact_uri: str


@dataclass(frozen=True, slots=True)
class UploadSessionState:
    upload_session_id: str
    workspace_id: str
    series_id: str
    episode_order: int
    upload_sequence: int
    original_filename: str
    media_type: str
    total_size_bytes: int
    preferred_segment_size_bytes: int
    total_segments: int
    status: str
    uploaded_segment_count: int
    uploaded_size_bytes: int
    expires_at: datetime
    video_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    content_sha256: str | None = None
    completion_stage: str | None = None
    job_id: str | None = None
    version: int = 1
    missing_segment_numbers: tuple[int, ...] = ()
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class UploadCompletionClaim:
    session: UploadSessionState
    segments: tuple[UploadSegmentState, ...]
    completion_token: str | None
    existing_job_id: str | None

    @property
    def already_completed(self) -> bool:
        return self.session.status == UploadSessionStatus.COMPLETED.value


@dataclass(frozen=True, slots=True)
class CompletedUploadState:
    session: UploadSessionState
    video_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class UploadSessionDetail:
    session: UploadSessionState
    missing_segment_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UploadSessionDeletion:
    session: UploadSessionState
    video_id: str | None
    job_id: str | None
    job_ids: tuple[str, ...]
    video_chunk_ids: tuple[str, ...]


class UploadSessionCreateRequest(ContractModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    episode_order: int | None = Field(
        default=None, gt=0, strict=True, deprecated=True,
        description="Compatibility only; ignored. The server assigns append order.",
    )
    upload_sequence: int | None = Field(
        default=None, gt=0, strict=True, deprecated=True,
        description="Compatibility only; ignored. The server assigns append order.",
    )
    original_filename: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    total_size_bytes: int = Field(gt=0, le=9_007_199_254_740_991, strict=True)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
