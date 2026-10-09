from __future__ import annotations

import logging
from collections.abc import Sequence
from copy import deepcopy

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engine.rag.nlp.transcript import (
    TranscriptChunkDocument,
    TranscriptCueDocument,
)

from ..model.dto.keyframeDTO import KeyframeBackfillCandidate
from ..model.dto.jobDTO import ParseJobLease
from ..model.dto.parseDTO import ParseImportReport, PreparedVideoParseImport
from ..model.dto.videoDTO import HydratedVideoChunkState
from ..model.index_sync import IndexSyncIntent, IndexSyncStatus, IndexSyncOperation
from ..model.job import ParseJobStage, ParseJobStatus, SeriesParseJob
from ..model.video import (
    ContextSnapshot,
    Series,
    Video,
    VideoChunkInfo,
    VideoChunkEmbedding,
    VideoChunk,
)
from ..mq.contracts import ParseJobLeaseLostError
from ..schema import TranscriptCueView, VideoChunkEvidence, VideoChunkKeyframeReference
from ..utils.media import merge_keyframe_uri_values
from ..utils.time import now_unix_time_ms
from ..utils.validation import canonical_uuid, require_nonempty_text
from .index_sync import list_ready_video_ids, materialize_index_sync_intents


_logger = logging.getLogger(__name__)


def _soft_validated_transcript(
    raw_cues: object,
    *,
    offset_ms: int,
) -> tuple[list[TranscriptCueView], int, int]:
    if not isinstance(raw_cues, list):
        return [], 0, 1

    cues: list[TranscriptCueView] = []
    retained_point_cue_count = 0
    skipped_invalid_cue_count = 0
    for raw_cue in raw_cues:
        if not isinstance(raw_cue, dict):
            skipped_invalid_cue_count += 1
            continue
        text = raw_cue.get("text")
        start_seconds = raw_cue.get("start_seconds")
        end_seconds = raw_cue.get("end_seconds")
        if not isinstance(text, str) or not text.strip():
            skipped_invalid_cue_count += 1
            continue
        if (
            isinstance(start_seconds, bool)
            or not isinstance(start_seconds, int)
            or isinstance(end_seconds, bool)
            or not isinstance(end_seconds, int)
            or start_seconds < 0
            or end_seconds < start_seconds
        ):
            skipped_invalid_cue_count += 1
            continue
        if end_seconds == start_seconds:
            retained_point_cue_count += 1
        speaker = raw_cue.get("speaker")
        cues.append(
            TranscriptCueView(
                speaker=speaker if isinstance(speaker, str) else None,
                text=text,
                start_seconds=offset_ms + start_seconds,
                end_seconds=offset_ms + end_seconds,
            )
        )
    return cues, retained_point_cue_count, skipped_invalid_cue_count


class ParseImportConflictError(RuntimeError):
    """A stable business ID already refers to another source snapshot."""


class VideoChunkNotFoundError(LookupError):
    """One or more candidate IDs cannot be hydrated from the fact source."""


class SeriesScopeNotFoundError(LookupError):
    """One or more requested Series do not exist in the fact source."""


class SeriesNotReadyError(RuntimeError):
    """One or more requested Series have not reached the searchable state."""


class VideoRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_keyframe_backfill_candidates(
        self,
        *,
        limit: int,
        tenant_id: str | None = None,
        series_id: str | None = None,
        video_id: str | None = None,
        after: KeyframeBackfillCandidate | None = None,
    ) -> list[KeyframeBackfillCandidate]:
        """Page through scoped chunks for service-level media inspection."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        normalized_workspace = (
            require_nonempty_text(tenant_id, "tenant_id")
            if tenant_id is not None
            else None
        )
        normalized_series = (
            canonical_uuid(series_id, "series_id")
            if series_id is not None
            else None
        )
        normalized_video = (
            canonical_uuid(video_id, "video_id")
            if video_id is not None
            else None
        )
        statement = (
            select(
                Series.tenant_id,
                Series.series_id,
                Video.video_id,
                Video.episode_order,
                Video.duration_seconds,
                VideoChunk.video_chunk_id,
                VideoChunk.sequence_no,
                VideoChunk.start_seconds,
                VideoChunk.end_seconds,
                VideoChunk.chunk_media_ref,
                VideoChunk.keyframe_uris,
            )
            .join(Video, Video.series_id == Series.series_id)
            .join(
                VideoChunk,
                VideoChunk.video_id == Video.video_id,
            )
            .order_by(
                Series.series_id,
                Video.episode_order,
                VideoChunk.sequence_no,
                VideoChunk.video_chunk_id,
            )
        )
        if normalized_workspace is not None:
            statement = statement.where(
                Series.tenant_id == normalized_workspace
            )
        if normalized_series is not None:
            statement = statement.where(
                Series.series_id == normalized_series
            )
        if normalized_video is not None:
            statement = statement.where(Video.video_id == normalized_video)
        if after is not None:
            statement = statement.where(
                or_(
                    Series.series_id > after.series_id,
                    and_(
                        Series.series_id == after.series_id,
                        Video.episode_order > after.episode_order,
                    ),
                    and_(
                        Series.series_id == after.series_id,
                        Video.episode_order == after.episode_order,
                        VideoChunk.sequence_no > after.sequence_no,
                    ),
                    and_(
                        Series.series_id == after.series_id,
                        Video.episode_order == after.episode_order,
                        VideoChunk.sequence_no == after.sequence_no,
                        VideoChunk.video_chunk_id > after.video_chunk_id,
                    ),
                )
            )

        with self._session_factory() as session:
            rows = session.execute(statement.limit(limit)).all()
        return [
            KeyframeBackfillCandidate(
                tenant_id=row.tenant_id,
                series_id=row.series_id,
                video_id=row.video_id,
                episode_order=row.episode_order,
                duration_seconds=row.duration_seconds,
                video_chunk_id=row.video_chunk_id,
                sequence_no=row.sequence_no,
                start_seconds=row.start_seconds,
                end_seconds=row.end_seconds,
                chunk_media_ref=row.chunk_media_ref,
                keyframe_snapshot=tuple(deepcopy(row.keyframe_uris)),
            )
            for row in rows
        ]

    def set_keyframe_if_missing(
        self,
        candidate: KeyframeBackfillCandidate,
        keyframe: VideoChunkKeyframeReference,
    ) -> bool:
        """Write a generated frame only while the scanned chunk snapshot is current."""

        if (
            keyframe.at_seconds is None
            or not candidate.start_seconds <= keyframe.at_seconds < candidate.end_seconds
        ):
            raise ValueError("keyframe must use an absolute time inside the VideoChunk")
        with self._session_factory.begin() as session:
            chunk = session.scalar(
                select(VideoChunk)
                .where(
                    VideoChunk.video_chunk_id == candidate.video_chunk_id
                )
                .with_for_update()
            )
            if chunk is None or (
                chunk.video_id != candidate.video_id
                or chunk.start_seconds != candidate.start_seconds
                or chunk.end_seconds != candidate.end_seconds
                or chunk.chunk_media_ref != candidate.chunk_media_ref
            ):
                return False
            if chunk.keyframe_uris != list(candidate.keyframe_snapshot):
                return False
            chunk.keyframe_uris = merge_keyframe_uri_values(
                [keyframe.model_dump(mode="json")],
                chunk.keyframe_uris,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
            )
            return True

    def import_video_parse(
        self,
        prepared: PreparedVideoParseImport,
    ) -> ParseImportReport:
        """Persist one complete Video parse atomically and idempotently."""

        if not prepared.chunks:
            raise ValueError(
                "a Video parse import must contain at least one VideoChunk"
            )
        report = ParseImportReport(
            series_id=prepared.series_id,
            video_id=prepared.video_id,
            video_chunk_ids=[item.video_chunk_id for item in prepared.chunks],
            chunk_count=len(prepared.chunks),
            vectors_imported=all(
                item.text_embedding is not None and item.vision_embedding is not None
                for item in prepared.chunks
            ),
        )

        for attempt in range(2):
            try:
                self._import_video_parse_once(prepared)
            except IntegrityError:
                if attempt == 1:
                    raise
            else:
                return report
        raise AssertionError("unreachable")

    def import_video_parse_for_job(
        self,
        prepared: PreparedVideoParseImport,
        lease: ParseJobLease,
    ) -> ParseImportReport:
        """Persist Worker output only while its lease is current."""

        if (
            prepared.series_id != lease.series_id
            or prepared.video_id != lease.video_id
            or prepared.pipeline_version != lease.pipeline_version
            or prepared.episode_order != lease.episode_order
            or prepared.original_source_uri != lease.original_source_uri
            or prepared.content_hash != lease.content_hash
        ):
            raise ParseImportConflictError(
                "prepared parse output does not match the claimed ParseJob"
            )
        if not prepared.chunks or any(
            item.text_embedding is None or item.vision_embedding is None
            for item in prepared.chunks
        ):
            raise ValueError(
                "a Worker import requires text and vision vectors for every VideoChunk"
            )
        report = ParseImportReport(
            series_id=prepared.series_id,
            video_id=prepared.video_id,
            video_chunk_ids=[item.video_chunk_id for item in prepared.chunks],
            chunk_count=len(prepared.chunks),
            vectors_imported=True,
        )
        for attempt in range(2):
            try:
                self._import_video_parse_once(prepared, lease=lease)
            except IntegrityError:
                if attempt == 1:
                    raise
            else:
                return report
        raise AssertionError("unreachable")

    def _import_video_parse_once(
        self,
        prepared: PreparedVideoParseImport,
        *,
        lease: ParseJobLease | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            job: SeriesParseJob | None = None
            if lease is not None:
                checked_at = now_unix_time_ms()
                job = session.scalar(
                    select(SeriesParseJob)
                    .where(
                        SeriesParseJob.job_id == lease.job_id,
                        SeriesParseJob.status
                        == ParseJobStatus.RUNNING.value,
                        SeriesParseJob.lease_owner == lease.worker_id,
                        SeriesParseJob.lease_version
                        == lease.lease_version,
                        SeriesParseJob.lease_expires_at > checked_at,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise ParseJobLeaseLostError(
                        "parse job lease is no longer current"
                    )
            existing_series = session.get(Series, prepared.series_id)
            if existing_series is not None and (
                existing_series.tenant_id != prepared.tenant_id
                or existing_series.name.casefold() != prepared.series_name.casefold()
            ):
                raise ParseImportConflictError(
                    f"series_id {prepared.series_id!r} already identifies "
                    "another Series"
                )
            if existing_series is None:
                series_with_same_name = session.scalar(
                    select(Series).where(
                        Series.tenant_id == prepared.tenant_id,
                        Series.name == prepared.series_name,
                    )
                )
                if series_with_same_name is not None:
                    raise ParseImportConflictError(
                        "tenant_id and Series name already use another series_id"
                    )
                session.add(
                    Series(
                        series_id=prepared.series_id,
                        tenant_id=prepared.tenant_id,
                        name=prepared.series_name,
                        description=prepared.series_description,
                        status="INDEXING",
                        important_extra_data=prepared.important_extra_data,
                        character_bible=prepared.character_bible,
                    )
                )

            existing_video = session.get(Video, prepared.video_id)
            if existing_video is not None and (
                existing_video.series_id != prepared.series_id
                or (
                    lease is None
                    and existing_video.episode_order != prepared.episode_order
                )
                or existing_video.content_hash != prepared.content_hash
                or existing_video.duration_seconds != prepared.parse_result.duration_seconds
            ):
                raise ParseImportConflictError(
                    f"video_id {prepared.video_id!r} already identifies another Video"
                )
            if existing_video is None:
                video_with_same_business_key = session.scalar(
                    select(Video).where(
                        Video.series_id == prepared.series_id,
                        or_(
                            Video.episode_order == prepared.episode_order,
                            Video.content_hash == prepared.content_hash,
                        ),
                    )
                )
                if video_with_same_business_key is not None:
                    raise ParseImportConflictError(
                        "episode order or source content already uses another video_id"
                    )
                existing_video = Video(
                    video_id=prepared.video_id,
                    series_id=prepared.series_id,
                    episode_order=prepared.episode_order,
                    upload_sequence=prepared.upload_sequence,
                    original_filename=prepared.original_filename,
                    original_source_uri=prepared.original_source_uri,
                    inference_proxy_uri=None,
                    content_hash=prepared.content_hash,
                    total_bytes=prepared.total_bytes,
                    duration_seconds=prepared.parse_result.duration_seconds,
                    upload_status="COMPLETED",
                    parse_status=(
                        ParseJobStatus.RUNNING.value
                        if lease is not None
                        else "COMPLETED"
                    ),
                    upload_progress=100,
                    parse_progress=100 if lease is None else 0,
                )
                session.add(existing_video)
            else:
                if lease is None:
                    existing_video.parse_status = "COMPLETED"
                elif not lease.preserve_existing_result:
                    existing_video.parse_status = ParseJobStatus.RUNNING.value
                if lease is None:
                    existing_video.parse_progress = 100

            existing_chunks = session.scalars(
                select(VideoChunk).where(
                    VideoChunk.video_id == prepared.video_id
                )
            ).all()
            if existing_chunks:
                actual = {
                    (
                        item.video_chunk_id,
                        item.sequence_no,
                        item.start_seconds,
                        item.end_seconds,
                    )
                    for item in existing_chunks
                }
                expected = {
                    (
                        item.video_chunk_id,
                        item.parse_result.window.sequence_no,
                        item.parse_result.window.chunk_start_seconds,
                        item.parse_result.window.chunk_end_seconds,
                    )
                    for item in prepared.chunks
                }
                if actual != expected:
                    raise ParseImportConflictError(
                        "the persisted Video uses a different canonical "
                        "VideoChunk snapshot"
                    )

            session.flush()

            existing_chunks_by_id = {
                item.video_chunk_id: item for item in existing_chunks
            }
            for item in prepared.chunks:
                chunk = item.parse_result
                window = chunk.window
                existing_chunk = existing_chunks_by_id.get(
                    item.video_chunk_id
                )
                if existing_chunk is not None:
                    # Canonical media may already have been promoted from the
                    # transitional file URI into the Artifact Store.
                    existing_chunk.analysis_window_start_seconds = (
                        window.analysis_start_seconds
                    )
                    existing_chunk.analysis_window_end_seconds = window.analysis_end_seconds
                    existing_chunk.analysis_window_media_ref = (
                        item.analysis_window_media_ref
                    )
                    existing_chunk.transcript = [
                        cue.model_dump(mode="json")
                        for cue in chunk.candidate_transcript
                    ]
                    if item.keyframes:
                        existing_chunk.keyframe_uris = merge_keyframe_uri_values(
                            [
                                keyframe.model_dump(mode="json")
                                for keyframe in item.keyframes
                            ],
                            existing_chunk.keyframe_uris,
                            start_seconds=window.chunk_start_seconds,
                            end_seconds=window.chunk_end_seconds,
                        )
                    continue
                session.add(
                    VideoChunk(
                        video_chunk_id=item.video_chunk_id,
                        video_id=prepared.video_id,
                        sequence_no=window.sequence_no,
                        start_seconds=window.chunk_start_seconds,
                        end_seconds=window.chunk_end_seconds,
                        analysis_window_start_seconds=window.analysis_start_seconds,
                        analysis_window_end_seconds=window.analysis_end_seconds,
                        chunk_media_ref=item.chunk_media_ref,
                        analysis_window_media_ref=item.analysis_window_media_ref,
                        transcript=[
                            cue.model_dump(mode="json")
                            for cue in chunk.candidate_transcript
                        ],
                        baseline_visual_caption=None,
                        keyframe_uris=[
                            keyframe.model_dump(mode="json")
                            for keyframe in item.keyframes
                        ],
                    )
                )
            session.flush()

            chunk_ids = [item.video_chunk_id for item in prepared.chunks]
            embeddings_by_chunk_id = {
                embedding.video_chunk_id: embedding
                for embedding in session.scalars(
                    select(VideoChunkEmbedding).where(
                        VideoChunkEmbedding.video_chunk_id.in_(
                            chunk_ids
                        )
                    )
                ).all()
            }
            for item in prepared.chunks:
                chunk = item.parse_result
                provenance = (
                    chunk.video_parse.model_dump(mode="json")
                    if chunk.video_parse is not None
                    else None
                )
                if chunk.video_parse is not None:
                    model_provider = chunk.video_parse.provider
                    model_name = chunk.video_parse.model
                elif prepared.parse_result.models is not None:
                    model_provider = "doubao"
                    model_name = prepared.parse_result.models.video_parser
                else:
                    model_provider = None
                    model_name = None
                session.merge(
                    VideoChunkInfo(
                        video_chunk_id=item.video_chunk_id,
                        all_time_events=[
                            event.model_dump(mode="json") for event in chunk.events
                        ],
                        all_time_event_text=chunk.self_compact,
                        video_compact=chunk.self_compact,
                        context_before_compact=chunk.context_before_compact,
                        context_after_compact=chunk.context_after_compact,
                        window_summary=chunk.window_summary,
                        model_provider=model_provider,
                        model_name=model_name,
                        pipeline_version=prepared.pipeline_version,
                        is_fallback=chunk.is_fallback,
                        parse_provenance=provenance,
                        parsed_at=prepared.parsed_at,
                    )
                )
                session.merge(
                    ContextSnapshot(
                        snapshot_id=item.snapshot_id,
                        engine_snapshot_id=chunk.context_after.snapshot_id,
                        series_id=prepared.series_id,
                        video_id=prepared.video_id,
                        after_chunk_sequence_no=(
                            chunk.context_after.after_chunk_sequence_no
                        ),
                        compact_text=chunk.context_after.compact_text,
                        structured_state={
                            "recent_events": [
                                event.model_dump(mode="json")
                                for event in chunk.context_after.recent_events
                            ]
                        },
                        created_from_video_chunk_id=item.video_chunk_id,
                    )
                )
                embedding = embeddings_by_chunk_id.get(item.video_chunk_id)
                if embedding is None:
                    session.add(
                        VideoChunkEmbedding(
                            video_chunk_id=item.video_chunk_id,
                            text_input=item.retrieval_document.text,
                            text_vector=(
                                item.text_embedding.vector
                                if item.text_embedding is not None
                                else None
                            ),
                            vision_vector=(
                                item.vision_embedding.vector
                                if item.vision_embedding is not None
                                else None
                            ),
                            text_embedding_metadata=(
                                item.text_embedding.metadata
                                if item.text_embedding is not None
                                else None
                            ),
                            vision_embedding_metadata=(
                                item.vision_embedding.metadata
                                if item.vision_embedding is not None
                                else None
                            ),
                        )
                    )
                    continue

                text_input_changed = (
                    embedding.text_input != item.retrieval_document.text
                )
                embedding.text_input = item.retrieval_document.text
                if item.text_embedding is not None:
                    embedding.text_vector = item.text_embedding.vector
                    embedding.text_embedding_metadata = item.text_embedding.metadata
                elif text_input_changed:
                    embedding.text_vector = None
                    embedding.text_embedding_metadata = None
                if item.vision_embedding is not None:
                    embedding.vision_vector = item.vision_embedding.vector
                    embedding.vision_embedding_metadata = (
                        item.vision_embedding.metadata
                    )

            # The desired Qdrant state is committed with the source facts. The
            # external write is deliberately handled after this transaction.
            materialize_index_sync_intents(session, chunk_ids)
            if job is not None:
                if (
                    job.lease_expires_at is None
                    or job.lease_expires_at <= now_unix_time_ms()
                ):
                    raise ParseJobLeaseLostError(
                        "parse job lease expired while facts were being persisted"
                    )
                if job.stage != ParseJobStage.INDEXING.value:
                    job.version += 1
                job.stage = ParseJobStage.INDEXING.value
                if not lease.preserve_existing_result:
                    existing_video.parse_status = ParseJobStatus.RUNNING.value

    def hydrate_video_chunks(
        self,
        video_chunk_ids: Sequence[str],
    ) -> list[VideoChunkEvidence]:
        """Hydrate candidate IDs with one joined fact-source query."""

        return [
            item.evidence
            for item in self.hydrate_video_chunk_states(video_chunk_ids)
        ]

    def require_ready_series(
        self,
        series_ids: Sequence[str],
        *,
        tenant_id: str,
    ) -> list[str]:
        """Resolve a complete, READY Series scope without silently narrowing it."""

        if isinstance(series_ids, (str, bytes)):
            raise ValueError("series_ids must be a sequence of IDs")
        try:
            ordered_ids = list(
                dict.fromkeys(
                    canonical_uuid(item, "series_id") for item in series_ids
                )
            )
        except TypeError as exc:
            raise ValueError("series_ids must be a sequence of IDs") from exc
        if not ordered_ids:
            raise ValueError("series_ids must not be empty")

        with self._session_factory() as session:
            rows = session.execute(
                select(Series.series_id, Series.status).where(
                    Series.series_id.in_(ordered_ids),
                    Series.tenant_id == tenant_id,
                )
            ).all()
        statuses = {series_id: status for series_id, status in rows}
        missing = [series_id for series_id in ordered_ids if series_id not in statuses]
        if missing:
            raise SeriesScopeNotFoundError(
                "seriesId values do not exist in this workspace: " + ", ".join(missing)
            )
        not_ready = [
            f"{series_id} ({statuses[series_id]})"
            for series_id in ordered_ids
            if statuses[series_id] != "READY"
        ]
        if not_ready:
            raise SeriesNotReadyError(
                "Series are not READY for retrieval: " + ", ".join(not_ready)
            )
        return ordered_ids

    def list_indexed_video_ids(self, series_ids: Sequence[str], *, tenant_id: str) -> list[str]:
        if not series_ids:
            return []
        with self._session_factory() as session:
            scoped_series_ids = list(session.scalars(
                select(Series.series_id).where(
                    Series.tenant_id == tenant_id,
                    Series.series_id.in_(series_ids),
                )
            ))
            return list_ready_video_ids(session, scoped_series_ids)

    def list_searchable_transcripts(
        self,
        series_ids: Sequence[str],
        *,
        tenant_id: str,
        video_ids: Sequence[str],
    ) -> list[TranscriptChunkDocument]:
        """Load ASR cues only for complete, searchable videos in the scope."""

        if isinstance(series_ids, (str, bytes)):
            raise ValueError("series_ids must be a sequence of IDs")
        ordered_series_ids = list(
            dict.fromkeys(
                canonical_uuid(series_id, "series_id") for series_id in series_ids
            )
        )
        if not ordered_series_ids:
            raise ValueError("series_ids must not be empty")
        normalized_workspace = require_nonempty_text(tenant_id, "tenant_id")
        if not video_ids:
            return []
        statement = (
            select(
                VideoChunk.video_chunk_id,
                VideoChunk.start_seconds,
                VideoChunk.end_seconds,
                VideoChunk.analysis_window_start_seconds,
                VideoChunk.transcript,
            )
            .join(Video, Video.video_id == VideoChunk.video_id)
            .join(Series, Series.series_id == Video.series_id)
            .join(
                IndexSyncIntent,
                IndexSyncIntent.video_chunk_id
                == VideoChunk.video_chunk_id,
            )
            .where(
                Series.tenant_id == normalized_workspace,
                Series.series_id.in_(ordered_series_ids),
                Video.video_id.in_(video_ids),
                IndexSyncIntent.status == IndexSyncStatus.SYNCED.value,
                IndexSyncIntent.operation == IndexSyncOperation.UPSERT.value,
            )
            .order_by(
                Video.episode_order,
                VideoChunk.sequence_no,
                VideoChunk.video_chunk_id,
            )
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()

        documents: list[TranscriptChunkDocument] = []
        retained_point_cue_count = 0
        skipped_invalid_cue_count = 0
        affected_chunk_ids: set[str] = set()
        for row in rows:
            cues: list[TranscriptCueDocument] = []
            validated_cues, retained_count, skipped_count = (
                _soft_validated_transcript(
                    row.transcript,
                    offset_ms=row.analysis_window_start_seconds,
                )
            )
            retained_point_cue_count += retained_count
            skipped_invalid_cue_count += skipped_count
            if retained_count or skipped_count:
                affected_chunk_ids.add(row.video_chunk_id)
            for cue in validated_cues:
                document_cue = TranscriptCueDocument(
                    text=cue.text,
                    start_seconds=cue.start_seconds,
                    end_seconds=cue.end_seconds,
                )
                # Analysis windows overlap. Assign each repeated cue to the
                # canonical chunk containing its midpoint before ranking it.
                midpoint_ms = cue.start_seconds + (cue.end_seconds - cue.start_seconds) // 2
                if not row.start_seconds <= midpoint_ms < row.end_seconds:
                    continue
                cues.append(document_cue)
            if cues:
                documents.append(
                    TranscriptChunkDocument(
                        video_chunk_id=row.video_chunk_id,
                        cues=cues,
                    )
                )
        if retained_point_cue_count or skipped_invalid_cue_count:
            _logger.warning(
                "event=persisted_transcript_cues_soft_validated "
                "tenant_id=%s series_count=%s affected_chunk_count=%s "
                "retained_point_cue_count=%s skipped_invalid_cue_count=%s "
                "action=continue",
                normalized_workspace,
                len(ordered_series_ids),
                len(affected_chunk_ids),
                retained_point_cue_count,
                skipped_invalid_cue_count,
            )
        return documents

    def hydrate_video_chunk_states(
        self,
        video_chunk_ids: Sequence[str],
    ) -> list[HydratedVideoChunkState]:
        """Hydrate Evidence and current index state with one joined query."""

        if isinstance(video_chunk_ids, (str, bytes)):
            raise ValueError("video_chunk_ids must be a sequence of IDs")
        try:
            ordered_ids = list(
                dict.fromkeys(
                    canonical_uuid(item, "video_chunk_id")
                    for item in video_chunk_ids
                )
            )
        except TypeError as exc:
            raise ValueError(
                "video_chunk_ids must be a sequence of IDs"
            ) from exc
        if not ordered_ids:
            return []

        statement = (
            select(
                Series,
                Video,
                VideoChunk,
                VideoChunkInfo,
                VideoChunkEmbedding,
                IndexSyncIntent,
            )
            .join(Video, Video.series_id == Series.series_id)
            .join(
                VideoChunk,
                VideoChunk.video_id == Video.video_id,
            )
            .join(
                VideoChunkInfo,
                VideoChunkInfo.video_chunk_id
                == VideoChunk.video_chunk_id,
            )
            .join(
                VideoChunkEmbedding,
                VideoChunkEmbedding.video_chunk_id
                == VideoChunk.video_chunk_id,
            )
            .outerjoin(
                IndexSyncIntent,
                IndexSyncIntent.video_chunk_id
                == VideoChunk.video_chunk_id,
            )
            .where(VideoChunk.video_chunk_id.in_(ordered_ids))
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()

        states_by_id: dict[str, HydratedVideoChunkState] = {}
        for series, video, chunk, info, embedding, intent in rows:
            transcript, _retained_count, _skipped_count = (
                _soft_validated_transcript(
                    chunk.transcript,
                    offset_ms=chunk.analysis_window_start_seconds,
                )
            )
            evidence = VideoChunkEvidence(
                series_id=series.series_id,
                tenant_id=series.tenant_id,
                series_name=series.name,
                series_description=series.description,
                series_status=series.status,
                important_extra_data=series.important_extra_data,
                character_bible=series.character_bible,
                video_id=video.video_id,
                episode_order=video.episode_order,
                original_filename=video.original_filename,
                original_source_uri=video.original_source_uri,
                inference_proxy_uri=video.inference_proxy_uri,
                content_hash=video.content_hash,
                duration_seconds=video.duration_seconds,
                upload_status=video.upload_status,
                parse_status=video.parse_status,
                video_chunk_id=chunk.video_chunk_id,
                sequence_no=chunk.sequence_no,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
                analysis_window_start_seconds=chunk.analysis_window_start_seconds,
                analysis_window_end_seconds=chunk.analysis_window_end_seconds,
                chunk_media_ref=chunk.chunk_media_ref,
                analysis_window_media_ref=chunk.analysis_window_media_ref,
                transcript=transcript,
                baseline_visual_caption=chunk.baseline_visual_caption,
                keyframe_uris=chunk.keyframe_uris,
                all_time_events=info.all_time_events,
                all_time_event_text=info.all_time_event_text,
                retrieval_text=embedding.text_input,
                video_compact=info.video_compact,
                context_before_compact=info.context_before_compact,
                context_after_compact=info.context_after_compact,
                window_summary=info.window_summary,
                model_provider=info.model_provider,
                model_name=info.model_name,
                pipeline_version=info.pipeline_version,
                is_fallback=info.is_fallback,
                parse_provenance=info.parse_provenance,
                parsed_at=info.parsed_at,
            )
            states_by_id[chunk.video_chunk_id] = HydratedVideoChunkState(
                evidence=evidence,
                source_revision=(
                    intent.source_revision if intent is not None else None
                ),
                index_operation=(intent.operation if intent is not None else None),
                index_status=(intent.status if intent is not None else None),
            )

        missing = [item for item in ordered_ids if item not in states_by_id]
        if missing:
            raise VideoChunkNotFoundError(
                "videoChunkId values are missing from MySQL: " + ", ".join(missing)
            )
        return [states_by_id[item] for item in ordered_ids]
