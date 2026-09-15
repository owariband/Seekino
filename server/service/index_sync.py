from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from ..model.dto.indexSyncDTO import IndexReconcileReport, IndexSyncItem, IndexSyncRunReport
from ..model.index_sync import IndexSyncOperation, IndexSyncStatus
from ..repository.index_sync import IndexSyncRepository
from ..storage.qdrant import QdrantVideoChunkStore, VideoChunkIndexPoint
from ..utils.errors import exception_summary
from ..utils.time import utc_now


class IndexSyncInvariantError(RuntimeError):
    """A persisted UPSERT intent cannot form a complete index point."""


class IndexSyncService:
    """Converge the rebuildable Qdrant index to MySQL desired state."""

    def __init__(
        self,
        repository: IndexSyncRepository,
        store: QdrantVideoChunkStore,
        *,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be greater than zero")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError(
                "retry_max_seconds must be greater than or equal to "
                "retry_base_seconds"
            )
        self._repository = repository
        self._store = store
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._clock = clock or utc_now

    def sync_due(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> IndexSyncRunReport:
        """Synchronize currently due intents without coupling fact commits to Qdrant."""

        materialized_count = self._repository.materialize_all()
        # Materialization can create PENDING intents at the current instant.
        # Read the default cutoff afterwards so a single sync run can consume them.
        due_at = now or self._clock()
        items = self._repository.list_due(limit, now=due_at)
        synced, failed, stale = self._apply_items(items)
        return IndexSyncRunReport(
            materialized_count=materialized_count,
            selected_count=len(items),
            synced_count=synced,
            failed_count=failed,
            stale_result_count=stale,
        )

    def sync_video_chunks(
        self,
        video_chunk_ids: Sequence[str],
    ) -> IndexSyncRunReport:
        """Synchronize one completed ParseJob's exact VideoChunk set."""

        chunk_ids = list(dict.fromkeys(video_chunk_ids))
        if not chunk_ids:
            raise ValueError("video_chunk_ids must not be empty")
        materialized_count = self._repository.materialize(chunk_ids)
        items = self._repository.list_by_video_chunk_ids(chunk_ids)
        present = {item.video_chunk_id for item in items}
        missing = [item for item in chunk_ids if item not in present]
        if missing:
            raise IndexSyncInvariantError(
                "missing index intents for VideoChunks: " + ", ".join(missing)
            )
        selected = [
            item for item in items if item.status is not IndexSyncStatus.SYNCED
        ]
        synced, failed, stale = self._apply_items(selected)
        current = self._repository.list_by_video_chunk_ids(chunk_ids)
        if failed or stale or any(
            item.status is not IndexSyncStatus.SYNCED for item in current
        ):
            raise IndexSyncInvariantError(
                "the ParseJob's Qdrant index did not converge to SYNCED"
            )
        return IndexSyncRunReport(
            materialized_count=materialized_count,
            selected_count=len(selected),
            synced_count=synced,
            failed_count=0,
            stale_result_count=0,
        )

    def reconcile(
        self,
        *,
        inspection_batch_size: int = 256,
    ) -> IndexReconcileReport:
        """Inspect every desired point and immediately repair missing/stale state."""

        if inspection_batch_size <= 0:
            raise ValueError("inspection_batch_size must be greater than zero")
        materialized_count = self._repository.materialize_all()
        items = self._repository.list_all()
        consistent_count = 0
        repair_attempted_count = 0
        synced_count = 0
        failed_count = 0
        stale_result_count = 0

        for batch in _batches(items, inspection_batch_size):
            try:
                states = self._store.inspect(
                    [item.video_chunk_id for item in batch]
                )
            except Exception as exc:
                for item in batch:
                    if self._repository.mark_failed(
                        item.video_chunk_id,
                        item.source_revision,
                        item.operation,
                        item.generation,
                        error=exception_summary(exc),
                        available_at=(
                            self._clock()
                            + self._retry_delay(item.attempt_count)
                        ),
                    ):
                        failed_count += 1
                    else:
                        stale_result_count += 1
                continue
            for item in batch:
                state = states[item.video_chunk_id]
                is_consistent = (
                    item.operation is IndexSyncOperation.UPSERT
                    and state.present
                    and state.source_revision == item.source_revision
                ) or (
                    item.operation is IndexSyncOperation.DELETE
                    and not state.present
                )
                if is_consistent:
                    consistent_count += 1
                    if item.status is not IndexSyncStatus.SYNCED:
                        if self._repository.mark_synced(
                            item.video_chunk_id,
                            item.source_revision,
                            item.operation,
                            item.generation,
                        ):
                            synced_count += 1
                        else:
                            stale_result_count += 1
                    continue

                next_generation = self._repository.mark_pending_if_current(
                    item.video_chunk_id,
                    item.source_revision,
                    item.operation,
                    item.generation,
                )
                if next_generation is None:
                    stale_result_count += 1
                    continue
                repair_attempted_count += 1
                pending_item = replace(
                    item,
                    generation=next_generation,
                    status=IndexSyncStatus.PENDING,
                    attempt_count=0,
                    last_error=None,
                    synced_at=None,
                )
                synced, failed, stale = self._apply_items([pending_item])
                synced_count += synced
                failed_count += failed
                stale_result_count += stale

        return IndexReconcileReport(
            materialized_count=materialized_count,
            inspected_count=len(items),
            consistent_count=consistent_count,
            repair_attempted_count=repair_attempted_count,
            synced_count=synced_count,
            failed_count=failed_count,
            stale_result_count=stale_result_count,
        )

    def rebuild(self) -> IndexSyncRunReport:
        """Force every MySQL desired state back through the idempotent sync path."""

        materialized_count = self._repository.materialize_all()
        current_items = self._repository.list_all()
        selected_items: list[IndexSyncItem] = []
        stale_result_count = 0
        for item in current_items:
            next_generation = self._repository.mark_pending_if_current(
                item.video_chunk_id,
                item.source_revision,
                item.operation,
                item.generation,
            )
            if next_generation is not None:
                selected_items.append(
                    replace(
                        item,
                        generation=next_generation,
                        status=IndexSyncStatus.PENDING,
                        attempt_count=0,
                        last_error=None,
                        synced_at=None,
                    )
                )
            else:
                stale_result_count += 1

        synced, failed, stale = self._apply_items(selected_items)
        return IndexSyncRunReport(
            materialized_count=materialized_count,
            selected_count=len(selected_items),
            synced_count=synced,
            failed_count=failed,
            stale_result_count=stale_result_count + stale,
        )

    def _apply_items(
        self,
        items: Sequence[IndexSyncItem],
    ) -> tuple[int, int, int]:
        synced_count = 0
        failed_count = 0
        stale_result_count = 0
        for item in items:
            try:
                self._apply_item(item)
            except Exception as exc:
                retry_at = self._clock() + self._retry_delay(item.attempt_count)
                recorded = self._repository.mark_failed(
                    item.video_chunk_id,
                    item.source_revision,
                    item.operation,
                    item.generation,
                    error=exception_summary(exc),
                    available_at=retry_at,
                )
                if recorded:
                    failed_count += 1
                else:
                    stale_result_count += 1
                    self._repository.invalidate_after_stale_result(
                        item.video_chunk_id,
                        item.generation,
                    )
                continue

            if self._repository.mark_synced(
                item.video_chunk_id,
                item.source_revision,
                item.operation,
                item.generation,
            ):
                synced_count += 1
            else:
                stale_result_count += 1
                self._repository.invalidate_after_stale_result(
                    item.video_chunk_id,
                    item.generation,
                )
        return synced_count, failed_count, stale_result_count

    def _apply_item(self, item: IndexSyncItem) -> None:
        if item.operation is IndexSyncOperation.DELETE:
            self._store.delete(item.video_chunk_id)
            return
        if not item.text_vector or not item.vision_vector:
            raise IndexSyncInvariantError(
                "an UPSERT intent requires non-empty text and vision vectors"
            )
        self._store.upsert(
            VideoChunkIndexPoint(
                series_id=item.series_id,
                video_id=item.video_id,
                video_chunk_id=item.video_chunk_id,
                source_revision=item.source_revision,
                is_fallback=item.is_fallback,
                text_vector=item.text_vector,
                vision_vector=item.vision_vector,
            )
        )

    def _retry_delay(self, previous_attempt_count: int) -> timedelta:
        exponent = min(max(previous_attempt_count, 0), 30)
        seconds = min(
            self._retry_base_seconds * (2**exponent),
            self._retry_max_seconds,
        )
        return timedelta(seconds=seconds)


def _batches(
    items: Sequence[IndexSyncItem],
    batch_size: int,
) -> Iterator[Sequence[IndexSyncItem]]:
    for offset in range(0, len(items), batch_size):
        yield items[offset : offset + batch_size]
