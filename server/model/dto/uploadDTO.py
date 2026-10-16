from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import AliasChoices, AfterValidator, Field

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config

from ...config.parse_profile import ParseConfig, validate_video_parse_schedule
from ...schema import ContractModel
from ..upload import UploadSessionStatus


_ValidatedVideoParseConfig = Annotated[
    VideoParseV2Config, AfterValidator(validate_video_parse_schedule),
]


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
    tenant_id: str
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
    initial_video_parse_task_id: str | None = None
    version: int = 1
    missing_segment_numbers: tuple[int, ...] = ()
    last_error_code: str | None = None
    parse_config: dict | None = None
    detail: dict | None = None
    created_by_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class UploadCompletionClaim:
    session: UploadSessionState
    segments: tuple[UploadSegmentState, ...]
    completion_token: str | None
    existing_job_id: str | None
    existing_video_parse_task_id: str | None

    @property
    def already_completed(self) -> bool:
        return self.session.status == UploadSessionStatus.COMPLETED.value


@dataclass(frozen=True, slots=True)
class CompletedUploadState:
    session: UploadSessionState
    video_id: str
    job_id: str | None
    video_parse_task_id: str


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
    video_parse_document_keys: tuple[tuple[str, str], ...]


class UploadSessionCreateRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    episode_order: int = Field(
        gt=0,
        le=2_147_483_647,
        strict=True,
        description="Confirmed one-based episode order within the Series.",
    )
    original_filename: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    total_size_bytes: int = Field(gt=0, le=9_007_199_254_740_991, strict=True)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parse_config: ParseConfig | _ValidatedVideoParseConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("parseConfig", "parse_config", "videoParseConfig", "video_parse_config"),
    )


class UploadReparseRequest(ContractModel):
    parse_config: ParseConfig | _ValidatedVideoParseConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("parseConfig", "parse_config", "videoParseConfig", "video_parse_config"),
    )
