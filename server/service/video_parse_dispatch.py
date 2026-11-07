"""Process-local dispatch for a fixed episode topology. MySQL grants every lease."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic

from sqlalchemy.orm import Session, sessionmaker

from agent.video_parse_v2.scheduler import batch_open_threshold, chunk_index_ready
from agent.video_parse_v2.state import ChunkTaskPhase
from ..constants.parse import RUNTIME_DETAIL_KEY
from ..model.dto.taskDTO import (
    ChunkParseLease, ChunkScheduleEntryState, ChunkScheduleState, IntervalParseLease, IntervalParseTaskData,
    VideoParseLease, VideoParseTaskData,
)
from ..model.task import interval_full_ready_mask, interval_ready_mask_bit
from ..repository import video_parse_chunk as chunk_store, video_parse_interval as interval_store
from ..repository import video_parse_task as task_store
from .video_parse_task import _topology_complete

_logger = logging.getLogger(__name__)


class EpisodeReadyTasks:
    def __init__(
        self, parent: VideoParseTaskData, chunks: list[chunk_store.ChunkProgress],
        intervals: list[IntervalParseTaskData], submitted: set[str],
    ) -> None:
        self.parent_owner = parent.lease_owner
        self.parent_lease = parent.lease_version
        self.parent_status = parent.status
        self.wait_seconds = max(0.05, min(2.0, parent.parse_config["dispatch_interval_seconds"] or 2.0))
        self.scoring = parent.parse_config.get("voiceprint_mode", "off") != "off"
        self.capacity = parent.parse_config["execution"]
        self.running = {"parse": set(), "refine": set(), "index": set()}
        self.batch_size = parent.parse_config["batch_max_size"]
        self.chunks = {row.id: row for row in chunks}
        self.windows = {row.id: row for row in intervals}
        self.members = defaultdict(list)
        self.ready = {"parse": {}, "refine": {}, "index": {}}
        self.batch_counts = [0] * ((len(chunks) + self.batch_size - 1) // self.batch_size)
        self.open_batches = 1
        self.pending_windows = set()
        self.pending_indexes = set()
        self.failed = bool((parent.detail or {}).get(RUNTIME_DETAIL_KEY, {}).get("failure_status"))
        for chunk in chunks:
            if chunk.has_content or self.scoring and chunk.phase == ChunkTaskPhase.PARSE and chunk.status == "FAILED":
                self.batch_counts[chunk.sequence_no // self.batch_size] += 1
            if chunk.status == "QUEUED" and chunk.phase == ChunkTaskPhase.PARSE and not chunk.has_content:
                self.ready["parse"][chunk.id] = chunk.next_execute_time
            if chunk.status == "RUNNING":
                stage = "parse" if chunk.phase == ChunkTaskPhase.PARSE else "index"
                self.running[stage].add(chunk.id)
            if not (chunk.phase == ChunkTaskPhase.INDEX and chunk.status == "COMPLETE"
                    and chunk.has_content and chunk.indexed_content_version == chunk.content_version):
                self.pending_indexes.add(chunk.id)
            if chunk.status == "FAILED" and (not self.scoring or chunk.phase != ChunkTaskPhase.PARSE):
                self.failed = True
        self.open_batches_if_ready()
        for window in intervals:
            for offset, member in enumerate(window.chunk_task_ids):
                self.members[member].append((window, interval_ready_mask_bit(offset, window.chunk_count)))
            if window.status != "COMPLETE" or window.id not in submitted:
                self.pending_windows.add(window.id)
            if window.status == "REFINING":
                self.running["refine"].add(window.id)
            if window.status == "PARSING_FAILED":
                self.failed = True
            if window.status == "WAITING" and window.ready_mask == interval_full_ready_mask(window.chunk_count):
                self.ready["refine"][window.id] = window.next_execute_time

        for chunk in chunks:
            if chunk.phase == ChunkTaskPhase.INDEX and chunk.status == "QUEUED" and chunk_index_ready(
                chunk.has_content, [w.id not in self.pending_windows for w, _ in self.members[chunk.id]],
            ):
                self.ready["index"][chunk.id] = chunk.next_execute_time

    def open_batches_if_ready(self) -> None:
        while self.open_batches < len(self.batch_counts):
            if self.batch_counts[self.open_batches - 1] < batch_open_threshold(self.batch_size):
                break
            self.open_batches += 1

    def candidates(self, stage: str, now: datetime) -> tuple[str, ...]:
        if len(self.running[stage]) >= self.capacity[stage + "_concurrency"]:
            return ()
        due = (key for key, when in self.ready[stage].items() if when <= now)
        if stage == "refine":
            return tuple(sorted(due, key=lambda key: self.windows[key].start_sequence_no))
        if stage == "parse":
            due = (key for key in due if self.chunks[key].sequence_no < self.open_batches * self.batch_size)
        return tuple(sorted(due, key=lambda key: self.chunks[key].sequence_no))

    def understood(self, lease: ChunkParseLease, when: datetime, touched: tuple[str, ...]) -> None:
        key = lease.chunk_parse_task_id
        chunk = self.chunks.get(key)
        if chunk is None or chunk.has_content or chunk.lease_version > lease.lease_version:
            return
        if chunk.phase == ChunkTaskPhase.PARSE:
            self.chunks[key] = chunk._replace(has_content=True, phase=ChunkTaskPhase.REFINE,
                                              status="WAITING", lease_version=lease.lease_version)
        else:
            self.chunks[key] = chunk._replace(has_content=True)
        self.ready["parse"].pop(key, None)
        self.running["parse"].discard(key)
        if not (self.scoring and chunk.phase == ChunkTaskPhase.PARSE and chunk.status == "FAILED"):
            self.batch_counts[chunk.sequence_no // self.batch_size] += 1
        self.open_batches_if_ready()
        for window, bit in self.members[key]:
            if window.id not in touched:
                continue
            before = window.ready_mask
            window.ready_mask |= bit
            if (window.status == "WAITING" and before != window.ready_mask
                    and window.ready_mask == interval_full_ready_mask(window.chunk_count)):
                self.ready["refine"][window.id] = when

    def refined(self, lease: IntervalParseLease, index_ids: tuple[str, ...], when: datetime) -> None:
        window = self.windows.get(lease.interval_parse_task_id)
        if window is None or window.lease_version > lease.lease_version or window.status == "COMPLETE":
            return
        window.status = "COMPLETE"
        window.lease_version = lease.lease_version
        self.pending_windows.discard(window.id)
        self.ready["refine"].pop(window.id, None)
        self.running["refine"].discard(window.id)
        for key in index_ids:
            chunk = self.chunks.get(key)
            if chunk is not None and chunk.phase != ChunkTaskPhase.INDEX:
                self.chunks[key] = chunk._replace(phase=ChunkTaskPhase.INDEX, status="QUEUED")
                self.ready["index"][key] = when

    def lease_state(self, lease: ChunkParseLease | IntervalParseLease, *, retry_at: datetime | None = None,
                    indexed_version: int | None = None) -> None:
        if isinstance(lease, ChunkParseLease):
            key = lease.chunk_parse_task_id
            stage = "parse" if lease.phase == ChunkTaskPhase.PARSE else "index"
            chunk = self.chunks.get(key)
            if chunk is None or chunk.phase != lease.phase or chunk.lease_version > lease.lease_version:
                return
            if chunk.lease_version == lease.lease_version and (
                chunk.status == "COMPLETE" or chunk.status == "QUEUED" and retry_at is None and indexed_version is None
            ):
                return
            status = "RUNNING"
            if indexed_version is not None:
                self.pending_indexes.discard(key)
                chunk = chunk._replace(has_content=True, content_version=indexed_version, indexed_content_version=indexed_version)
                status = "COMPLETE"
            elif retry_at is not None:
                status = "QUEUED"
            self.chunks[key] = chunk._replace(status=status, lease_version=lease.lease_version)
        else:
            key = lease.interval_parse_task_id
            stage = "refine"
            window = self.windows.get(key)
            if window is None or window.status == "COMPLETE" or window.lease_version > lease.lease_version:
                return
            if window.lease_version == lease.lease_version and window.status == "WAITING" and retry_at is None:
                return
            window.lease_version = lease.lease_version
            window.status = "WAITING" if retry_at is not None else "REFINING"
        if retry_at is None and indexed_version is None:
            self.running[stage].add(key)
        else:
            self.running[stage].discard(key)
        if retry_at is None:
            self.ready[stage].pop(key, None)
        else:
            self.ready[stage][key] = retry_at


class ParseDispatch:
    """Snapshots and post-commit changes are ordered under the same process lock."""

    def __init__(self, session_factory: sessionmaker[Session], *, reconcile_interval: timedelta):
        self._factory = session_factory
        self._interval = reconcile_interval.total_seconds()
        self._episodes: dict[str, tuple[float, EpisodeReadyTasks]] = {}
        self._lock = Lock()
        self.wake: Callable[[], None] | None = None

    def _episode(self, task_id: str) -> EpisodeReadyTasks | None:
        entry = self._episodes.get(task_id)
        if entry is not None and monotonic() < entry[0]:
            return entry[1]
        with self._factory() as session:
            parent = task_store.get_task(session, task_id)
            if parent is None or parent.is_deleted or parent.status not in ("PARSING", "INDEXING"):
                self._episodes.pop(task_id, None)
                return None
            chunks = chunk_store.list_progress(session, task_id)
            intervals = interval_store.list_tasks(session, task_id)
            topology = [(c.id, c.video_id, c.sequence_no) for c in chunks]
            if not _topology_complete(parent, topology, intervals):
                self._episodes.pop(task_id, None)
                return None
            submitted = interval_store.list_content_ids(session, [window.id for window in intervals])
            episode = EpisodeReadyTasks(parent, chunks, intervals, submitted)
        self._episodes[task_id] = (monotonic() + self._interval, episode)
        return episode

    def candidates(self, task_id: str, stage: str, now: datetime) -> tuple[int | None, tuple[str, ...]]:
        with self._lock:
            episode = self._episode(task_id)
            return (episode.parent_lease, episode.candidates(stage, now)) if episode is not None else (None, ())

    def schedule(self, task_id: str) -> ChunkScheduleState | None:
        with self._lock:
            episode = self._episode(task_id)
            if episode is None:
                return None
            rows = tuple(ChunkScheduleEntryState(c.id, c.sequence_no, c.status, c.phase, c.has_content)
                         for c in episode.chunks.values())
            return ChunkScheduleState(rows, episode.wait_seconds, "a Chunk stage exhausted its retry budget" if episode.failed else None)

    def active_status(self, lease: VideoParseLease) -> str | None:
        with self._lock:
            episode = self._episode(lease.video_parse_task_id)
            if (episode is None or episode.parent_owner != lease.worker_id
                    or episode.parent_lease != lease.lease_version or episode.failed):
                return None
            if episode.parent_status == "PARSING" and episode.pending_windows:
                return "PARSING"
            if episode.parent_status == "INDEXING" and episode.pending_indexes:
                return "INDEXING"
            return None

    def after_commit(self, lease, change, *args, **kwargs) -> None:
        with self._lock:
            entry = self._episodes.get(lease.video_parse_task_id)
            if entry is not None:
                try:
                    change(entry[1], lease, *args, **kwargs)
                except Exception:
                    # The transaction already committed. Reload the next time
                    # this episode is scheduled instead of failing its worker.
                    self._episodes.pop(lease.video_parse_task_id, None)
                    _logger.exception("Dropping stale dispatch state for %s", lease.video_parse_task_id)
        self._notify()

    def refresh_indexes(self, task_id: str, reset_ids: tuple[str, ...]) -> None:
        # Rebuild and Prefix revoke an old INDEX lease. Read the committed
        # version for exactly those keys before accepting late completions.
        with self._lock:
            entry = self._episodes.get(task_id)
            if entry is not None:
                episode = entry[1]
                try:
                    with self._factory() as session:
                        rows = chunk_store.list_progress(session, task_id, task_ids=reset_ids)
                    for row in rows:
                        episode.chunks[row.id] = row
                        episode.ready["index"].pop(row.id, None)
                        episode.running["index"].discard(row.id)
                        if row.status == "RUNNING":
                            episode.running["index"].add(row.id)
                        if row.status == "QUEUED":
                            episode.ready["index"][row.id] = row.next_execute_time
                        if row.status == "COMPLETE" and row.content_version == row.indexed_content_version:
                            episode.pending_indexes.discard(row.id)
                        else:
                            episode.pending_indexes.add(row.id)
                except Exception:
                    self._episodes.pop(task_id, None)
                    _logger.exception("Could not refresh requeued indexes for %s", task_id)
        self._notify()

    def invalidate(self, task_id: str) -> None:
        with self._lock:
            self._episodes.pop(task_id, None)
        self._notify()

    def _notify(self) -> None:
        wake = self.wake
        if wake is not None:
            wake()

    def retain(self, task_ids: list[str]) -> None:
        with self._lock:
            for key in self._episodes.keys() - set(task_ids):
                del self._episodes[key]

    def next_delay(self, now: datetime, maximum: float) -> float:
        with self._lock:
            due = ((when - now).total_seconds() for _, episode in self._episodes.values()
                   for items in episode.ready.values() for when in items.values() if when > now)
            return min(maximum, min(due, default=maximum))
