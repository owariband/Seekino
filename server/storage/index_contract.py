"""Search adapter contracts shared by desktop and server hosts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import EventFilters, RawEventHit

from ..utils.validation import canonical_uuid


class IndexDocumentConsistencyError(RuntimeError):
    """A search document disagrees with its authorized source or publication."""


class IndexCleanupStore(Protocol):
    def bulk_delete(
        self, keys: Sequence[tuple[str, str]], *, refresh: Literal[False, "wait_for"] = False,
    ) -> object: ...

    def verify_absent(self, keys: Sequence[tuple[str, str]]) -> bool: ...


class ChunkIndexStore(IndexCleanupStore, Protocol):
    semantic_dimensions: int
    vision_dimensions: int


class EventSearchStore(Protocol):
    index_name: str
    semantic_dimensions: int
    vision_dimensions: int

    def bind_contract(self, contract: EventIndexingContract, *, create: bool = False) -> None: ...

    def search_lexical(
        self, query: str, *, sources: Sequence[tuple[str, str, int]], filters: EventFilters, limit: int,
    ) -> tuple[RawEventHit, ...]: ...

    def search_vector(
        self, query_vector: Sequence[float], *, channel: Literal["semantic", "vision"],
        sources: Sequence[tuple[str, str, int]], filters: EventFilters, limit: int, exact: bool = False,
    ) -> tuple[RawEventHit, ...]: ...


class IndexSchemaError(RuntimeError):
    """The configured physical index has an incompatible search contract."""


@dataclass(frozen=True, slots=True)
class StoredDocumentState:
    present: bool
    tenant_id: str | None = None
    series_id: str | None = None
    video_id: str | None = None
    parse_task_id: str | None = None
    video_chunk_id: str | None = None
    content_version: int | None = None
    index_generation: int | None = None
    lexical_fields_present: bool = False
    semantic_text_present: bool = False
    semantic_vector_present: bool = False
    vision_vector_present: bool = False
    semantic_vector: tuple[float, ...] | None = None
    vision_vector: tuple[float, ...] | None = None
    sparse_token_weights: tuple[tuple[str, float], ...] | None = None


def document_id(parse_task_id: str, video_chunk_id: str) -> str:
    """Keep one stable document per parse task and Chunk across content updates."""

    return (
        f"{canonical_uuid(parse_task_id, 'parse_task_id')}:"
        f"{canonical_uuid(video_chunk_id, 'video_chunk_id')}"
    )
