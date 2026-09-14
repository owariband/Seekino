from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..model.index_sync import (
    IndexSyncIntentRecord,
    IndexSyncOperation,
    IndexSyncStatus,
)
from ..model.video import (
    SeriesRecord,
    VideoRecord,
    VideoSegmentInfoRecord,
    VideoSegmentParseChunkRecord,
    VideoSegmentRecord,
)


@dataclass(frozen=True)
class IndexSyncItem:
    video_segment_id: str
    series_id: str
    video_id: str
    operation: IndexSyncOperation
    source_revision: str
    generation: int
    status: IndexSyncStatus
    attempt_count: int
    available_at: datetime
    last_error: str | None
    synced_at: datetime | None
    is_fallback: bool
    text_vector: list[float] | None
    vision_vector: list[float] | None

    @property
    def payload(self) -> dict[str, str | bool]:
        return {
            "seriesId": self.series_id,
            "videoId": self.video_id,
            "videoSegmentId": self.video_segment_id,
            "sourceRevision": self.source_revision,
            "isFallback": self.is_fallback,
        }


class IndexSyncRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def materialize_all(self) -> int:
        """Create or update durable desired-state intents from MySQL facts."""

        with self._session_factory.begin() as session:
            return materialize_index_sync_intents(session)

    def list_due(
        self,
        limit: int,
        *,
        now: datetime | None = None,
    ) -> list[IndexSyncItem]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        due_at = _as_naive_utc(now or datetime.now(UTC))
        statement = _item_statement().where(
            IndexSyncIntentRecord.status.in_(
                (IndexSyncStatus.PENDING.value, IndexSyncStatus.RETRY_WAIT.value)
            ),
            IndexSyncIntentRecord.available_at <= due_at,
        ).order_by(
            IndexSyncIntentRecord.available_at,
            IndexSyncIntentRecord.updated_at,
            IndexSyncIntentRecord.video_segment_id,
        ).limit(limit)
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def list_all(self) -> list[IndexSyncItem]:
        statement = _item_statement().order_by(
            IndexSyncIntentRecord.video_segment_id
        )
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def mark_synced(
        self,
        video_segment_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
    ) -> bool:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        now = _utc_now()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_segment_id == video_segment_id,
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
            _refresh_segment_series_status(session, video_segment_id)
            return True

    def mark_failed(
        self,
        video_segment_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
        *,
        error: str,
        available_at: datetime,
    ) -> bool:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        retry_at = _as_naive_utc(available_at)
        now = _utc_now()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_segment_id == video_segment_id,
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
            _refresh_segment_series_status(session, video_segment_id)
            return True

    def mark_pending_if_current(
        self,
        video_segment_id: str,
        source_revision: str,
        operation: IndexSyncOperation | str,
        generation: int,
    ) -> int | None:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        now = _utc_now()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_segment_id == video_segment_id,
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
            _refresh_segment_series_status(session, video_segment_id)
            return current_generation + 1

    def invalidate_after_stale_result(
        self,
        video_segment_id: str,
        completed_generation: int,
    ) -> int | None:
        """Requeue current state after an older external write may have won."""

        stale_generation = _validated_generation(completed_generation)
        now = _utc_now()
        with self._session_factory.begin() as session:
            intent = session.scalar(
                select(IndexSyncIntentRecord)
                .where(
                    IndexSyncIntentRecord.video_segment_id == video_segment_id
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
            _refresh_segment_series_status(session, video_segment_id)
            return intent.generation


def materialize_index_sync_intents(
    session: Session,
    video_segment_ids: Sequence[str] | None = None,
) -> int:
    """Materialize current Qdrant intent inside the caller's transaction."""

    statement = select(
        VideoSegmentRecord.video_segment_id,
        VideoRecord.series_id,
        VideoSegmentRecord.video_id,
        VideoSegmentInfoRecord.video_segment_id.label("info_video_segment_id"),
        VideoSegmentInfoRecord.is_fallback,
        VideoSegmentParseChunkRecord.text_chunk,
        VideoSegmentParseChunkRecord.vision_chunk,
    ).join(
        VideoRecord,
        VideoRecord.video_id == VideoSegmentRecord.video_id,
    ).outerjoin(
        VideoSegmentInfoRecord,
        VideoSegmentInfoRecord.video_segment_id
        == VideoSegmentRecord.video_segment_id,
    ).outerjoin(
        VideoSegmentParseChunkRecord,
        VideoSegmentParseChunkRecord.video_segment_id
        == VideoSegmentRecord.video_segment_id,
    )
    if video_segment_ids is not None:
        segment_ids = list(dict.fromkeys(video_segment_ids))
        if not segment_ids:
            return 0
        statement = statement.where(
            VideoSegmentRecord.video_segment_id.in_(segment_ids)
        )
    # This must be a MySQL current/locking read. A plain consistent read under
    # REPEATABLE READ can observe older facts and then overwrite a newer intent
    # after waiting for that intent's row lock.
    statement = statement.order_by(
        VideoSegmentRecord.video_segment_id
    ).with_for_update()

    rows = session.execute(statement).all()
    row_segment_ids = [row.video_segment_id for row in rows]
    intents_by_segment_id: dict[str, IndexSyncIntentRecord] = {}
    if row_segment_ids:
        intents_by_segment_id = {
            intent.video_segment_id: intent
            for intent in session.scalars(
                select(IndexSyncIntentRecord)
                .where(IndexSyncIntentRecord.video_segment_id.in_(row_segment_ids))
                .order_by(IndexSyncIntentRecord.video_segment_id)
                .with_for_update()
            ).all()
        }

    changed_count = 0
    now = _utc_now()
    affected_series_ids: set[str] = set()
    for row in rows:
        text_vector = _copy_vector(row.text_chunk)
        vision_vector = _copy_vector(row.vision_chunk)
        has_both_vectors = (
            row.info_video_segment_id is not None
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
            video_segment_id=row.video_segment_id,
            operation=operation,
            is_fallback=is_fallback,
            text_vector=text_vector,
            vision_vector=vision_vector,
        )
        affected_series_ids.add(row.series_id)

        intent = intents_by_segment_id.get(row.video_segment_id)
        if intent is None:
            session.add(
                IndexSyncIntentRecord(
                    video_segment_id=row.video_segment_id,
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
    _refresh_series_statuses(session, affected_series_ids)
    return changed_count


def _compute_index_source_revision(
    *,
    series_id: str,
    video_id: str,
    video_segment_id: str,
    operation: IndexSyncOperation | str,
    is_fallback: bool,
    text_vector: list[float] | None,
    vision_vector: list[float] | None,
) -> str:
    """Hash the complete desired point before adding the hash to its payload."""

    source = {
        "operation": IndexSyncOperation(operation).value,
        "pointId": video_segment_id,
        "payload": {
            "seriesId": series_id,
            "videoId": video_id,
            "videoSegmentId": video_segment_id,
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
        IndexSyncIntentRecord.video_segment_id,
        VideoRecord.series_id,
        VideoSegmentRecord.video_id,
        IndexSyncIntentRecord.operation,
        IndexSyncIntentRecord.source_revision,
        IndexSyncIntentRecord.generation,
        IndexSyncIntentRecord.status,
        IndexSyncIntentRecord.attempt_count,
        IndexSyncIntentRecord.available_at,
        IndexSyncIntentRecord.last_error,
        IndexSyncIntentRecord.synced_at,
        VideoSegmentInfoRecord.is_fallback,
        VideoSegmentParseChunkRecord.text_chunk,
        VideoSegmentParseChunkRecord.vision_chunk,
    ).join(
        VideoSegmentRecord,
        VideoSegmentRecord.video_segment_id
        == IndexSyncIntentRecord.video_segment_id,
    ).join(
        VideoRecord,
        VideoRecord.video_id == VideoSegmentRecord.video_id,
    ).outerjoin(
        VideoSegmentInfoRecord,
        VideoSegmentInfoRecord.video_segment_id
        == VideoSegmentRecord.video_segment_id,
    ).outerjoin(
        VideoSegmentParseChunkRecord,
        VideoSegmentParseChunkRecord.video_segment_id
        == VideoSegmentRecord.video_segment_id,
    )


def _items_from_rows(rows) -> list[IndexSyncItem]:
    return [
        IndexSyncItem(
            video_segment_id=row.video_segment_id,
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
            text_vector=_copy_vector(row.text_chunk),
            vision_vector=_copy_vector(row.vision_chunk),
        )
        for row in rows
    ]


def _refresh_segment_series_status(
    session: Session,
    video_segment_id: str,
) -> None:
    series_id = session.scalar(
        select(VideoRecord.series_id)
        .join(
            VideoSegmentRecord,
            VideoSegmentRecord.video_id == VideoRecord.video_id,
        )
        .where(VideoSegmentRecord.video_segment_id == video_segment_id)
    )
    if series_id is not None:
        _refresh_series_statuses(session, {series_id})


def _refresh_series_statuses(
    session: Session,
    series_ids: set[str],
) -> None:
    for series_id in series_ids:
        rows = session.execute(
            select(
                VideoSegmentParseChunkRecord.text_chunk,
                VideoSegmentParseChunkRecord.vision_chunk,
                IndexSyncIntentRecord.operation,
                IndexSyncIntentRecord.status,
            )
            .select_from(VideoSegmentRecord)
            .join(
                VideoRecord,
                VideoRecord.video_id == VideoSegmentRecord.video_id,
            )
            .outerjoin(
                VideoSegmentParseChunkRecord,
                VideoSegmentParseChunkRecord.video_segment_id
                == VideoSegmentRecord.video_segment_id,
            )
            .outerjoin(
                IndexSyncIntentRecord,
                IndexSyncIntentRecord.video_segment_id
                == VideoSegmentRecord.video_segment_id,
            )
            .where(VideoRecord.series_id == series_id)
        ).all()
        is_ready = bool(rows) and all(
            bool(row.text_chunk)
            and bool(row.vision_chunk)
            and row.operation == IndexSyncOperation.UPSERT.value
            and row.status == IndexSyncStatus.SYNCED.value
            for row in rows
        )
        series = session.get(SeriesRecord, series_id)
        if series is not None:
            series.status = "READY" if is_ready else "INDEXING"


def _copy_vector(vector) -> list[float] | None:
    return list(vector) if vector is not None else None


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _validated_generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")
    return value
