from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from engine.parse.analysis_windows import probe_video_duration_ms

from ..config.upload import UploadServiceConfig
from ..identity import canonical_uuid, derive_video_id
from ..model.upload import UploadSessionStatus
from ..repository.upload import (
    CompletedUploadState,
    UploadCompletionClaim,
    UploadConflictError,
    UploadRepository,
    UploadSegmentState,
    UploadSessionNotFoundError,
    UploadSessionState,
    UploadStateError,
)
from ..storage.artifact import (
    ArtifactStore,
    UploadSegmentArtifact,
)
from ..utils.errors import exception_summary
from ..utils.time import utc_now_naive
from ..utils.validation import require_nonempty_text

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
    }
)


class UploadChecksumMismatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UploadSessionDetail:
    session: UploadSessionState
    missing_segment_numbers: tuple[int, ...]


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
        self._clock = clock or utc_now_naive

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
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        normalized_series_id = canonical_uuid(series_id, "series_id")
        normalized_filename = _filename(original_filename)
        normalized_media_type = require_nonempty_text(media_type, "media_type").casefold()
        normalized_idempotency_key = require_nonempty_text(
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

    def get_session(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
    ) -> UploadSessionState:
        normalized_workspace = require_nonempty_text(workspace_id, "workspace_id")
        state = self._repository.get_session(
            canonical_uuid(upload_session_id, "upload_session_id"),
            now=self._clock(),
        )
        if state.workspace_id != normalized_workspace:
            raise UploadSessionNotFoundError(
                "upload session does not exist in this workspace"
            )
        return state

    def get_detail(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
    ) -> UploadSessionDetail:
        state = self.get_session(upload_session_id, workspace_id=workspace_id)
        segments = self._repository.list_segments(state.upload_session_id)
        present = {item.segment_number for item in segments}
        return UploadSessionDetail(
            session=state,
            missing_segment_numbers=tuple(
                number for number in range(state.total_segments) if number not in present
            ),
        )

    def upload_segment(
        self,
        upload_session_id: str,
        segment_number: int,
        data: bytes,
        *,
        workspace_id: str,
        checksum_sha256: str,
        start_byte: int,
        end_byte_exclusive: int,
        total_size_bytes: int,
    ) -> UploadSegmentState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        checksum = checksum_sha256.casefold()
        if not _SHA256_PATTERN.fullmatch(checksum):
            raise ValueError("checksum_sha256 must be 64 lowercase hex characters")
        self.require_uploadable_segment(
            session_id,
            segment_number,
            workspace_id=workspace_id,
            start_byte=start_byte,
            end_byte_exclusive=end_byte_exclusive,
            total_size_bytes=total_size_bytes,
        )
        if len(data) != end_byte_exclusive - start_byte:
            raise UploadConflictError(
                "upload segment size does not match its byte range"
            )
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

    def require_uploadable_segment(
        self,
        upload_session_id: str,
        segment_number: int,
        *,
        workspace_id: str,
        start_byte: int,
        end_byte_exclusive: int,
        total_size_bytes: int,
    ) -> None:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        current = self.get_session(session_id, workspace_id=workspace_id)
        if current.status != UploadSessionStatus.UPLOADING.value:
            raise UploadStateError(
                f"upload session is {current.status.lower()} and cannot accept segments"
            )
        if total_size_bytes != current.total_size_bytes:
            raise UploadConflictError(
                "upload total size does not match the upload session"
            )
        expected_start = segment_number * current.preferred_segment_size_bytes
        expected_end = min(
            expected_start + current.preferred_segment_size_bytes,
            current.total_size_bytes,
        )
        if segment_number < 0 or (start_byte, end_byte_exclusive) != (
            expected_start,
            expected_end,
        ):
            raise UploadConflictError(
                "byte range does not match this upload segment"
            )

    def complete(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
    ) -> CompletedUploadState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        self.get_session(session_id, workspace_id=workspace_id)
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
                error=exception_summary(exc),
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


def _filename(value: str) -> str:
    normalized = require_nonempty_text(value, "original_filename")
    if Path(normalized).name != normalized or "\x00" in normalized:
        raise ValueError("original_filename must not contain a path")
    return normalized
