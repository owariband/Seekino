from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoRecord,
    VideoSegmentInfoRecord,
    VideoSegmentParseChunkRecord,
    VideoSegmentRecord,
)
from ..schema import ParseImportReport, VideoSegmentEvidence
from ..service.parse_import import PreparedVideoParseImport
from .index_sync import materialize_index_sync_intents


class ParseImportConflictError(RuntimeError):
    """A stable business ID already refers to another source snapshot."""


class VideoSegmentNotFoundError(LookupError):
    """One or more candidate IDs cannot be hydrated from the fact source."""


class VideoRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def import_video_parse(
        self,
        prepared: PreparedVideoParseImport,
    ) -> ParseImportReport:
        """Persist one complete Video parse atomically and idempotently."""

        if not prepared.segments:
            raise ValueError("a Video parse import must contain at least one Segment")
        report = ParseImportReport(
            series_id=prepared.series_id,
            video_id=prepared.video_id,
            video_segment_ids=[item.video_segment_id for item in prepared.segments],
            segment_count=len(prepared.segments),
            vectors_imported=all(
                item.text_embedding is not None and item.vision_embedding is not None
                for item in prepared.segments
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

    def _import_video_parse_once(
        self,
        prepared: PreparedVideoParseImport,
    ) -> None:
        with self._session_factory.begin() as session:
            existing_series = session.get(SeriesRecord, prepared.series_id)
            if existing_series is not None and (
                existing_series.workspace_id != prepared.workspace_id
                or existing_series.name.casefold() != prepared.series_name.casefold()
            ):
                raise ParseImportConflictError(
                    f"series_id {prepared.series_id!r} already identifies "
                    "another Series"
                )
            if existing_series is None:
                series_with_same_name = session.scalar(
                    select(SeriesRecord).where(
                        SeriesRecord.workspace_id == prepared.workspace_id,
                        SeriesRecord.name == prepared.series_name,
                    )
                )
                if series_with_same_name is not None:
                    raise ParseImportConflictError(
                        "workspace_id and Series name already use another series_id"
                    )
                session.add(
                    SeriesRecord(
                        series_id=prepared.series_id,
                        workspace_id=prepared.workspace_id,
                        name=prepared.series_name,
                        description=prepared.series_description,
                        status="INDEXING",
                        important_extra_data=prepared.important_extra_data,
                        character_bible=prepared.character_bible,
                    )
                )

            existing_video = session.get(VideoRecord, prepared.video_id)
            if existing_video is not None and (
                existing_video.series_id != prepared.series_id
                or existing_video.episode_order != prepared.episode_order
                or existing_video.content_hash != prepared.content_hash
                or existing_video.duration_ms != prepared.parse_result.duration_ms
            ):
                raise ParseImportConflictError(
                    f"video_id {prepared.video_id!r} already identifies another Video"
                )
            if existing_video is None:
                video_with_same_business_key = session.scalar(
                    select(VideoRecord).where(
                        VideoRecord.series_id == prepared.series_id,
                        or_(
                            VideoRecord.episode_order == prepared.episode_order,
                            VideoRecord.content_hash == prepared.content_hash,
                        ),
                    )
                )
                if video_with_same_business_key is not None:
                    raise ParseImportConflictError(
                        "episode order or source content already uses another video_id"
                    )
                session.add(
                    VideoRecord(
                        video_id=prepared.video_id,
                        series_id=prepared.series_id,
                        episode_order=prepared.episode_order,
                        upload_sequence=prepared.upload_sequence,
                        original_filename=prepared.original_filename,
                        original_source_uri=prepared.original_source_uri,
                        inference_proxy_uri=None,
                        content_hash=prepared.content_hash,
                        total_bytes=prepared.total_bytes,
                        duration_ms=prepared.parse_result.duration_ms,
                        upload_status="COMPLETED",
                        parse_status="COMPLETED",
                        upload_progress=100,
                        parse_progress=100,
                    )
                )
            else:
                existing_video.parse_status = "COMPLETED"
                existing_video.parse_progress = 100

            existing_segments = session.scalars(
                select(VideoSegmentRecord).where(
                    VideoSegmentRecord.video_id == prepared.video_id
                )
            ).all()
            if existing_segments:
                actual = {
                    (
                        item.video_segment_id,
                        item.sequence_no,
                        item.start_ms,
                        item.end_ms,
                    )
                    for item in existing_segments
                }
                expected = {
                    (
                        item.video_segment_id,
                        item.parse_result.window.sequence_no,
                        item.parse_result.window.segment_start_ms,
                        item.parse_result.window.segment_end_ms,
                    )
                    for item in prepared.segments
                }
                if actual != expected:
                    raise ParseImportConflictError(
                        "the persisted Video uses a different canonical "
                        "Segment snapshot"
                    )

            session.flush()

            existing_segments_by_id = {
                item.video_segment_id: item for item in existing_segments
            }
            for item in prepared.segments:
                segment = item.parse_result
                window = segment.window
                existing_segment = existing_segments_by_id.get(
                    item.video_segment_id
                )
                if existing_segment is not None:
                    # Canonical media may already have been promoted from the
                    # transitional file URI into the Artifact Store.
                    existing_segment.analysis_window_start_ms = (
                        window.analysis_start_ms
                    )
                    existing_segment.analysis_window_end_ms = window.analysis_end_ms
                    existing_segment.analysis_window_media_ref = (
                        item.analysis_window_media_ref
                    )
                    existing_segment.transcript = [
                        cue.model_dump(mode="json")
                        for cue in segment.candidate_transcript
                    ]
                    continue
                session.add(
                    VideoSegmentRecord(
                        video_segment_id=item.video_segment_id,
                        video_id=prepared.video_id,
                        sequence_no=window.sequence_no,
                        start_ms=window.segment_start_ms,
                        end_ms=window.segment_end_ms,
                        analysis_window_start_ms=window.analysis_start_ms,
                        analysis_window_end_ms=window.analysis_end_ms,
                        segment_media_ref=item.segment_media_ref,
                        analysis_window_media_ref=item.analysis_window_media_ref,
                        transcript=[
                            cue.model_dump(mode="json")
                            for cue in segment.candidate_transcript
                        ],
                        baseline_visual_caption=None,
                        keyframe_uris=[],
                    )
                )
            session.flush()

            segment_ids = [item.video_segment_id for item in prepared.segments]
            chunks_by_segment_id = {
                chunk.video_segment_id: chunk
                for chunk in session.scalars(
                    select(VideoSegmentParseChunkRecord).where(
                        VideoSegmentParseChunkRecord.video_segment_id.in_(
                            segment_ids
                        )
                    )
                ).all()
            }
            for item in prepared.segments:
                segment = item.parse_result
                provenance = (
                    segment.video_parse.model_dump(mode="json")
                    if segment.video_parse is not None
                    else None
                )
                if segment.video_parse is not None:
                    model_provider = segment.video_parse.provider
                    model_name = segment.video_parse.model
                elif prepared.parse_result.models is not None:
                    model_provider = "doubao"
                    model_name = prepared.parse_result.models.video_parser
                else:
                    model_provider = None
                    model_name = None
                session.merge(
                    VideoSegmentInfoRecord(
                        video_segment_id=item.video_segment_id,
                        all_time_events=[
                            event.model_dump(mode="json") for event in segment.events
                        ],
                        all_time_event_text=segment.self_compact,
                        video_compact=segment.self_compact,
                        context_before_compact=segment.context_before_compact,
                        context_after_compact=segment.context_after_compact,
                        window_summary=segment.window_summary,
                        model_provider=model_provider,
                        model_name=model_name,
                        pipeline_version=prepared.pipeline_version,
                        is_fallback=segment.is_fallback,
                        parse_provenance=provenance,
                        parsed_at=prepared.parsed_at,
                    )
                )
                session.merge(
                    ContextSnapshotRecord(
                        snapshot_id=item.snapshot_id,
                        engine_snapshot_id=segment.context_after.snapshot_id,
                        series_id=prepared.series_id,
                        video_id=prepared.video_id,
                        after_segment_sequence_no=(
                            segment.context_after.after_segment_sequence_no
                        ),
                        compact_text=segment.context_after.compact_text,
                        structured_state={
                            "recent_events": [
                                event.model_dump(mode="json")
                                for event in segment.context_after.recent_events
                            ]
                        },
                        created_from_video_segment_id=item.video_segment_id,
                    )
                )
                chunk = chunks_by_segment_id.get(item.video_segment_id)
                if chunk is None:
                    session.add(
                        VideoSegmentParseChunkRecord(
                            video_segment_id=item.video_segment_id,
                            text_input=item.retrieval_chunk.text,
                            text_chunk=(
                                item.text_embedding.vector
                                if item.text_embedding is not None
                                else None
                            ),
                            vision_chunk=(
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

                text_input_changed = chunk.text_input != item.retrieval_chunk.text
                chunk.text_input = item.retrieval_chunk.text
                if item.text_embedding is not None:
                    chunk.text_chunk = item.text_embedding.vector
                    chunk.text_embedding_metadata = item.text_embedding.metadata
                elif text_input_changed:
                    chunk.text_chunk = None
                    chunk.text_embedding_metadata = None
                if item.vision_embedding is not None:
                    chunk.vision_chunk = item.vision_embedding.vector
                    chunk.vision_embedding_metadata = item.vision_embedding.metadata

            # The desired Qdrant state is committed with the source facts. The
            # external write is deliberately handled after this transaction.
            materialize_index_sync_intents(session, segment_ids)

    def hydrate_video_segments(
        self,
        video_segment_ids: Sequence[str],
    ) -> list[VideoSegmentEvidence]:
        """Hydrate candidate IDs with one joined fact-source query."""

        ordered_ids = list(dict.fromkeys(video_segment_ids))
        if not ordered_ids:
            return []

        statement = (
            select(
                SeriesRecord,
                VideoRecord,
                VideoSegmentRecord,
                VideoSegmentInfoRecord,
            )
            .join(VideoRecord, VideoRecord.series_id == SeriesRecord.series_id)
            .join(
                VideoSegmentRecord,
                VideoSegmentRecord.video_id == VideoRecord.video_id,
            )
            .join(
                VideoSegmentInfoRecord,
                VideoSegmentInfoRecord.video_segment_id
                == VideoSegmentRecord.video_segment_id,
            )
            .where(VideoSegmentRecord.video_segment_id.in_(ordered_ids))
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()

        evidence_by_id: dict[str, VideoSegmentEvidence] = {}
        for series, video, segment, info in rows:
            evidence_by_id[segment.video_segment_id] = VideoSegmentEvidence(
                series_id=series.series_id,
                workspace_id=series.workspace_id,
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
                duration_ms=video.duration_ms,
                upload_status=video.upload_status,
                parse_status=video.parse_status,
                video_segment_id=segment.video_segment_id,
                sequence_no=segment.sequence_no,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                analysis_window_start_ms=segment.analysis_window_start_ms,
                analysis_window_end_ms=segment.analysis_window_end_ms,
                segment_media_ref=segment.segment_media_ref,
                analysis_window_media_ref=segment.analysis_window_media_ref,
                transcript=segment.transcript,
                baseline_visual_caption=segment.baseline_visual_caption,
                keyframe_uris=segment.keyframe_uris,
                all_time_events=info.all_time_events,
                all_time_event_text=info.all_time_event_text,
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

        missing = [item for item in ordered_ids if item not in evidence_by_id]
        if missing:
            raise VideoSegmentNotFoundError(
                "videoSegmentId values are missing from MySQL: " + ", ".join(missing)
            )
        return [evidence_by_id[item] for item in ordered_ids]
