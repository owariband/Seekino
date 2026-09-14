from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.parse.analysis_windows import probe_video_duration_ms

from ..identity import canonical_uuid, derive_video_id
from ..repository.upload import (
    CompletedUploadState,
    UploadCompletionClaim,
    UploadConflictError,
    UploadRepository,
    UploadSegmentState,
    UploadSessionState,
)
from ..storage.artifact import (
    ArtifactStore,
    UploadSegmentArtifact,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
    }
)


@dataclass(frozen=True, slots=True)
class UploadServiceConfig:
    preferred_segment_size_bytes: int = 8 * 1024 * 1024
    maximum_upload_size_bytes: int = 10 * 1024 * 1024 * 1024
    session_ttl: timedelta = timedelta(hours=24)
    completion_stale_after: timedelta = timedelta(minutes=15)
    pipeline_version: str = "m1"
    max_job_attempts: int = 3

    def __post_init__(self) -> None:
        if self.preferred_segment_size_bytes <= 0:
            raise ValueError("preferred segment size must be greater than zero")
        if self.maximum_upload_size_bytes < self.preferred_segment_size_bytes:
            raise ValueError("maximum upload size cannot be smaller than a segment")
        if self.session_ttl <= timedelta(0):
            raise ValueError("upload session TTL must be positive")
        if self.completion_stale_after <= timedelta(0):
            raise ValueError("completion stale timeout must be positive")
        if not self.pipeline_version.strip():
            raise ValueError("pipeline version must not be empty")
        if self.max_job_attempts <= 0:
            raise ValueError("max job attempts must be greater than zero")


class UploadChecksumMismatchError(ValueError):
    pass


class UploadService:
    def __init__(
        self,
        repository: UploadRepository,
        artifact_store: ArtifactStore,
        *,
        config: UploadServiceConfig | None = None,
        probe_duration: Callable[[str | Path], int] = probe_video_duration_ms,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._config = config or UploadServiceConfig()
        self._probe_duration = probe_duration
        self._clock = clock or _utc_now

    def create_session(
        self,
        *,
        workspace_id: str,
        series_id: str,
        episode_order: int,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        idempotency_key: str,
        upload_sequence: int | None = None,
    ) -> UploadSessionState:
        normalized_workspace = _required_text(workspace_id, "workspace_id")
        normalized_series_id = canonical_uuid(series_id, "series_id")
        normalized_filename = _filename(original_filename)
        normalized_media_type = _required_text(media_type, "media_type").casefold()
        normalized_idempotency_key = _required_text(
            idempotency_key,
            "idempotency_key",
        )
        if normalized_media_type not in _SUPPORTED_MEDIA_TYPES:
            raise ValueError(
                "media_type must be video/mp4, video/quicktime, or video/x-msvideo"
            )
        if isinstance(total_size_bytes, bool) or not isinstance(total_size_bytes, int):
            raise ValueError("total_size_bytes must be an integer")
        if not 0 < total_size_bytes <= self._config.maximum_upload_size_bytes:
            raise ValueError("total_size_bytes is outside the configured upload limit")
        if isinstance(episode_order, bool) or not isinstance(episode_order, int):
            raise ValueError("episode_order must be an integer")
        if episode_order <= 0:
            raise ValueError("episode_order must be greater than zero")
        resolved_upload_sequence = (
            episode_order if upload_sequence is None else upload_sequence
        )
        if (
            isinstance(resolved_upload_sequence, bool)
            or not isinstance(resolved_upload_sequence, int)
            or resolved_upload_sequence <= 0
        ):
            raise ValueError("upload_sequence must be a positive integer")

        segment_size = self._config.preferred_segment_size_bytes
        total_segments = math.ceil(total_size_bytes / segment_size)
        now = self._clock()
        return self._repository.create_session(
            workspace_id=normalized_workspace,
            series_id=normalized_series_id,
            episode_order=episode_order,
            upload_sequence=resolved_upload_sequence,
            original_filename=normalized_filename,
            media_type=normalized_media_type,
            total_size_bytes=total_size_bytes,
            preferred_segment_size_bytes=segment_size,
            total_segments=total_segments,
            idempotency_key=normalized_idempotency_key,
            expires_at=now + self._config.session_ttl,
            now=now,
        )

    def get_session(self, upload_session_id: str) -> UploadSessionState:
        return self._repository.get_session(
            canonical_uuid(upload_session_id, "upload_session_id"),
            now=self._clock(),
        )

    def list_segments(self, upload_session_id: str) -> list[UploadSegmentState]:
        return self._repository.list_segments(
            canonical_uuid(upload_session_id, "upload_session_id")
        )

    def upload_segment(
        self,
        upload_session_id: str,
        segment_number: int,
        data: bytes,
        *,
        checksum_sha256: str,
        start_byte: int,
        end_byte_exclusive: int,
    ) -> UploadSegmentState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        checksum = checksum_sha256.casefold()
        if not _SHA256_PATTERN.fullmatch(checksum):
            raise ValueError("checksum_sha256 must be 64 lowercase hex characters")
        stored = self._artifact_store.put_upload_segment(
            session_id,
            segment_number,
            data,
        )
        if stored.sha256 != checksum:
            raise UploadChecksumMismatchError(
                f"segment {segment_number} SHA-256 does not match its header"
            )
        return self._repository.register_segment(
            session_id,
            segment_number=segment_number,
            start_byte=start_byte,
            end_byte_exclusive=end_byte_exclusive,
            actual_size_bytes=stored.size_bytes,
            checksum_sha256=stored.sha256,
            artifact_uri=stored.uri,
            now=self._clock(),
        )

    def complete(self, upload_session_id: str) -> CompletedUploadState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        claim = self._repository.claim_completion(
            session_id,
            now=self._clock(),
            stale_after=self._config.completion_stale_after,
        )
        if claim.already_completed:
            if claim.session.video_id is None or claim.existing_job_id is None:
                raise UploadConflictError(
                    "completed upload is missing its Video or ParseJob"
                )
            return CompletedUploadState(
                session=claim.session,
                video_id=claim.session.video_id,
                job_id=claim.existing_job_id,
            )
        assert claim.completion_token is not None

        try:
            assembled = self._artifact_store.assemble_upload(
                session_id,
                [_artifact_segment(item) for item in claim.segments],
                expected_total_size=claim.session.total_size_bytes,
            )
            video_id = derive_video_id(claim.session.series_id, assembled.sha256)
            published = self._artifact_store.publish_original(
                assembled,
                series_id=claim.session.series_id,
                video_id=video_id,
                original_filename=claim.session.original_filename,
            )
            duration_ms = self._probe_duration(
                self._artifact_store.resolve(published.uri)
            )
            completed = self._repository.complete_upload(
                session_id,
                claim.completion_token,
                video_id=video_id,
                original_source_uri=published.uri,
                content_hash=published.sha256,
                duration_ms=duration_ms,
                pipeline_version=self._config.pipeline_version,
                max_job_attempts=self._config.max_job_attempts,
                now=self._clock(),
            )
        except Exception as exc:
            self._repository.release_completion(
                session_id,
                claim.completion_token,
                error=_error_summary(exc),
            )
            raise

        self._artifact_store.delete_upload_staging(session_id)
        return completed


def _artifact_segment(segment: UploadSegmentState) -> UploadSegmentArtifact:
    return UploadSegmentArtifact(
        segment_number=segment.segment_number,
        uri=segment.artifact_uri,
        size_bytes=segment.actual_size_bytes,
        sha256=segment.checksum_sha256,
    )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _filename(value: str) -> str:
    normalized = _required_text(value, "original_filename")
    if Path(normalized).name != normalized or "\x00" in normalized:
        raise ValueError("original_filename must not contain a path")
    return normalized


def _error_summary(exc: Exception) -> str:
    message = str(exc).strip()
    return (type(exc).__name__ if not message else f"{type(exc).__name__}: {message}")[
        :8192
    ]


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
