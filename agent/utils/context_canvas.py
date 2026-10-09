"""Execution-local canonical state shared by Agent business canvases."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from .errors import AgentToolFeedbackError
from .tool_output import bounded_json

CanvasData = dict[str, dict[str, Any]]
_T = TypeVar("_T")


def canvas_json(value: object) -> str:
    def encode(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", exclude_unset=True)
        raise TypeError(f"Unsupported Canvas value: {type(item).__name__}")

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode,
    )


class CanvasRevisionConflict(AgentToolFeedbackError):
    def __init__(self, expected: int, current: int) -> None:
        self.expected_revision = expected
        self.current_revision = current
        super().__init__(
            "STALE_REVISION", f"Expected Canvas revision {expected}, current is {current}.",
            "Read the current Canvas, review the affected entries, and retry with its revision.",
        )


@dataclass(frozen=True, slots=True)
class CanvasSnapshot:
    scope: str
    id: str
    revision: int
    collections: CanvasData
    readonly_collections: frozenset[str]
    readonly_entries: frozenset[tuple[str, str]]


class ContextCanvas:
    """Own state outside model messages; commit validated local drafts atomically."""

    def __init__(self, scope: str) -> None:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("Canvas scope must be a nonempty string")
        self.scope = scope
        self.id = uuid4().hex
        self._revision = 0
        self._data: CanvasData = {}
        self._readonly_collections: set[str] = set()
        self._readonly_entries: set[tuple[str, str]] = set()
        self._batches: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._lock = RLock()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def get(self, collection: str, reference: str) -> Any:
        with self._lock:
            try:
                return deepcopy(self._data[collection][reference])
            except KeyError as exc:
                raise AgentToolFeedbackError(
                    "UNKNOWN_REFERENCE", f"Unknown Canvas reference {reference!r} in {collection!r}.",
                    "Read the collection catalog and use an issued reference.",
                ) from exc

    def items(self, collection: str) -> list[tuple[str, Any]]:
        with self._lock:
            return deepcopy(list(self._data.get(collection, {}).items()))

    def put(self, collection: str, reference: str, value: Any, *, readonly: bool = False) -> None:
        def insert(draft: CanvasData) -> None:
            draft.setdefault(collection, {})[reference] = deepcopy(value)

        self.mutate(insert, readonly_entries=((collection, reference),) if readonly else ())

    def mutate(
        self,
        callback: Callable[[CanvasData], _T],
        *,
        readonly_collections: Iterable[str] = (),
        readonly_entries: Iterable[tuple[str, str]] = (),
    ) -> _T:
        """Host mutation may append evidence, while existing frozen values stay intact."""

        with self._lock:
            draft = deepcopy(self._data)
            result = callback(draft)
            self._check_readonly(draft, allow_additions=True)
            collections = set(readonly_collections)
            entries = set(readonly_entries)
            if any(name not in draft for name in collections) or any(
                reference not in draft.get(collection, {}) for collection, reference in entries
            ):
                raise ValueError("Readonly targets must exist in the committed draft")
            changed = draft != self._data
            frozen_changed = not collections <= self._readonly_collections or not entries <= self._readonly_entries
            if changed or frozen_changed:
                self._data = deepcopy(draft)
                self._readonly_collections.update(collections)
                self._readonly_entries.update(entries)
                self._revision += 1
            return deepcopy(result)

    def atomic_batch(
        self,
        batch_id: str,
        expected_revision: int,
        operations: Sequence[Any],
        apply: Callable[[CanvasData, Sequence[Any]], list[dict[str, Any]]],
        *,
        role: str,
        maximum_result_bytes: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id must be a nonempty string")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role must be bound by the host")
        fingerprint = sha256(canvas_json(list(operations)).encode("utf-8")).hexdigest()
        key = (role, batch_id)
        with self._lock:
            previous = self._batches.get(key)
            if previous is not None:
                old_fingerprint, receipt = previous
                if fingerprint != old_fingerprint:
                    raise AgentToolFeedbackError(
                        "BATCH_ID_REUSED", "This batch ID was already used with different operations.",
                        "Use a new batch ID after reviewing the current Canvas.",
                    )
                replay = {**deepcopy(receipt), "replayed": True}
                if maximum_result_bytes is not None:
                    bounded_json(replay, maximum_result_bytes)
                return replay
            if expected_revision != self._revision:
                raise CanvasRevisionConflict(expected_revision, self._revision)
            draft = deepcopy(self._data)
            results = apply(draft, deepcopy(list(operations)))
            if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                raise TypeError("Canvas batch application must return a list of result dictionaries")
            self._check_readonly(draft, allow_additions=False)
            next_revision = self._revision + int(draft != self._data)
            receipt = {
                "batch_id": batch_id, "revision": next_revision,
                "results": deepcopy(results), "replayed": False,
            }
            if maximum_result_bytes is not None:
                bounded_json(receipt, maximum_result_bytes)
            # Nothing becomes visible, including replay records, before all checks succeed.
            self._data = deepcopy(draft)
            self._revision = next_revision
            self._batches[key] = (fingerprint, deepcopy(receipt))
            return receipt

    def _check_readonly(self, draft: CanvasData, *, allow_additions: bool) -> None:
        for collection in self._readonly_collections:
            original = self._data.get(collection, {})
            candidate = draft.get(collection)
            if candidate is None or (
                not allow_additions and candidate != original
            ) or any(key not in candidate or candidate[key] != value for key, value in original.items()):
                raise AgentToolFeedbackError(
                    "READONLY_RESOURCE", f"Collection {collection!r} is read-only.",
                    "Keep evidence and approved snapshots unchanged; use an allowed draft operation.",
                )
        for collection, reference in self._readonly_entries:
            if reference not in draft.get(collection, {}) or (
                draft[collection][reference] != self._data[collection][reference]
            ):
                raise AgentToolFeedbackError(
                    "READONLY_RESOURCE", f"Reference {reference!r} is read-only.",
                    "Keep source evidence unchanged and edit only the allowed draft.",
                )

    def snapshot(self) -> CanvasSnapshot:
        with self._lock:
            return CanvasSnapshot(
                scope=self.scope, id=self.id, revision=self._revision,
                collections=deepcopy(self._data),
                readonly_collections=frozenset(self._readonly_collections),
                readonly_entries=frozenset(self._readonly_entries),
            )

    def checkpoint(self) -> dict[str, Any]:
        with self._lock:
            return {
                "canvas_id": self.id, "scope": self.scope, "revision": self._revision,
                "collections": {name: {"item_count": len(items)} for name, items in self._data.items()},
            }

    def catalog(self, collection: str, *, role: str) -> list[dict[str, Any]]:
        return [{"reference": reference} for reference, _value in self.items(collection)]

    def sections(self, collection: str, reference: str, *, role: str) -> dict[str, str]:
        return {"value": canvas_json(self.get(collection, reference))}
