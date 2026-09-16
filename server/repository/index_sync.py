from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, exists, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.indexSyncDTO import IndexSyncItem
from ..model.index_sync import (
    IndexSyncIntentRecord,
    IndexSyncOperation,
    IndexSyncStatus,
)
from ..model.job import ParseJobStatus
from ..model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSessionRecord,
)
from ..model.video import (
    SeriesRecord,
    VideoRecord,
    VideoChunkInfoRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkRecord,
)
from ..utils.time import as_naive_utc, utc_now, utc_now_naive


class IndexSyncRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def materialize_all(self) -> int:
        """Create or update durable desired-state intents from MySQL facts."""

        with self._session_factory.begin() as session:
            return materialize_index_sync_intents(session)

    def materialize(self, video_chunk_ids: Sequence[str]) -> int:
        chunk_ids = list(dict.fromkeys(video_chunk_ids))
        if not chunk_ids:
            raise ValueError("video_chunk_ids must not be empty")
        with self._session_factory.begin() as session:
            return materialize_index_sync_intents(session, chunk_ids)

    def list_due(
        self,
        limit: int,
        *,
        now: datetime | None = None,
    ) -> list[IndexSyncItem]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        due_at = as_naive_utc(now or utc_now())
        statement = _item_statement().where(
            IndexSyncIntentRecord.status.in_(
                (IndexSyncStatus.PENDING.value, IndexSyncStatus.RETRY_WAIT.value)
            ),
            IndexSyncIntentRecord.available_at <= due_at,
        ).order_by(
            IndexSyncIntentRecord.available_at,
            IndexSyncIntentRecord.updated_at,
            IndexSyncIntentRecord.video_chunk_id,
        ).limit(limit)
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def list_all(self) -> list[IndexSyncItem]:
        statement = _item_statement().order_by(
            IndexSyncIntentRecord.video_chunk_id
        )
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def list_by_video_chunk_ids(
        self,
        video_chunk_ids: Sequence[str],
    ) -> list[IndexSyncItem]:
        chunk_ids = list(dict.fromkeys(video_chunk_ids))
        if not chunk_ids:
            return []
        statement = (
            _item_statement()
            .where(IndexSyncIntentRecord.video_chunk_id.in_(chunk_ids))
            .order_by(IndexSyncIntentRecord.video_chunk_id)
        )
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def mark_synced(
        self,
        video_chunk_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
    ) -> bool:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_chunk_id == video_chunk_id,
                    IndexSyncIntentRecord.source_revision == source_revision,
                    IndexSyncIntentRecord.operation == operation_value,
                    IndexSyncIntentRecord.generation == current_generation,
                )
                .values(
                    status=IndexSyncStatus.SYNCED.value,
                    available_at=now,
                    last_error=None,
                    synced_at=now,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                return False
            _refresh_chunk_series_status(session, video_chunk_id)
            return True

    def mark_failed(
        self,
        video_chunk_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
        *,
        error: str,
        available_at: datetime,
    ) -> bool:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        retry_at = as_naive_utc(available_at)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_chunk_id == video_chunk_id,
                    IndexSyncIntentRecord.source_revision == source_revision,
                    IndexSyncIntentRecord.operation == operation_value,
                    IndexSyncIntentRecord.generation == current_generation,
                )
                .values(
                    status=IndexSyncStatus.RETRY_WAIT.value,
                    attempt_count=IndexSyncIntentRecord.attempt_count + 1,
                    available_at=retry_at,
                    last_error=error,
                    synced_at=None,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                return False
            _refresh_chunk_series_status(session, video_chunk_id)
            return True

    def mark_pending_if_current(
        self,
        video_chunk_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
    ) -> int | None:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_chunk_id == video_chunk_id,
                    IndexSyncIntentRecord.source_revision == source_revision,
                    IndexSyncIntentRecord.operation == operation_value,
                    IndexSyncIntentRecord.generation == current_generation,
                )
                .values(
                    generation=IndexSyncIntentRecord.generation + 1,
                    status=IndexSyncStatus.PENDING.value,
                    attempt_count=0,
                    available_at=now,
                    last_error=None,
                    synced_at=None,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                return None
            _refresh_chunk_series_status(session, video_chunk_id)
            return current_generation + 1

    def invalidate_after_stale_result(
        self,
        video_chunk_id: str,
        completed_generation: int,
    ) -> int | None:
        """Requeue current state after an older external write may have won."""

        stale_generation = _validated_generation(completed_generation)
        now = utc_now_naive()
        with self._session_factory.begin() as session:
            intent = session.scalar(
                select(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_chunk_id == video_chunk_id
                )
                .with_for_update()
            )
            if intent is None or intent.generation < stale_generation:
                return None

            intent.generation += 1
            intent.status = IndexSyncStatus.PENDING.value
            intent.attempt_count = 0
            intent.available_at = now
            intent.last_error = None
            intent.synced_at = None
            intent.updated_at = now
            session.flush()
            _refresh_chunk_series_status(session, video_chunk_id)
            return intent.generation


def materialize_index_sync_intents(
    session: Session,
    video_chunk_ids: Sequence[str] | None = None,
) -> int:
    """Materialize current Qdrant intent inside the caller's transaction."""

    statement = select(
        VideoChunkRecord.video_chunk_id,
        VideoRecord.series_id,
        VideoChunkRecord.video_id,
        VideoChunkInfoRecord.video_chunk_id.label("info_video_chunk_id"),
        VideoChunkInfoRecord.is_fallback,
        VideoChunkEmbeddingRecord.text_vector,
        VideoChunkEmbeddingRecord.vision_vector,
    ).join(
        VideoRecord,
        VideoRecord.video_id == VideoChunkRecord.video_id,
    ).outerjoin(
        VideoChunkInfoRecord,
        VideoChunkInfoRecord.video_chunk_id
        == VideoChunkRecord.video_chunk_id,
    ).outerjoin(
        VideoChunkEmbeddingRecord,
        VideoChunkEmbeddingRecord.video_chunk_id
        == VideoChunkRecord.video_chunk_id,
    ).where(_video_is_not_pending_deletion())
    if video_chunk_ids is not None:
        chunk_ids = list(dict.fromkeys(video_chunk_ids))
        if not chunk_ids:
            return 0
        statement = statement.where(
            VideoChunkRecord.video_chunk_id.in_(chunk_ids)
        )
    # This must be a MySQL current/locking read. A plain consistent read under
    # REPEATABLE READ can observe older facts and then overwrite a newer intent
    # after waiting for that intent's row lock.
    statement = statement.order_by(
        VideoChunkRecord.video_chunk_id
    ).with_for_update()

    rows = session.execute(statement).all()
    row_chunk_ids = [row.video_chunk_id for row in rows]
    intents_by_chunk_id: dict[str, IndexSyncIntentRecord] = {}
    if row_chunk_ids:
        intents_by_chunk_id = {
            intent.video_chunk_id: intent
            for intent in session.scalars(
                select(IndexSyncIntentRecord)
                .where(IndexSyncIntentRecord.video_chunk_id.in_(row_chunk_ids))
                .order_by(IndexSyncIntentRecord.video_chunk_id)
                .with_for_update()
            ).all()
        }

    changed_count = 0
    now = utc_now_naive()
    affected_series_ids: set[str] = set()
    for row in rows:
        text_vector = _copy_vector(row.text_vector)
        vision_vector = _copy_vector(row.vision_vector)
        has_both_vectors = (
            row.info_video_chunk_id is not None
            and bool(text_vector)
            and bool(vision_vector)
        )
        operation = (
            IndexSyncOperation.UPSERT
            if has_both_vectors
            else IndexSyncOperation.DELETE
        )
        is_fallback = bool(row.is_fallback)
        source_revision = _compute_index_source_revision(
            series_id=row.series_id,
            video_id=row.video_id,
            video_chunk_id=row.video_chunk_id,
            operation=operation,
            is_fallback=is_fallback,
            text_vector=text_vector,
            vision_vector=vision_vector,
        )
        affected_series_ids.add(row.series_id)

        intent = intents_by_chunk_id.get(row.video_chunk_id)
        if intent is None:
            session.add(
                IndexSyncIntentRecord(
                    video_chunk_id=row.video_chunk_id,
                    operation=operation.value,
                    source_revision=source_revision,
                    generation=1,
                    status=IndexSyncStatus.PENDING.value,
                    attempt_count=0,
                    available_at=now,
                )
            )
            changed_count += 1
            continue
        if (
            intent.operation == operation.value
            and intent.source_revision == source_revision
        ):
            continue

        intent.operation = operation.value
        intent.source_revision = source_revision
        intent.generation += 1
        intent.status = IndexSyncStatus.PENDING.value
        intent.attempt_count = 0
        intent.available_at = now
        intent.last_error = None
        intent.synced_at = None
        changed_count += 1

    session.flush()
    refresh_series_statuses(session, affected_series_ids)
    return changed_count


def _compute_index_source_revision(
    *,
    series_id: str,
    video_id: str,
    video_chunk_id: str,
    operation: IndexSyncOperation | str,
    is_fallback: bool,
    text_vector: list[float] | None,
    vision_vector: list[float] | None,
) -> str:
    """Hash the complete desired point before adding the hash to its payload."""

    source = {
        "operation": IndexSyncOperation(operation).value,
        "pointId": video_chunk_id,
        "payload": {
            "seriesId": series_id,
            "videoId": video_id,
            "videoChunkId": video_chunk_id,
            "isFallback": is_fallback,
        },
        "vectors": {
            "text": text_vector,
            "vision": vision_vector,
        },
    }
    canonical = json.dumps(
        source,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item_statement() -> Select:
    return select(
        IndexSyncIntentRecord.video_chunk_id,
        VideoRecord.series_id,
        VideoChunkRecord.video_id,
        IndexSyncIntentRecord.operation,
        IndexSyncIntentRecord.source_revision,
        IndexSyncIntentRecord.generation,
        IndexSyncIntentRecord.status,
        IndexSyncIntentRecord.attempt_count,
        IndexSyncIntentRecord.available_at,
        IndexSyncIntentRecord.last_error,
        IndexSyncIntentRecord.synced_at,
        VideoChunkInfoRecord.is_fallback,
        VideoChunkEmbeddingRecord.text_vector,
        VideoChunkEmbeddingRecord.vision_vector,
    ).join(
        VideoChunkRecord,
        VideoChunkRecord.video_chunk_id
        == IndexSyncIntentRecord.video_chunk_id,
    ).join(
        VideoRecord,
        VideoRecord.video_id == VideoChunkRecord.video_id,
    ).outerjoin(
        VideoChunkInfoRecord,
        VideoChunkInfoRecord.video_chunk_id
        == VideoChunkRecord.video_chunk_id,
    ).outerjoin(
        VideoChunkEmbeddingRecord,
        VideoChunkEmbeddingRecord.video_chunk_id
        == VideoChunkRecord.video_chunk_id,
    ).where(_video_is_not_pending_deletion())


def _video_is_not_pending_deletion():
    return ~exists().where(
        UploadSessionRecord.video_id == VideoRecord.video_id,
        UploadSessionRecord.last_error_code
        == UPLOAD_DELETION_PENDING_ERROR_CODE,
    )


def _items_from_rows(rows) -> list[IndexSyncItem]:
    return [
        IndexSyncItem(
            video_chunk_id=row.video_chunk_id,
            series_id=row.series_id,
            video_id=row.video_id,
            operation=IndexSyncOperation(row.operation),
            source_revision=row.source_revision,
            generation=row.generation,
            status=IndexSyncStatus(row.status),
            attempt_count=row.attempt_count,
            available_at=row.available_at,
            last_error=row.last_error,
            synced_at=row.synced_at,
            is_fallback=bool(row.is_fallback),
            text_vector=_copy_vector(row.text_vector),
            vision_vector=_copy_vector(row.vision_vector),
        )
        for row in rows
    ]


def _refresh_chunk_series_status(
    session: Session,
    video_chunk_id: str,
) -> None:
    series_id = session.scalar(
        select(VideoRecord.series_id)
        .join(
            VideoChunkRecord,
            VideoChunkRecord.video_id == VideoRecord.video_id,
        )
        .where(VideoChunkRecord.video_chunk_id == video_chunk_id)
    )
    if series_id is not None:
        refresh_series_statuses(session, {series_id})


def refresh_series_statuses(
    session: Session,
    series_ids: set[str],
) -> None:
    """Recompute READY under a per-Series lock using current committed facts."""

    for series_id in series_ids:
        series = session.scalar(
            select(SeriesRecord)
            .where(SeriesRecord.series_id == series_id)
            .with_for_update()
        )
        if series is None:
            continue
        videos = session.execute(
            select(
                VideoRecord.parse_status,
                func.count(VideoChunkRecord.video_chunk_id).label("chunk_count"),
            )
            .outerjoin(
                VideoChunkRecord,
                VideoChunkRecord.video_id == VideoRecord.video_id,
            )
            .where(VideoRecord.series_id == series_id)
            .group_by(VideoRecord.video_id, VideoRecord.parse_status)
        ).all()
        rows = session.execute(
            select(
                VideoChunkEmbeddingRecord.text_vector,
                VideoChunkEmbeddingRecord.vision_vector,
                IndexSyncIntentRecord.operation,
                IndexSyncIntentRecord.status,
            )
            .select_from(VideoChunkRecord)
            .join(
                VideoRecord,
                VideoRecord.video_id == VideoChunkRecord.video_id,
            )
            .outerjoin(
                VideoChunkEmbeddingRecord,
                VideoChunkEmbeddingRecord.video_chunk_id
                == VideoChunkRecord.video_chunk_id,
            )
            .outerjoin(
                IndexSyncIntentRecord,
                IndexSyncIntentRecord.video_chunk_id
                == VideoChunkRecord.video_chunk_id,
            )
            .where(VideoRecord.series_id == series_id)
        ).all()
        all_videos_completed = bool(videos) and all(
            row.parse_status
            in {
                "COMPLETED",  # Transitional direct-import compatibility.
                ParseJobStatus.SUCCEEDED.value,
            }
            and row.chunk_count > 0
            for row in videos
        )
        is_ready = (
            all_videos_completed
            and bool(rows)
            and all(
                bool(row.text_vector)
                and bool(row.vision_vector)
                and row.operation == IndexSyncOperation.UPSERT.value
                and row.status == IndexSyncStatus.SYNCED.value
                for row in rows
            )
        )
        series.status = "READY" if is_ready else "INDEXING"


def _copy_vector(vector) -> list[float] | None:
    return list(vector) if vector is not None else None


def _validated_generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")
    return value
