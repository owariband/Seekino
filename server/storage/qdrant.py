from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from qdrant_client import QdrantClient, models

from ..database import DatabaseEnvironment
from ..utils.validation import require_nonempty_text

TEXT_VECTOR_NAME = "text"
VISION_VECTOR_NAME = "vision"
SERIES_ID_PAYLOAD_KEY = "seriesId"
VIDEO_ID_PAYLOAD_KEY = "videoId"
VIDEO_CHUNK_ID_PAYLOAD_KEY = "videoChunkId"
SOURCE_REVISION_PAYLOAD_KEY = "sourceRevision"
IS_FALLBACK_PAYLOAD_KEY = "isFallback"
_POINT_PAYLOAD_KEYS = frozenset(
    {
        SERIES_ID_PAYLOAD_KEY,
        VIDEO_ID_PAYLOAD_KEY,
        VIDEO_CHUNK_ID_PAYLOAD_KEY,
        SOURCE_REVISION_PAYLOAD_KEY,
        IS_FALLBACK_PAYLOAD_KEY,
    }
)


class QdrantCollectionSchemaError(RuntimeError):
    """The configured collection cannot store anivag VideoChunk vectors."""


class QdrantCollectionNotFoundError(RuntimeError):
    """The configured derived index does not exist."""


class QdrantPointConsistencyError(RuntimeError):
    """A Qdrant result violates the formal VideoChunk index contract."""


@dataclass(frozen=True, slots=True)
class VideoChunkIndexPoint:
    series_id: str
    video_id: str
    video_chunk_id: str
    source_revision: str
    is_fallback: bool
    text_vector: Sequence[float]
    vision_vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class StoredPointState:
    present: bool
    source_revision: str | None


@dataclass(frozen=True, slots=True)
class VideoChunkIndexMatch:
    rank: int
    score: float
    series_id: str
    video_id: str
    video_chunk_id: str
    source_revision: str
    is_fallback: bool


class QdrantVideoChunkStore:
    """Persist the rebuildable VideoChunk index owned by the Server layer."""

    def __init__(
        self,
        client: QdrantClient,
        *,
        collection_name: str,
        environment: DatabaseEnvironment | str,
        text_dimensions: int,
        vision_dimensions: int,
    ) -> None:
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise ValueError("collection_name must not be empty")
        if collection_name != collection_name.strip():
            raise ValueError("collection_name must not contain surrounding whitespace")
        _validate_dimensions(text_dimensions, vision_dimensions)

        self._client = client
        self.collection_name = collection_name
        self.environment = DatabaseEnvironment(environment)
        self.text_dimensions = text_dimensions
        self.vision_dimensions = vision_dimensions
        self._validate_collection_environment()

    def ensure_collection(self) -> None:
        expected = {
            TEXT_VECTOR_NAME: models.VectorParams(
                size=self.text_dimensions,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT32,
            ),
            VISION_VECTOR_NAME: models.VectorParams(
                size=self.vision_dimensions,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT32,
            ),
        }
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=expected,
            )

        actual = self._client.get_collection(
            self.collection_name
        ).config.params.vectors
        self._validate_vector_schema(actual, expected)

        # Local Qdrant intentionally treats payload indexes as a no-op and emits
        # this warning. The same call creates the real index against Qdrant Server.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Payload indexes have no effect in the local Qdrant.*",
                category=UserWarning,
            )
            self._client.create_payload_index(
                collection_name=self.collection_name,
                field_name=SERIES_ID_PAYLOAD_KEY,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )

    def upsert(self, point: VideoChunkIndexPoint) -> None:
        point_id = _canonical_video_chunk_id(point.video_chunk_id)
        series_id = require_nonempty_text(point.series_id, "series_id")
        video_id = require_nonempty_text(point.video_id, "video_id")
        source_revision = require_nonempty_text(point.source_revision, "source_revision")
        if not isinstance(point.is_fallback, bool):
            raise ValueError("is_fallback must be a bool")

        text_vector = _validated_vector(point.text_vector, "text_vector")
        vision_vector = _validated_vector(point.vision_vector, "vision_vector")
        if len(text_vector) != self.text_dimensions:
            raise ValueError(
                f"text_vector must have exactly {self.text_dimensions} dimensions"
            )
        if len(vision_vector) != self.vision_dimensions:
            raise ValueError(
                f"vision_vector must have exactly {self.vision_dimensions} dimensions"
            )
        self.ensure_collection()
        self._client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={
                        TEXT_VECTOR_NAME: text_vector,
                        VISION_VECTOR_NAME: vision_vector,
                    },
                    payload={
                        SERIES_ID_PAYLOAD_KEY: series_id,
                        VIDEO_ID_PAYLOAD_KEY: video_id,
                        VIDEO_CHUNK_ID_PAYLOAD_KEY: point_id,
                        SOURCE_REVISION_PAYLOAD_KEY: source_revision,
                        IS_FALLBACK_PAYLOAD_KEY: point.is_fallback,
                    },
                )
            ],
            wait=True,
        )

    def delete(self, video_chunk_id: str) -> None:
        point_id = _canonical_video_chunk_id(video_chunk_id)
        if not self._client.collection_exists(self.collection_name):
            return
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=[point_id],
            wait=True,
        )

    def inspect(
        self,
        video_chunk_ids: Sequence[str],
    ) -> dict[str, StoredPointState]:
        point_ids = list(
            dict.fromkeys(
                _canonical_video_chunk_id(item) for item in video_chunk_ids
            )
        )
        states = {
            point_id: StoredPointState(present=False, source_revision=None)
            for point_id in point_ids
        }
        if not point_ids or not self._client.collection_exists(self.collection_name):
            return states

        records = self._client.retrieve(
            collection_name=self.collection_name,
            ids=point_ids,
            with_payload=[SOURCE_REVISION_PAYLOAD_KEY],
            with_vectors=False,
        )
        for record in records:
            record_id = str(record.id)
            if record_id not in states:
                continue
            payload = record.payload or {}
            source_revision = payload.get(SOURCE_REVISION_PAYLOAD_KEY)
            states[record_id] = StoredPointState(
                present=True,
                source_revision=(
                    source_revision
                    if isinstance(source_revision, str) and source_revision
                    else None
                ),
            )
        return states

    def search_text(
        self,
        query_vector: Sequence[float],
        *,
        series_ids: Sequence[str],
        limit: int,
        exact: bool = False,
    ) -> list[VideoChunkIndexMatch]:
        return self._search(
            query_vector,
            vector_name=TEXT_VECTOR_NAME,
            expected_dimensions=self.text_dimensions,
            series_ids=series_ids,
            limit=limit,
            exact=exact,
        )

    def search_vision(
        self,
        query_vector: Sequence[float],
        *,
        series_ids: Sequence[str],
        limit: int,
        exact: bool = False,
    ) -> list[VideoChunkIndexMatch]:
        return self._search(
            query_vector,
            vector_name=VISION_VECTOR_NAME,
            expected_dimensions=self.vision_dimensions,
            series_ids=series_ids,
            limit=limit,
            exact=exact,
        )

    def _search(
        self,
        query_vector: Sequence[float],
        *,
        vector_name: str,
        expected_dimensions: int,
        series_ids: Sequence[str],
        limit: int,
        exact: bool,
    ) -> list[VideoChunkIndexMatch]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        vector = _validated_vector(query_vector, "query_vector")
        if len(vector) != expected_dimensions:
            raise ValueError(
                f"query_vector must have exactly {expected_dimensions} dimensions "
                f"for the {vector_name!r} channel"
            )
        scope = _validated_series_scope(series_ids)
        if not self._client.collection_exists(self.collection_name):
            raise QdrantCollectionNotFoundError(
                f"Qdrant collection {self.collection_name!r} does not exist"
            )

        search_params = models.SearchParams(
            exact=exact,
            quantization=(
                models.QuantizationSearchParams(ignore=True) if exact else None
            ),
        )
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    r"Local mode performs exact \(brute-force\) search, so "
                    r"`search_params` has no effect.*"
                ),
                category=UserWarning,
            )
            response = self._client.query_points(
                collection_name=self.collection_name,
                query=vector,
                using=vector_name,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=SERIES_ID_PAYLOAD_KEY,
                            match=models.MatchAny(any=scope),
                        )
                    ]
                ),
                search_params=search_params,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )

        parsed: list[tuple[str, float, str, str, str, bool]] = []
        seen_ids: set[str] = set()
        for point in response.points:
            try:
                point_id = _canonical_video_chunk_id(str(point.id))
                payload = point.payload
                if not isinstance(payload, Mapping):
                    raise ValueError("payload must be a mapping")
                if set(payload) != _POINT_PAYLOAD_KEYS:
                    raise ValueError(
                        "payload keys must exactly match the formal index contract"
                    )
                payload_chunk_id = _canonical_video_chunk_id(
                    payload[VIDEO_CHUNK_ID_PAYLOAD_KEY]
                )
                if payload_chunk_id != point_id:
                    raise ValueError("payload videoChunkId does not match point ID")
                series_id = require_nonempty_text(
                    payload[SERIES_ID_PAYLOAD_KEY],
                    "payload seriesId",
                )
                if series_id not in scope:
                    raise ValueError("payload seriesId is outside the requested scope")
                video_id = require_nonempty_text(
                    payload[VIDEO_ID_PAYLOAD_KEY],
                    "payload videoId",
                )
                source_revision = require_nonempty_text(
                    payload[SOURCE_REVISION_PAYLOAD_KEY],
                    "payload sourceRevision",
                )
                is_fallback = payload[IS_FALLBACK_PAYLOAD_KEY]
                if not isinstance(is_fallback, bool):
                    raise ValueError("payload isFallback must be a bool")
                if isinstance(point.score, bool):
                    raise ValueError("score must be a finite number")
                score = float(point.score)
                if not math.isfinite(score):
                    raise ValueError("score must be a finite number")
                if point_id in seen_ids:
                    raise ValueError("query returned the same point more than once")
            except (KeyError, TypeError, ValueError) as exc:
                raise QdrantPointConsistencyError(
                    f"Qdrant returned an invalid {vector_name} VideoChunk point: {exc}"
                ) from exc
            seen_ids.add(point_id)
            parsed.append(
                (
                    point_id,
                    score,
                    series_id,
                    video_id,
                    source_revision,
                    is_fallback,
                )
            )

        parsed.sort(key=lambda item: (-item[1], item[0]))
        return [
            VideoChunkIndexMatch(
                rank=rank,
                video_chunk_id=point_id,
                score=score,
                series_id=series_id,
                video_id=video_id,
                source_revision=source_revision,
                is_fallback=is_fallback,
            )
            for rank, (
                point_id,
                score,
                series_id,
                video_id,
                source_revision,
                is_fallback,
            ) in enumerate(parsed, start=1)
        ]

    def _validate_collection_environment(self) -> None:
        normalized = self.collection_name.casefold()
        is_test = normalized.endswith("_test")
        is_development = normalized.endswith(("_dev", "_development"))
        if self.environment is DatabaseEnvironment.TEST and not is_test:
            raise ValueError("test collection name must end with '_test'")
        if self.environment is DatabaseEnvironment.DEVELOPMENT and not is_development:
            raise ValueError(
                "development collection name must end with '_dev' or '_development'"
            )
        if self.environment is DatabaseEnvironment.PRODUCTION and (
            is_test or is_development
        ):
            raise ValueError(
                "production cannot use a test or development collection"
            )

    def _validate_vector_schema(
        self,
        actual: object,
        expected: Mapping[str, models.VectorParams],
    ) -> None:
        if not isinstance(actual, Mapping):
            raise QdrantCollectionSchemaError(
                f"collection {self.collection_name!r} does not use named vectors"
            )
        if set(actual) != set(expected):
            raise QdrantCollectionSchemaError(
                f"collection {self.collection_name!r} must contain exactly the "
                f"named vectors {sorted(expected)}"
            )

        for vector_name, expected_params in expected.items():
            actual_params = actual[vector_name]
            if (
                actual_params.size != expected_params.size
                or actual_params.distance != models.Distance.COSINE
                or actual_params.datatype not in (None, models.Datatype.FLOAT32)
                or actual_params.multivector_config is not None
            ):
                raise QdrantCollectionSchemaError(
                    f"collection {self.collection_name!r} named vector "
                    f"{vector_name!r} has an incompatible vector schema"
                )


def _canonical_video_chunk_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("video_chunk_id must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("video_chunk_id must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ValueError("video_chunk_id must be a canonical UUID string")
    return value


def _validated_series_scope(series_ids: Sequence[str]) -> list[str]:
    if isinstance(series_ids, (str, bytes)):
        raise ValueError("series_ids must be a sequence of IDs")
    try:
        scope = list(
            dict.fromkeys(require_nonempty_text(item, "series_id") for item in series_ids)
        )
    except TypeError as exc:
        raise ValueError("series_ids must be a sequence of IDs") from exc
    if not scope:
        raise ValueError("series_ids must not be empty")
    return scope


def _validated_vector(values: Sequence[float], field_name: str) -> list[float]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must contain numeric values")
    try:
        vector = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError
            vector.append(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain numeric values") from exc
    if not vector:
        raise ValueError(f"{field_name} must not be empty")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError(f"{field_name} must contain only finite values")
    return vector


def _validate_dimensions(text_dimensions: int, vision_dimensions: int) -> None:
    dimensions = (text_dimensions, vision_dimensions)
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in dimensions
    ):
        raise ValueError("vector dimensions must be positive integers")
