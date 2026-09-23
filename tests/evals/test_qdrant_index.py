from uuid import NAMESPACE_URL, uuid5

import pytest
from qdrant_client import QdrantClient, models

from evals.recall.qdrant_index import (
    QdrantCollectionSchemaError,
    QdrantDatasetConflictError,
    QdrantVideoChunkIndex,
)
from engine.retrieval.video_retrieval import VideoChunkRetrievalDocument


def _chunk(
    sequence_no: int,
    *,
    video_compact: str,
    is_fallback: bool = False,
) -> VideoChunkRetrievalDocument:
    return VideoChunkRetrievalDocument(
        sequence_no=sequence_no,
        start_seconds=sequence_no * 60,
        end_seconds=(sequence_no + 1) * 60,
        video_compact=video_compact,
        text=video_compact,
        is_fallback=is_fallback,
    )


def test_named_vectors_are_stored_and_queried_per_dataset() -> None:
    client = QdrantClient(":memory:")
    first = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="mygo-ep07",
    )
    second = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="other-video",
    )
    first.ensure_collection(text_dimensions=2, vision_dimensions=2)
    second.ensure_collection(text_dimensions=2, vision_dimensions=2)

    chunks = [
        _chunk(0, video_compact="text winner"),
        _chunk(1, video_compact="vision winner", is_fallback=True),
    ]
    first.upsert_chunks(
        chunks,
        text_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
        vision_vectors={0: [0.0, 1.0], 1: [1.0, 0.0]},
    )
    second.upsert_chunks(
        [_chunk(0, video_compact="must stay isolated")],
        text_vectors={0: [1.0, 0.0]},
        vision_vectors={0: [1.0, 0.0]},
    )

    text = first.search_text([1.0, 0.0], limit=2, exact=False)
    vision = first.search_vision([1.0, 0.0], limit=2, exact=False)

    assert [item.chunk_sequence_no for item in text] == [0, 1]
    assert [item.chunk_sequence_no for item in vision] == [1, 0]
    assert first.count_chunks() == 2
    assert second.count_chunks() == 1

    stored, _ = client.scroll(
        collection_name="video_chunks_test",
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="dataset_id",
                    match=models.MatchValue(value="mygo-ep07"),
                ),
                models.FieldCondition(
                    key="chunk_sequence_no",
                    match=models.MatchValue(value=1),
                ),
            ]
        ),
        limit=1,
        with_payload=True,
        with_vectors=True,
    )
    assert len(stored) == 1
    point = stored[0]
    assert point.id == str(uuid5(NAMESPACE_URL, "anivag:mygo-ep07:segment:1"))
    assert set(point.vector) == {"text", "vision"}
    assert point.payload == {
        "dataset_id": "mygo-ep07",
        "video_chunk_id": "mygo-ep07:chunk-0001",
        "chunk_sequence_no": 1,
        "start_seconds": 60,
        "end_seconds": 120,
        "video_compact": "vision winner",
        "is_fallback": True,
    }


def test_upsert_is_idempotent_for_the_same_dataset_and_sequence() -> None:
    client = QdrantClient(":memory:")
    index = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="mygo-ep07",
    )
    index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    chunks = [_chunk(0, video_compact="first")]
    index.upsert_chunks(
        chunks,
        text_vectors={0: [1.0, 0.0]},
        vision_vectors={0: [1.0, 0.0]},
    )
    updated = [_chunk(0, video_compact="updated")]
    index.upsert_chunks(
        updated,
        text_vectors={0: [0.0, 1.0]},
        vision_vectors={0: [0.0, 1.0]},
    )

    assert index.count_chunks() == 1
    result = index.search_text([0.0, 1.0], limit=1, exact=False)
    assert result[0].chunk_sequence_no == 0


def test_existing_collection_schema_must_match() -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="video_chunks_test",
        vectors_config={
            "text": models.VectorParams(size=3, distance=models.Distance.COSINE),
            "vision": models.VectorParams(size=2, distance=models.Distance.COSINE),
        },
    )
    index = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="mygo-ep07",
    )

    with pytest.raises(QdrantCollectionSchemaError) as exc_info:
        index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    assert "incompatible vector schema" in str(exc_info.value)


def test_existing_collection_rejects_reduced_precision_vectors() -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="video_chunks_test",
        vectors_config={
            "text": models.VectorParams(
                size=2,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT16,
            ),
            "vision": models.VectorParams(size=2, distance=models.Distance.COSINE),
        },
    )
    index = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="mygo-ep07",
    )

    with pytest.raises(QdrantCollectionSchemaError, match="vector schema"):
        index.ensure_collection(text_dimensions=2, vision_dimensions=2)


def test_upsert_requires_both_vectors_for_every_chunk() -> None:
    index = QdrantVideoChunkIndex(
        QdrantClient(":memory:"),
        collection_name="video_chunks_test",
        dataset_id="mygo-ep07",
    )
    chunks = [_chunk(0, video_compact="chunk")]

    with pytest.raises(ValueError) as exc_info:
        index.upsert_chunks(
            chunks,
            text_vectors={0: [1.0, 0.0]},
            vision_vectors={},
        )
    assert "vision vectors" in str(exc_info.value)


def test_reusing_dataset_id_with_another_chunk_count_is_rejected() -> None:
    client = QdrantClient(":memory:")
    index = QdrantVideoChunkIndex(
        client,
        collection_name="video_chunks_test",
        dataset_id="test:mygo:ep07:60s:v1",
    )
    index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    index.upsert_chunks(
        [_chunk(0, video_compact="first"), _chunk(1, video_compact="second")],
        text_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
        vision_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
    )

    with pytest.raises(QdrantDatasetConflictError, match="Use a new dataset ID"):
        index.assert_dataset_size(1, allow_empty=True)

    assert index.count_chunks() == 2
