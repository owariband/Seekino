from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from .video_retrieval import RankedVideoSegment, SegmentRetrievalChunk

TEXT_VECTOR_NAME = "text"
VISION_VECTOR_NAME = "vision"
DATASET_ID_PAYLOAD_KEY = "dataset_id"


class QdrantCollectionSchemaError(RuntimeError):
    """The existing collection cannot store this experiment's vectors."""


class QdrantDatasetConflictError(RuntimeError):
    """A dataset ID already refers to a different Segment snapshot."""


class QdrantVideoSegmentIndex:
    """Store and query one isolated VideoSegment dataset in Qdrant."""

    def __init__(
        self,
        client: QdrantClient,
        *,
        collection_name: str,
        dataset_id: str,
    ) -> None:
        self._client = client
        self.collection_name = collection_name.strip()
        self.dataset_id = dataset_id.strip()
        if not self.collection_name:
            raise ValueError("collection_name must not be empty")
        if not self.dataset_id:
            raise ValueError("dataset_id must not be empty")

    def ensure_collection(
        self,
        *,
        text_dimensions: int,
        vision_dimensions: int,
    ) -> None:
        if text_dimensions <= 0 or vision_dimensions <= 0:
            raise ValueError("vector dimensions must be greater than zero")

        expected = {
            TEXT_VECTOR_NAME: models.VectorParams(
                size=text_dimensions,
                distance=models.Distance.COSINE,
            ),
            VISION_VECTOR_NAME: models.VectorParams(
                size=vision_dimensions,
                distance=models.Distance.COSINE,
            ),
        }
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=expected,
            )
        else:
            actual = self._client.get_collection(
                self.collection_name
            ).config.params.vectors
            if not isinstance(actual, Mapping):
                raise QdrantCollectionSchemaError(
                    f"collection {self.collection_name!r} does not use named vectors"
                )
            for vector_name, expected_params in expected.items():
                actual_params = actual.get(vector_name)
                if actual_params is None:
                    raise QdrantCollectionSchemaError(
                        f"collection {self.collection_name!r} is missing "
                        f"named vector {vector_name!r}"
                    )
                if (
                    actual_params.size != expected_params.size
                    or actual_params.distance != expected_params.distance
                    or actual_params.datatype
                    not in (None, models.Datatype.FLOAT32)
                    or actual_params.multivector_config is not None
                ):
                    raise QdrantCollectionSchemaError(
                        f"collection {self.collection_name!r} named vector "
                        f"{vector_name!r} has an incompatible vector schema"
                    )

        self._client.create_payload_index(
            collection_name=self.collection_name,
            field_name=DATASET_ID_PAYLOAD_KEY,
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )

    def upsert_segments(
        self,
        chunks: Sequence[SegmentRetrievalChunk],
        *,
        text_vectors: Mapping[int, list[float]],
        vision_vectors: Mapping[int, list[float]],
    ) -> None:
        sequences = {chunk.sequence_no for chunk in chunks}
        if not chunks:
            raise ValueError("at least one VideoSegment chunk is required")
        if sequences != set(text_vectors):
            raise ValueError("text vectors must contain exactly one vector per Segment")
        if sequences != set(vision_vectors):
            raise ValueError("vision vectors must contain exactly one vector per Segment")

        points = [
            models.PointStruct(
                id=self._point_id(chunk.sequence_no),
                vector={
                    TEXT_VECTOR_NAME: text_vectors[chunk.sequence_no],
                    VISION_VECTOR_NAME: vision_vectors[chunk.sequence_no],
                },
                payload={
                    DATASET_ID_PAYLOAD_KEY: self.dataset_id,
                    "video_segment_id": self._video_segment_id(chunk.sequence_no),
                    "segment_sequence_no": chunk.sequence_no,
                    "start_ms": chunk.start_ms,
                    "end_ms": chunk.end_ms,
                    "video_compact": chunk.video_compact,
                    "is_fallback": chunk.is_fallback,
                },
            )
            for chunk in chunks
        ]
        self._client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )

    def search_text(
        self,
        query_vector: list[float],
        *,
        limit: int,
        exact: bool,
    ) -> list[RankedVideoSegment]:
        return self._search(
            query_vector,
            vector_name=TEXT_VECTOR_NAME,
            limit=limit,
            exact=exact,
        )

    def search_vision(
        self,
        query_vector: list[float],
        *,
        limit: int,
        exact: bool,
    ) -> list[RankedVideoSegment]:
        return self._search(
            query_vector,
            vector_name=VISION_VECTOR_NAME,
            limit=limit,
            exact=exact,
        )

    def count_segments(self) -> int:
        return self._client.count(
            collection_name=self.collection_name,
            count_filter=self._dataset_filter(),
            exact=True,
        ).count

    def assert_dataset_size(
        self,
        expected_segment_count: int,
        *,
        allow_empty: bool,
    ) -> None:
        if expected_segment_count <= 0:
            raise ValueError("expected_segment_count must be greater than zero")
        actual = self.count_segments()
        if actual == expected_segment_count or (allow_empty and actual == 0):
            return
        raise QdrantDatasetConflictError(
            f"Qdrant dataset {self.dataset_id!r} contains {actual} Segments; "
            f"expected {expected_segment_count}. Use a new dataset ID for a "
            "different parse or segmentation snapshot."
        )

    def _search(
        self,
        query_vector: list[float],
        *,
        vector_name: str,
        limit: int,
        exact: bool,
    ) -> list[RankedVideoSegment]:
        if not query_vector:
            raise ValueError("query_vector must not be empty")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        search_params = models.SearchParams(
            exact=exact,
            quantization=(
                models.QuantizationSearchParams(ignore=True) if exact else None
            ),
        )
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            using=vector_name,
            query_filter=self._dataset_filter(),
            search_params=search_params,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        scored: list[tuple[int, float]] = []
        for point in response.points:
            payload = point.payload or {}
            if payload.get(DATASET_ID_PAYLOAD_KEY) != self.dataset_id:
                raise RuntimeError("Qdrant returned a Segment from another dataset")
            sequence_no = payload.get("segment_sequence_no")
            if not isinstance(sequence_no, int) or sequence_no < 0:
                raise RuntimeError(
                    "Qdrant Segment payload has an invalid segment_sequence_no"
                )
            scored.append((sequence_no, float(point.score)))

        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            RankedVideoSegment(
                rank=rank,
                segment_sequence_no=sequence_no,
                score=score,
            )
            for rank, (sequence_no, score) in enumerate(scored, start=1)
        ]

    def _dataset_filter(self) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key=DATASET_ID_PAYLOAD_KEY,
                    match=models.MatchValue(value=self.dataset_id),
                )
            ]
        )

    def _point_id(self, sequence_no: int) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                f"anivag:{self.dataset_id}:segment:{sequence_no}",
            )
        )

    def _video_segment_id(self, sequence_no: int) -> str:
        return f"{self.dataset_id}:segment-{sequence_no:04d}"
