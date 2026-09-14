import pytest
from qdrant_client import QdrantClient, models

from evals.recall.qdrant_index import (
    QdrantCollectionSchemaError,
    QdrantDatasetConflictError,
    QdrantVideoSegmentIndex,
)
from engine.retrieval.video_retrieval import SegmentRetrievalChunk


def _chunk(
    sequence_no: int,
    *,
    video_compact: str,
    is_fallback: bool = False,
) -> SegmentRetrievalChunk:
    return SegmentRetrievalChunk(
        sequence_no=sequence_no,
        start_ms=sequence_no * 60_000,
        end_ms=(sequence_no + 1) * 60_000,
        video_compact=video_compact,
        text=video_compact,
        is_fallback=is_fallback,
    )


def test_named_vectors_are_stored_and_queried_per_dataset() -> None:
    client = QdrantClient(":memory:")
    first = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="mygo-ep07",
    )
    second = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="other-video",
    )
    first.ensure_collection(text_dimensions=2, vision_dimensions=2)
    second.ensure_collection(text_dimensions=2, vision_dimensions=2)

    chunks = [
        _chunk(0, video_compact="text winner"),
        _chunk(1, video_compact="vision winner", is_fallback=True),
    ]
    first.upsert_segments(
        chunks,
        text_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
        vision_vectors={0: [0.0, 1.0], 1: [1.0, 0.0]},
    )
    second.upsert_segments(
        [_chunk(0, video_compact="must stay isolated")],
        text_vectors={0: [1.0, 0.0]},
        vision_vectors={0: [1.0, 0.0]},
    )

    text = first.search_text([1.0, 0.0], limit=2, exact=False)
    vision = first.search_vision([1.0, 0.0], limit=2, exact=False)

    assert [item.segment_sequence_no for item in text] == [0, 1]
    assert [item.segment_sequence_no for item in vision] == [1, 0]
    assert first.count_segments() == 2
    assert second.count_segments() == 1

    stored, _ = client.scroll(
        collection_name="video_segments_test",
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="dataset_id",
                    match=models.MatchValue(value="mygo-ep07"),
                ),
                models.FieldCondition(
                    key="segment_sequence_no",
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
    assert set(point.vector) == {"text", "vision"}
    assert point.payload == {
        "dataset_id": "mygo-ep07",
        "video_segment_id": "mygo-ep07:segment-0001",
        "segment_sequence_no": 1,
        "start_ms": 60_000,
        "end_ms": 120_000,
        "video_compact": "vision winner",
        "is_fallback": True,
    }


def test_upsert_is_idempotent_for_the_same_dataset_and_sequence() -> None:
    client = QdrantClient(":memory:")
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="mygo-ep07",
    )
    index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    chunks = [_chunk(0, video_compact="first")]
    index.upsert_segments(
        chunks,
        text_vectors={0: [1.0, 0.0]},
        vision_vectors={0: [1.0, 0.0]},
    )
    updated = [_chunk(0, video_compact="updated")]
    index.upsert_segments(
        updated,
        text_vectors={0: [0.0, 1.0]},
        vision_vectors={0: [0.0, 1.0]},
    )

    assert index.count_segments() == 1
    result = index.search_text([0.0, 1.0], limit=1, exact=False)
    assert result[0].segment_sequence_no == 0


def test_existing_collection_schema_must_match() -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="video_segments_test",
        vectors_config={
            "text": models.VectorParams(size=3, distance=models.Distance.COSINE),
            "vision": models.VectorParams(size=2, distance=models.Distance.COSINE),
        },
    )
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="mygo-ep07",
    )

    with pytest.raises(QdrantCollectionSchemaError) as exc_info:
        index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    assert "incompatible vector schema" in str(exc_info.value)


def test_existing_collection_rejects_reduced_precision_vectors() -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="video_segments_test",
        vectors_config={
            "text": models.VectorParams(
                size=2,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT16,
            ),
            "vision": models.VectorParams(size=2, distance=models.Distance.COSINE),
        },
    )
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="mygo-ep07",
    )

    with pytest.raises(QdrantCollectionSchemaError, match="vector schema"):
        index.ensure_collection(text_dimensions=2, vision_dimensions=2)


def test_upsert_requires_both_vectors_for_every_segment() -> None:
    index = QdrantVideoSegmentIndex(
        QdrantClient(":memory:"),
        collection_name="video_segments_test",
        dataset_id="mygo-ep07",
    )
    chunks = [_chunk(0, video_compact="segment")]

    with pytest.raises(ValueError) as exc_info:
        index.upsert_segments(
            chunks,
            text_vectors={0: [1.0, 0.0]},
            vision_vectors={},
        )
    assert "vision vectors" in str(exc_info.value)


def test_reusing_dataset_id_with_another_segment_count_is_rejected() -> None:
    client = QdrantClient(":memory:")
    index = QdrantVideoSegmentIndex(
        client,
        collection_name="video_segments_test",
        dataset_id="test:mygo:ep07:60s:v1",
    )
    index.ensure_collection(text_dimensions=2, vision_dimensions=2)
    index.upsert_segments(
        [_chunk(0, video_compact="first"), _chunk(1, video_compact="second")],
        text_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
        vision_vectors={0: [1.0, 0.0], 1: [0.0, 1.0]},
    )

    with pytest.raises(QdrantDatasetConflictError, match="Use a new dataset ID"):
        index.assert_dataset_size(1, allow_empty=True)

    assert index.count_segments() == 2
