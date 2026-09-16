from __future__ import annotations

import math
import logging
import re
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

from engine.parse.analysis_windows import probe_video_duration_ms

from ..config.upload import UploadServiceConfig
from ..identity import canonical_uuid, derive_video_id
from ..model.upload import UploadSessionStatus, UploadCompletionStage
from ..model.dto.uploadDTO import (
    CompletedUploadState, UploadCompletionClaim, UploadSegmentState, UploadSessionDetail,
    UploadSessionState,
)
from ..repository.upload import (
    UploadConflictError,
    UploadRepository,
    UploadSessionNotFoundError,
    UploadStateError,
)
from ..storage.artifact import (
    ArtifactStore,
    UploadSegmentArtifact,
)
from ..storage.qdrant import QdrantVideoChunkStore
from ..utils.errors import exception_summary
from ..utils.time import utc_now_naive
from ..utils.validation import require_nonempty_text
from .qdrant_mutation_lock import QdrantMutationLock

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_logger = logging.getLogger(__name__)
_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
    }
)


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
        video_chunk_store: QdrantVideoChunkStore | None = None,
        qdrant_mutation_lock: QdrantMutationLock | None = None,
    ) -> None:
        if video_chunk_store is not None and qdrant_mutation_lock is None:
            raise ValueError(
                "qdrant_mutation_lock is required when video_chunk_store is configured"
            )
        self._repository = repository
        self._artifact_store = artifact_store
        self._config = config or UploadServiceConfig()
        self._probe_duration = probe_duration
        self._clock = clock or utc_now_naive
        self._video_chunk_store = video_chunk_store
        self._qdrant_mutation_lock = qdrant_mutation_lock

    def create_session(
        self,
        *,
        workspace_id: str,
        series_id: str,
        episode_order: int | None = None,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        idempotency_key: str,
        upload_sequence: int | None = None,
        content_sha256: str | None = None,
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
        if content_sha256 is not None and not _SHA256_PATTERN.fullmatch(content_sha256):
            raise ValueError("content_sha256 must be 64 lowercase hex characters")

        segment_size = self._config.preferred_segment_size_bytes
        total_segments = math.ceil(total_size_bytes / segment_size)
        now = self._clock()
        state = self._repository.create_session(
            workspace_id=normalized_workspace,
            series_id=normalized_series_id,
            original_filename=normalized_filename,
            media_type=normalized_media_type,
            total_size_bytes=total_size_bytes,
            preferred_segment_size_bytes=segment_size,
            total_segments=total_segments,
            idempotency_key=normalized_idempotency_key,
            expires_at=now + self._config.session_ttl,
            now=now,
            content_sha256=content_sha256,
        )
        _logger.info(
            "upload_session_created upload_session_id=%s series_id=%s episode_order=%s size_bytes=%s segments=%s status=%s",
            state.upload_session_id, state.series_id, state.episode_order,
            state.total_size_bytes, state.total_segments, state.status,
        )
        return state

    def list_sessions(self, *, workspace_id: str, series_id: str | None = None, status: str | None = None) -> list[UploadSessionState]:
        return self._repository.list_for_workspace(
            require_nonempty_text(workspace_id, "workspace_id"),
            series_id=canonical_uuid(series_id, "series_id") if series_id is not None else None,
            status=status,
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
        return UploadSessionDetail(
            session=state,
            missing_segment_numbers=state.missing_segment_numbers,
        )

    def retry_parse(
        self,
        upload_session_id: str,
        *,
        workspace_id: str,
        expected_job_version: int,
    ) -> str:
        if (
            isinstance(expected_job_version, bool)
            or not isinstance(expected_job_version, int)
            or expected_job_version < 1
        ):
            raise ValueError("expected_job_version must be a positive integer")
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        job_id = self._repository.retry_parse(
            session_id,
            workspace_id=require_nonempty_text(workspace_id, "workspace_id"),
            expected_job_version=expected_job_version,
            now=self._clock(),
        )
        _logger.info(
            "upload_parse_retry_requested upload_session_id=%s job_id=%s expected_job_version=%s",
            session_id,
            job_id,
            expected_job_version,
        )
        return job_id

    def delete_session(self, upload_session_id: str, *, workspace_id: str) -> UploadSessionState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        mutation_scope = (
            self._qdrant_mutation_lock.hold()
            if self._qdrant_mutation_lock is not None
            else nullcontext()
        )
        with mutation_scope:
            deletion = self._repository.begin_delete_session(
                session_id,
                workspace_id=require_nonempty_text(workspace_id, "workspace_id"),
                now=self._clock(),
            )
            try:
                self._artifact_store.delete_upload_staging(session_id)
                if deletion.video_chunk_ids and self._video_chunk_store is None:
                    raise RuntimeError("video chunk cleanup store is unavailable")
                if self._video_chunk_store is not None:
                    for video_chunk_id in deletion.video_chunk_ids:
                        self._video_chunk_store.delete(video_chunk_id)
                for job_id in deletion.job_ids:
                    self._artifact_store.delete_job_artifacts(job_id)
                if deletion.video_id is not None:
                    self._artifact_store.delete_video_artifacts(
                        deletion.session.series_id,
                        deletion.video_id,
                    )
            except Exception:
                _logger.exception(
                    "upload_cleanup_failed upload_session_id=%s video_id=%s job_id=%s",
                    session_id, deletion.video_id, deletion.job_id,
                )
                raise
            state = self._repository.finish_delete_session(
                deletion,
                now=self._clock(),
            )
            _logger.info(
                "upload_deleted upload_session_id=%s series_id=%s",
                session_id,
                state.series_id,
            )
            return state

    def reconcile_deletions(self, *, limit: int = 100) -> tuple[int, int]:
        try:
            pending = self._repository.list_pending_deletions(limit=limit)
        except Exception:
            _logger.exception("upload_deletion_reconcile_list_failed")
            return 0, 1
        completed = 0
        failed = 0
        for upload_session_id, workspace_id in pending:
            try:
                self.delete_session(
                    upload_session_id,
                    workspace_id=workspace_id,
                )
            except Exception:
                failed += 1
                _logger.exception(
                    "upload_deletion_reconcile_failed upload_session_id=%s",
                    upload_session_id,
                )
            else:
                completed += 1
        return completed, failed

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
        try:
            stored = self._artifact_store.put_upload_segment(
                session_id,
                segment_number,
                data,
            )
            if stored.sha256 != checksum:
                raise UploadChecksumMismatchError(
                    f"segment {segment_number} SHA-256 does not match its header"
                )
            segment = self._repository.register_segment(
                session_id,
                segment_number=segment_number,
                start_byte=start_byte,
                end_byte_exclusive=end_byte_exclusive,
                actual_size_bytes=stored.size_bytes,
                checksum_sha256=stored.sha256,
                artifact_uri=stored.uri,
                now=self._clock(),
            )
        except (UploadStateError, UploadChecksumMismatchError, FileNotFoundError) as exc:
            if self._repository.get_session(session_id, now=self._clock()).status == UploadSessionStatus.EXPIRED.value:
                self._artifact_store.delete_upload_staging(session_id)
            _logger.warning(
                "upload_segment_failed upload_session_id=%s segment_number=%s error=%s",
                session_id, segment_number, exception_summary(exc),
            )
            raise
        _logger.info(
            "upload_segment_confirmed upload_session_id=%s segment_number=%s size_bytes=%s",
            session_id, segment_number, segment.actual_size_bytes,
        )
        return segment

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
        started_at = time.perf_counter()
        _logger.info("upload_completion_started upload_session_id=%s", session_id)

        published_video_id: str | None = None
        try:
            self._repository.set_completion_stage(session_id, claim.completion_token, UploadCompletionStage.ASSEMBLING)
            _logger.info("upload_stage upload_session_id=%s stage=ASSEMBLING", session_id)
            assembled = self._artifact_store.assemble_upload(
                session_id,
                [_artifact_segment(item) for item in claim.segments],
                expected_total_size=claim.session.total_size_bytes,
            )
            self._repository.set_completion_stage(session_id, claim.completion_token, UploadCompletionStage.VERIFYING)
            _logger.info("upload_stage upload_session_id=%s stage=VERIFYING", session_id)
            if claim.session.content_sha256 is not None and assembled.sha256 != claim.session.content_sha256:
                raise UploadChecksumMismatchError("source file SHA-256 does not match the upload session")
            video_id = derive_video_id(claim.session.series_id, assembled.sha256)
            published = self._artifact_store.publish_original(
                assembled,
                series_id=claim.session.series_id,
                video_id=video_id,
                original_filename=claim.session.original_filename,
            )
            published_video_id = video_id
            self._repository.set_completion_stage(session_id, claim.completion_token, UploadCompletionStage.PROBING)
            _logger.info("upload_stage upload_session_id=%s stage=PROBING", session_id)
            duration_ms = self._probe_duration(
                self._artifact_store.resolve(published.uri)
            )
            self._repository.set_completion_stage(session_id, claim.completion_token, UploadCompletionStage.CREATING_JOB)
            _logger.info("upload_stage upload_session_id=%s stage=CREATING_JOB", session_id)
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
            _logger.warning(
                "upload_completion_failed upload_session_id=%s duration_ms=%.1f error=%s",
                session_id, (time.perf_counter() - started_at) * 1000, exception_summary(exc),
            )
            if published_video_id is not None:
                try:
                    if not self._repository.video_exists(published_video_id):
                        self._artifact_store.delete_video_artifacts(
                            claim.session.series_id,
                            published_video_id,
                        )
                except Exception:
                    _logger.exception(
                        "upload_failed_publication_cleanup_failed upload_session_id=%s video_id=%s",
                        session_id, published_video_id,
                    )
            self._repository.release_completion(
                session_id,
                claim.completion_token,
                error=exception_summary(exc),
                error_code="CHECKSUM_MISMATCH" if isinstance(exc, UploadChecksumMismatchError) else "PROCESSING_FAILED",
            )
            raise

        self._artifact_store.delete_upload_staging(session_id)
        _logger.info(
            "upload_completed upload_session_id=%s video_id=%s job_id=%s duration_ms=%.1f",
            session_id, completed.video_id, completed.job_id,
            (time.perf_counter() - started_at) * 1000,
        )
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
