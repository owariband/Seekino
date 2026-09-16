from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock, RLock
from typing import Any

from sqlalchemy import Engine, text

from ..database import DatabaseEnvironment

_logger = logging.getLogger(__name__)
_local_locks_guard = Lock()
_local_locks: dict[str, Any] = {}


class QdrantMutationLockBusyError(RuntimeError):
    """Another process is changing the same Qdrant collection."""


class QdrantMutationLock:
    """Serialize MySQL-backed writes to one environment's Qdrant collection."""

    def __init__(
        self,
        engine: Engine,
        *,
        environment: DatabaseEnvironment | str,
        collection_name: str,
        acquire_timeout_seconds: int = 5,
    ) -> None:
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise ValueError("collection_name must not be empty")
        if isinstance(acquire_timeout_seconds, bool) or not isinstance(
            acquire_timeout_seconds, int
        ):
            raise ValueError("acquire_timeout_seconds must be an integer")
        if acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds must not be negative")

        resolved_environment = DatabaseEnvironment(environment)
        collection_digest = hashlib.sha256(
            collection_name.encode("utf-8")
        ).hexdigest()[:32]
        self._engine = engine
        self._lock_name = (
            f"anivag:qdrant:{resolved_environment.value}:{collection_digest}"
        )
        self._acquire_timeout_seconds = acquire_timeout_seconds

    @property
    def lock_name(self) -> str:
        return self._lock_name

    @contextmanager
    def hold(self) -> Iterator[None]:
        dialect = self._engine.dialect.name
        if dialect == "sqlite":
            with _local_locks_guard:
                lock = _local_locks.setdefault(self._lock_name, RLock())
            acquired = lock.acquire(timeout=self._acquire_timeout_seconds)
            if not acquired:
                raise self._busy_error()
            try:
                yield
            finally:
                lock.release()
            return

        if dialect != "mysql":
            raise RuntimeError(
                "Qdrant mutation locking requires a MySQL or test SQLite engine"
            )

        with self._engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
                {
                    "lock_name": self._lock_name,
                    "timeout_seconds": self._acquire_timeout_seconds,
                },
            )
            if acquired != 1:
                raise self._busy_error()
            try:
                yield
            finally:
                try:
                    released = connection.scalar(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": self._lock_name},
                    )
                    if released != 1:
                        _logger.error(
                            "qdrant_mutation_lock_release_failed lock_name=%s result=%s",
                            self._lock_name,
                            released,
                        )
                except Exception:
                    # Closing the owning MySQL connection also releases GET_LOCK.
                    _logger.exception(
                        "qdrant_mutation_lock_release_failed lock_name=%s",
                        self._lock_name,
                    )

    def _busy_error(self) -> QdrantMutationLockBusyError:
        return QdrantMutationLockBusyError(
            "the Qdrant collection is being updated; retry this operation"
        )
