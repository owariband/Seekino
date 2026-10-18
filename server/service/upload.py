from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config
from engine.common.media_probe import probe_video_duration_seconds
from engine.rag import EncoderContract
from gateway.model_sources import ModelReference

from ..config.parse_profile import ParseConfig, index_contract_fingerprint, validate_video_parse_schedule
from ..config.upload import UploadServiceConfig
from ..model.identifiers import derive_video_id
from ..model.upload import UploadSessionStatus, UploadCompletionStage
from ..model.dto.uploadDTO import (
    CompletedUploadState, UploadSegmentState, UploadSessionDetail,
    UploadSessionState,
)
from ..model.dto.jobDTO import VideoParseTaskState
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
from ..storage.elasticsearch import ElasticsearchVideoChunkStore
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from ..utils.errors import exception_summary
from ..utils.time import now_utc, require_utc_datetime, unix_time_ms_to_datetime
from ..utils.validation import canonical_uuid, require_nonempty_text

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
        probe_duration: Callable[[str | Path], int] = probe_video_duration_seconds,
        clock: Callable[[], datetime | int] | None = None,
        video_parse_index_store: ElasticsearchVideoChunkStore | None = None,
        parse_defaults: ParseConfig,
        video_parse_query_encoder: EncoderContract | None = None,
        video_parse_query_encoder_source: str | None = None,
        video_event_index_store: ElasticsearchVideoEventStore | None = None,
    ) -> None:
        if (video_parse_query_encoder is None) != (
            video_parse_query_encoder_source is None
        ):
            raise ValueError(
                "video_parse_query_encoder and its source must be provided together"
            )
        if video_parse_index_store is not None and video_parse_query_encoder is None:
            raise ValueError(
                "video_parse_query_encoder is required when the V2 index store is configured"
            )
        if video_parse_query_encoder_source is not None:
            video_parse_query_encoder_source = require_nonempty_text(
                video_parse_query_encoder_source,
                "video_parse_query_encoder_source",
            )
        self._repository = repository
        self._artifact_store = artifact_store
        self._config = config or UploadServiceConfig()
        self._probe_duration = probe_duration
        self._clock = clock or now_utc
        self._video_parse_index_store = video_parse_index_store
        self._video_event_index_store = video_event_index_store
        self._parse_defaults = parse_defaults
        self._video_parse_query_encoder = video_parse_query_encoder
        self._video_parse_query_encoder_source = video_parse_query_encoder_source

    @property
    def parse_defaults(self) -> ParseConfig:
        return self._parse_defaults

    def _now(self) -> datetime:
        value = self._clock()
        if isinstance(value, datetime):
            return require_utc_datetime(value, "clock")
        # Temporary compatibility for injected test clocks and callers that
        # still expose the old millisecond clock at the service boundary.
        return unix_time_ms_to_datetime(value)

    def create_session(
        self,
        *,
        tenant_id: str,
        series_id: str,
        episode_order: int,
        original_filename: str,
        media_type: str,
        total_size_bytes: int,
        idempotency_key: str,
        content_sha256: str | None = None,
        parse_config: ParseConfig | VideoParseV2Config | None = None,
        created_by_user_id: str | None = None,
    ) -> UploadSessionState:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        if created_by_user_id is not None:
            created_by_user_id = require_nonempty_text(created_by_user_id, "created_by_user_id", maximum=64)
        normalized_series_id = canonical_uuid(series_id, "series_id")
        normalized_filename = _filename(original_filename)
        normalized_media_type = require_nonempty_text(media_type, "media_type").casefold()
        normalized_idempotency_key = require_nonempty_text(
            idempotency_key,
            "idempotency_key",
        )
        if (
            isinstance(episode_order, bool)
            or not isinstance(episode_order, int)
            or not 0 < episode_order <= 2_147_483_647
        ):
            raise ValueError("episode_order must be a positive 32-bit integer")
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

        def resolve_config() -> dict:
            return self._resolve_parse_config(parse_config or self._parse_defaults).model_dump(mode="json")

        # Fingerprint the submitted form; normalized execution settings may change
        # independently, but a replay must return the original accepted snapshot.
        requested_parse_config = parse_config.model_dump(mode="json") if parse_config else None
        if requested_parse_config is not None:
            embedding = (parse_config.embedding if isinstance(parse_config, ParseConfig)
                         else parse_config.models.embedding)
            if embedding.encoder_model is None:
                target = (requested_parse_config["embedding"] if isinstance(parse_config, ParseConfig)
                          else requested_parse_config["models"]["embedding"])
                target.pop("encoder_model", None)

        segment_size = self._config.preferred_segment_size_bytes
        total_segments = math.ceil(total_size_bytes / segment_size)
        now = self._now()
        state = self._repository.create_session(
            tenant_id=normalized_workspace,
            created_by_user_id=created_by_user_id,
            series_id=normalized_series_id,
            episode_order=episode_order,
            original_filename=normalized_filename,
            media_type=normalized_media_type,
            total_size_bytes=total_size_bytes,
            preferred_segment_size_bytes=segment_size,
            total_segments=total_segments,
            idempotency_key=normalized_idempotency_key,
            expires_at=now + self._config.session_ttl,
            now=now,
            content_sha256=content_sha256,
            requested_parse_config=requested_parse_config,
            config_resolver=resolve_config,
        )
        _logger.info(
            "upload_session_created upload_session_id=%s series_id=%s episode_order=%s size_bytes=%s segments=%s status=%s",
            state.upload_session_id, state.series_id, state.episode_order,
            state.total_size_bytes, state.total_segments, state.status,
        )
        return state

    def list_sessions(self, *, tenant_id: str, series_id: str | None = None, status: str | None = None) -> list[UploadSessionState]:
        return self._repository.list_for_workspace(
            require_nonempty_text(tenant_id, "tenant_id"),
            series_id=canonical_uuid(series_id, "series_id") if series_id is not None else None,
            status=status,
        )

    def get_session(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
    ) -> UploadSessionState:
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        state = self._repository.get_session(
            canonical_uuid(upload_session_id, "upload_session_id"),
            now=self._now(),
        )
        if state.tenant_id != normalized_workspace:
            raise UploadSessionNotFoundError(
                "upload session does not exist in this workspace"
            )
        return state

    def get_detail(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
    ) -> UploadSessionDetail:
        state = self.get_session(upload_session_id, tenant_id=tenant_id)
        return UploadSessionDetail(
            session=state,
            missing_segment_numbers=state.missing_segment_numbers,
        )

    def retry_parse(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
        expected_job_version: int,
        expected_lease_version: int,
        actor_user_id: str | None = None,
    ) -> VideoParseTaskState:
        if (
            isinstance(expected_job_version, bool)
            or not isinstance(expected_job_version, int)
            or expected_job_version < 1
        ):
            raise ValueError("expected_job_version must be a positive integer")
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        if actor_user_id is not None:
            actor_user_id = require_nonempty_text(actor_user_id, "actor_user_id", maximum=64)

        task = self._repository.retry_parse(
            session_id,
            tenant_id=require_nonempty_text(tenant_id, "tenant_id"),
            expected_job_version=expected_job_version,
            expected_lease_version=expected_lease_version,
            actor_user_id=actor_user_id,
            now=self._now(),
        )
        _logger.info(
            "upload_parse_retry_requested upload_session_id=%s video_parse_task_id=%s expected_parse_version=%s",
            session_id,
            task.video_parse_task_id,
            expected_job_version,
        )
        return task

    def reparse(
        self,
        upload_session_id: str,
        *,
        tenant_id: str,
        expected_job_version: int,
        parse_config: ParseConfig | VideoParseV2Config | None = None,
        actor_user_id: str | None = None,
    ) -> VideoParseTaskState:
        if (
            isinstance(expected_job_version, bool)
            or not isinstance(expected_job_version, int)
            or expected_job_version < 1
        ):
            raise ValueError("expected_job_version must be a positive integer")
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        if actor_user_id is not None:
            actor_user_id = require_nonempty_text(actor_user_id, "actor_user_id", maximum=64)
        normalized_workspace = require_nonempty_text(
            tenant_id, "tenant_id"
        )

        def resolve_snapshot(snapshot: dict) -> dict:
            if isinstance(parse_config, ParseConfig):
                return self._resolve_parse_config(parse_config).model_dump(mode="json")
            selected = VideoParseV2Config.model_validate(snapshot)
            if parse_config is None:
                # A new parse version adopts the active index generation, while
                # retry keeps its existing task snapshot and never enters here.
                selected = selected.model_copy(update={"indexing": selected.indexing.model_copy(
                    update={"index_generation": None},
                )})
            return self._resolve_parse_config(selected).model_dump(mode="json")

        task = self._repository.reparse(
            session_id,
            tenant_id=normalized_workspace,
            expected_job_version=expected_job_version,
            actor_user_id=actor_user_id,
            parse_config=(parse_config.model_dump(mode="json") if parse_config is not None else None),
            snapshot_resolver=resolve_snapshot,
            now=self._now(),
        )
        _logger.info(
            "upload_full_reparse_requested upload_session_id=%s video_parse_task_id=%s expected_parse_version=%s",
            session_id,
            task.video_parse_task_id,
            expected_job_version,
        )
        return task

    def delete_session(self, upload_session_id: str, *, tenant_id: str) -> UploadSessionState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        deletion = self._repository.begin_delete_session(
            session_id,
            tenant_id=require_nonempty_text(tenant_id, "tenant_id"),
            now=self._now(),
        )
        try:
            self._artifact_store.delete_upload_staging(session_id)
            if (
                deletion.video_parse_document_keys
                and self._video_parse_index_store is None
            ):
                raise RuntimeError(
                    "Video Parse V2 index cleanup store is unavailable"
                )
            if self._video_parse_index_store is not None:
                keys = deletion.video_parse_document_keys
                if keys:
                    self._video_parse_index_store.bulk_delete(
                        keys,
                        refresh="wait_for",
                    )
                    if not self._video_parse_index_store.verify_absent(keys):
                        raise RuntimeError(
                            "Video Parse V2 index cleanup was not visible"
                        )
                    if self._video_event_index_store is not None:
                        self._video_event_index_store.bulk_delete(keys, refresh="wait_for")
                        if not self._video_event_index_store.verify_absent(keys):
                            raise RuntimeError("Event index cleanup was not visible")
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
            now=self._now(),
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
        for upload_session_id, tenant_id in pending:
            try:
                self.delete_session(
                    upload_session_id,
                    tenant_id=tenant_id,
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
        tenant_id: str,
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
            tenant_id=tenant_id,
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
                now=self._now(),
            )
        except (UploadStateError, UploadChecksumMismatchError, FileNotFoundError) as exc:
            if self._repository.get_session(session_id, now=self._now()).status == UploadSessionStatus.EXPIRED.value:
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
        tenant_id: str,
        start_byte: int,
        end_byte_exclusive: int,
        total_size_bytes: int,
    ) -> None:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        current = self.get_session(session_id, tenant_id=tenant_id)
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
        tenant_id: str,
    ) -> CompletedUploadState:
        session_id = canonical_uuid(upload_session_id, "upload_session_id")
        self.get_session(session_id, tenant_id=tenant_id)
        claim = self._repository.claim_completion(
            session_id,
            now=self._now(),
            stale_after=self._config.completion_stale_after,
        )
        if claim.already_completed:
            if claim.session.video_id is None:
                raise UploadConflictError(
                    "completed upload is missing its Video"
                )
            if claim.existing_video_parse_task_id is None:
                raise UploadConflictError(
                    "completed upload is missing its VideoParseTask"
                )
            return CompletedUploadState(
                session=claim.session,
                video_id=claim.session.video_id,
                job_id=claim.existing_job_id,
                video_parse_task_id=claim.existing_video_parse_task_id,
            )
        assert claim.completion_token is not None
        started_at = time.perf_counter()
        _logger.info("upload_completion_started upload_session_id=%s", session_id)

        published_video_id: str | None = None
        try:
            snapshot = _completion_parse_config(claim.session)
            parse_config = self._resolve_parse_config(snapshot)
            if snapshot.indexing.contract_fingerprint != parse_config.indexing.contract_fingerprint:
                raise UploadConflictError("upload index contract differs from its frozen configuration")
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
            duration_seconds = self._probe_duration(
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
                duration_seconds=duration_seconds,
                pipeline_version=self._config.pipeline_version,
                parse_config=parse_config.model_dump(mode="json"),
                max_video_parse_attempts=(
                    parse_config.execution.max_retry_count + 1
                ),
                now=self._now(),
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
            "upload_completed upload_session_id=%s video_id=%s job_id=%s video_parse_task_id=%s duration_ms=%.1f",
            session_id, completed.video_id, completed.job_id,
            completed.video_parse_task_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return completed

    def _resolve_parse_config(self, config: ParseConfig | VideoParseV2Config) -> VideoParseV2Config:
        if isinstance(config, ParseConfig):
            config = config.resolve_execution(self._parse_defaults).to_video_parse_v2()
        indexing = self._parse_defaults.indexing
        generation = indexing.index_generation if indexing is not None else None
        if type(generation) is not int or generation <= 0:
            raise ValueError("server parse defaults require an index_generation")
        if config.indexing.index_generation not in (None, generation):
            raise ValueError("parse_config.indexing.index_generation must match the active index generation")
        config = config.model_copy(update={"indexing": config.indexing.model_copy(
            update={"index_generation": generation},
        )})
        validate_video_parse_schedule(config)
        return self._require_video_parse_index_compatibility(config)

    def _require_video_parse_index_compatibility(
        self,
        config: VideoParseV2Config,
    ) -> VideoParseV2Config:
        store = self._video_parse_index_store
        if store is None:
            return config
        embedding = config.models.embedding
        dimensions = embedding.dimensions
        if (
            dimensions != store.semantic_dimensions
            or dimensions != store.vision_dimensions
        ):
            raise ValueError(
                "parse_config.models.embedding.dimensions must match "
                "the active Elasticsearch semantic and vision dimensions"
            )
        active_encoder = self._video_parse_query_encoder
        active_source = self._video_parse_query_encoder_source
        if active_encoder is None or active_source is None:
            raise ValueError(
                "the active Elasticsearch index is missing its V2 query encoder binding"
            )
        # Provider and adapter revision belong to the server-owned source
        # binding. Requiring that same source plus the public EncoderContract
        # fields prevents an accepted task from producing vectors that the
        # active query encoder can never search.
        reference = ModelReference.resolve(
            embedding.model, encoder_model=embedding.encoder_model,
            default_encoder_model=active_encoder.model,
        )
        if (
            embedding.source != active_source
            or reference.model != active_encoder.model
            or embedding.dimensions != active_encoder.dimensions
        ):
            raise ValueError(
                "parse_config.models.embedding must match the exact active "
                "V2 query encoder source, model, and dimensions"
            )
        resolved_embedding = embedding.model_copy(update={"encoder_model": reference.model})
        return config.model_copy(update={
            "models": config.models.model_copy(update={"embedding": resolved_embedding}),
            "indexing": config.indexing.model_copy(update={
                "contract_fingerprint": index_contract_fingerprint(
                    resolved_embedding, provider=active_encoder.provider,
                    encoder_revision=active_encoder.encoder_revision,
                    max_height=config.visual_input.max_height,
                ),
            }),
        })


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


def _completion_parse_config(
    session: UploadSessionState,
) -> VideoParseV2Config:
    if session.parse_config is None:
        raise UploadConflictError(
            "upload session is missing its fixed V2 parse configuration snapshot"
        )
    return VideoParseV2Config.model_validate(session.parse_config)
