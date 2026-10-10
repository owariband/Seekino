from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import Select, exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.indexSyncDTO import IndexSyncItem
from ..model.index_sync import (
    IndexSyncIntent,
    IndexSyncOperation,
    IndexSyncStatus,
)
from ..model.job import ParseJobStatus
from ..model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSession,
)
from ..model.video import (
    Series,
    Video,
    VideoChunkInfo,
    VideoChunkEmbedding,
    VideoChunk,
)
from ..utils.time import now_unix_time_ms


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
        now: int | None = None,
    ) -> list[IndexSyncItem]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        due_at = now if now is not None else now_unix_time_ms()
        statement = _item_statement().where(
            IndexSyncIntent.status.in_(
                (IndexSyncStatus.PENDING.value, IndexSyncStatus.RETRY_WAIT.value)
            ),
            IndexSyncIntent.next_execute_time <= due_at,
        ).order_by(
            IndexSyncIntent.next_execute_time,
            IndexSyncIntent.update_time,
            IndexSyncIntent.chunk_id,
        ).limit(limit)
        with self._session_factory() as session:
            return _items_from_rows(session.execute(statement).all())

    def list_all(self) -> list[IndexSyncItem]:
        statement = _item_statement().order_by(
            IndexSyncIntent.chunk_id
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
            .where(IndexSyncIntent.chunk_id.in_(chunk_ids))
            .order_by(IndexSyncIntent.chunk_id)
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
        now = now_unix_time_ms()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntent)
                .where(
                    IndexSyncIntent.chunk_id == video_chunk_id,
                    IndexSyncIntent.source_revision == source_revision,
                    IndexSyncIntent.operation == operation_value,
                    IndexSyncIntent.generation == current_generation,
                )
                .values(
                    status=IndexSyncStatus.SYNCED.value,
                    next_execute_time=now,
                    last_error=None,
                    sync_time=now,
                    update_time=now,
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
        available_at: int,
    ) -> bool:
        operation_value = IndexSyncOperation(operation).value
        current_generation = _validated_generation(generation)
        retry_at = available_at
        now = now_unix_time_ms()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntent)
                .where(
                    IndexSyncIntent.chunk_id == video_chunk_id,
                    IndexSyncIntent.source_revision == source_revision,
                    IndexSyncIntent.operation == operation_value,
                    IndexSyncIntent.generation == current_generation,
                )
                .values(
                    status=IndexSyncStatus.RETRY_WAIT.value,
                    attempt_count=IndexSyncIntent.attempt_count + 1,
                    next_execute_time=retry_at,
                    last_error=error,
                    sync_time=None,
                    update_time=now,
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
        now = now_unix_time_ms()
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(IndexSyncIntent)
                .where(
                    IndexSyncIntent.chunk_id == video_chunk_id,
                    IndexSyncIntent.source_revision == source_revision,
                    IndexSyncIntent.operation == operation_value,
                    IndexSyncIntent.generation == current_generation,
                )
                .values(
                    generation=IndexSyncIntent.generation + 1,
                    status=IndexSyncStatus.PENDING.value,
                    attempt_count=0,
                    next_execute_time=now,
                    last_error=None,
                    sync_time=None,
                    update_time=now,
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
        now = now_unix_time_ms()
        with self._session_factory.begin() as session:
            intent = session.scalar(
                select(IndexSyncIntent)
                .where(
                    IndexSyncIntent.chunk_id == video_chunk_id
                )
                .with_for_update()
            )
            if intent is None or intent.generation < stale_generation:
                return None

            intent.generation += 1
            intent.status = IndexSyncStatus.PENDING.value
            intent.attempt_count = 0
            intent.next_execute_time = now
            intent.last_error = None
            intent.sync_time = None
            intent.update_time = now
            session.flush()
            _refresh_chunk_series_status(session, video_chunk_id)
            return intent.generation


def materialize_index_sync_intents(
    session: Session,
    video_chunk_ids: Sequence[str] | None = None,
) -> int:
    """Materialize current Qdrant intent inside the caller's transaction."""

    statement = select(
        VideoChunk.id.label("video_chunk_id"),
        Video.series_id,
        VideoChunk.video_id,
        VideoChunkInfo.chunk_id.label("info_video_chunk_id"),
        VideoChunkInfo.is_fallback,
        VideoChunkEmbedding.text_vector,
        VideoChunkEmbedding.vision_vector,
    ).join(
        Video,
        Video.id == VideoChunk.video_id,
    ).outerjoin(
        VideoChunkInfo,
        VideoChunkInfo.chunk_id
        == VideoChunk.id,
    ).outerjoin(
        VideoChunkEmbedding,
        VideoChunkEmbedding.chunk_id
        == VideoChunk.id,
    ).where(_video_is_not_pending_deletion())
    if video_chunk_ids is not None:
        chunk_ids = list(dict.fromkeys(video_chunk_ids))
        if not chunk_ids:
            return 0
        statement = statement.where(
            VideoChunk.id.in_(chunk_ids)
        )
    # This must be a MySQL current/locking read. A plain consistent read under
    # REPEATABLE READ can observe older facts and then overwrite a newer intent
    # after waiting for that intent's row lock.
    statement = statement.order_by(
        VideoChunk.id
    ).with_for_update()

    rows = session.execute(statement).all()
    row_chunk_ids = [row.video_chunk_id for row in rows]
    intents_by_chunk_id: dict[str, IndexSyncIntent] = {}
    if row_chunk_ids:
        intents_by_chunk_id = {
            intent.chunk_id: intent
            for intent in session.scalars(
                select(IndexSyncIntent)
                .where(IndexSyncIntent.chunk_id.in_(row_chunk_ids))
                .order_by(IndexSyncIntent.chunk_id)
                .with_for_update()
            ).all()
        }

    changed_count = 0
    now = now_unix_time_ms()
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
                IndexSyncIntent(
                    chunk_id=row.video_chunk_id,
                    operation=operation.value,
                    source_revision=source_revision,
                    generation=1,
                    status=IndexSyncStatus.PENDING.value,
                    attempt_count=0,
                    next_execute_time=now,
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
        intent.next_execute_time = now
        intent.last_error = None
        intent.sync_time = None
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
        IndexSyncIntent.chunk_id.label("video_chunk_id"),
        Video.series_id,
        VideoChunk.video_id,
        IndexSyncIntent.operation,
        IndexSyncIntent.source_revision,
        IndexSyncIntent.generation,
        IndexSyncIntent.status,
        IndexSyncIntent.attempt_count,
        IndexSyncIntent.next_execute_time.label("available_at"),
        IndexSyncIntent.last_error,
        IndexSyncIntent.sync_time.label("synced_at"),
        VideoChunkInfo.is_fallback,
        VideoChunkEmbedding.text_vector,
        VideoChunkEmbedding.vision_vector,
    ).join(
        VideoChunk,
        VideoChunk.id
        == IndexSyncIntent.chunk_id,
    ).join(
        Video,
        Video.id == VideoChunk.video_id,
    ).outerjoin(
        VideoChunkInfo,
        VideoChunkInfo.chunk_id
        == VideoChunk.id,
    ).outerjoin(
        VideoChunkEmbedding,
        VideoChunkEmbedding.chunk_id
        == VideoChunk.id,
    ).where(_video_is_not_pending_deletion())


def _video_is_not_pending_deletion():
    return ~exists().where(
        UploadSession.video_id == Video.id,
        UploadSession.last_error_code
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
        select(Video.series_id)
        .join(
            VideoChunk,
            VideoChunk.video_id == Video.id,
        )
        .where(VideoChunk.id == video_chunk_id)
    )
    if series_id is not None:
        refresh_series_statuses(session, {series_id})


def list_ready_video_ids(session: Session, series_ids: Sequence[str]) -> list[str]:
    """Return videos whose parse and every indexed chunk are ready to search."""

    ids = list(dict.fromkeys(series_ids))
    if not ids:
        return []
    rows = session.execute(
        select(
            Video.id.label("video_id"),
            VideoChunk.id.label("video_chunk_id"),
            VideoChunkEmbedding.text_vector,
            VideoChunkEmbedding.vision_vector,
            IndexSyncIntent.operation,
            IndexSyncIntent.status,
        )
        .select_from(Video)
        .outerjoin(
            VideoChunk,
            VideoChunk.video_id == Video.id,
        )
        .outerjoin(
            VideoChunkEmbedding,
            VideoChunkEmbedding.chunk_id
            == VideoChunk.id,
        )
        .outerjoin(
            IndexSyncIntent,
            IndexSyncIntent.chunk_id
            == VideoChunk.id,
        )
        .where(
            Video.series_id.in_(ids),
            Video.parse_status.in_(
                ("COMPLETED", ParseJobStatus.SUCCEEDED.value)
            ),
        )
        .order_by(Video.id)
    ).all()
    ready: dict[str, bool] = {}
    for row in rows:
        chunk_ready = (
            row.video_chunk_id is not None
            and bool(row.text_vector)
            and bool(row.vision_vector)
            and row.operation == IndexSyncOperation.UPSERT.value
            and row.status == IndexSyncStatus.SYNCED.value
        )
        ready[row.video_id] = ready.get(row.video_id, True) and chunk_ready
    return [video_id for video_id, is_ready in ready.items() if is_ready]


def refresh_series_statuses(
    session: Session,
    series_ids: set[str],
) -> None:
    """Recompute READY under a per-Series lock using current committed facts."""

    for series_id in series_ids:
        series = session.scalar(
            select(Series)
            .where(Series.id == series_id)
            .with_for_update()
        )
        if series is None:
            continue
        series.status = (
            "READY" if list_ready_video_ids(session, [series_id]) else "INDEXING"
        )


def _copy_vector(vector) -> list[float] | None:
    return list(vector) if vector is not None else None


def _validated_generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")
    return value
