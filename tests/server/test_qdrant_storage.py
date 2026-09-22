from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from qdrant_client import QdrantClient, models

from server.database import DatabaseEnvironment
from server.storage.qdrant import (
    QdrantCollectionNotFoundError,
    QdrantCollectionSchemaError,
    QdrantPointConsistencyError,
    QdrantVideoChunkStore,
    VideoChunkIndexPoint,
)


def _point(
    video_chunk_id: str | None = None,
    *,
    series_id: str | None = None,
    video_id: str | None = None,
    source_revision: str = "revision-1",
    is_fallback: bool = False,
    text_vector: list[float] | None = None,
    vision_vector: list[float] | None = None,
) -> VideoChunkIndexPoint:
    return VideoChunkIndexPoint(
        series_id=series_id or str(uuid4()),
        video_id=video_id or str(uuid4()),
        video_chunk_id=video_chunk_id or str(uuid4()),
        source_revision=source_revision,
        is_fallback=is_fallback,
        text_vector=text_vector if text_vector is not None else [1.0, 0.0],
        vision_vector=(
            vision_vector if vision_vector is not None else [0.0, 1.0, 0.0]
        ),
    )


def _store(
    client: QdrantClient,
    *,
    text_dimensions: int = 2,
    vision_dimensions: int = 3,
) -> QdrantVideoChunkStore:
    return QdrantVideoChunkStore(
        client,
        collection_name="video_chunks_test",
        environment=DatabaseEnvironment.TEST,
        text_dimensions=text_dimensions,
        vision_dimensions=vision_dimensions,
    )


def test_upsert_uses_formal_chunk_id_and_exact_payload() -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    point = _point()
    original_upsert = client.upsert
    original_create_payload_index = client.create_payload_index
    client.upsert = Mock(wraps=original_upsert)
    client.create_payload_index = Mock(wraps=original_create_payload_index)

    store.upsert(point)

    records = client.retrieve(
        collection_name=store.collection_name,
        ids=[point.video_chunk_id],
        with_payload=True,
        with_vectors=True,
    )
    assert len(records) == 1
    stored = records[0]
    assert stored.id == point.video_chunk_id
    assert UUID(str(stored.id)) == UUID(point.video_chunk_id)
    assert set(stored.vector) == {"text", "vision"}
    assert stored.payload == {
        "seriesId": point.series_id,
        "videoId": point.video_id,
        "videoChunkId": point.video_chunk_id,
        "sourceRevision": point.source_revision,
        "isFallback": False,
    }
    assert client.upsert.call_args.kwargs["wait"] is True
    assert client.create_payload_index.call_args.kwargs == {
        "collection_name": store.collection_name,
        "field_name": "seriesId",
        "field_schema": models.PayloadSchemaType.KEYWORD,
        "wait": True,
    }

    vectors = client.get_collection(
        store.collection_name
    ).config.params.vectors
    assert set(vectors) == {"text", "vision"}
    assert vectors["text"].size == 2
    assert vectors["vision"].size == 3
    assert all(item.distance is models.Distance.COSINE for item in vectors.values())
    assert all(item.datatype is models.Datatype.FLOAT32 for item in vectors.values())


def test_repeated_upsert_replaces_one_stable_point() -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    point = _point()

    store.upsert(point)
    client.set_payload(
        collection_name=store.collection_name,
        payload={"unexpected": "stale"},
        points=[point.video_chunk_id],
        wait=True,
    )
    store.upsert(
        VideoChunkIndexPoint(
            series_id=point.series_id,
            video_id=point.video_id,
            video_chunk_id=point.video_chunk_id,
            source_revision="revision-2",
            is_fallback=True,
            text_vector=[0.0, 1.0],
            vision_vector=[1.0, 0.0, 0.0],
        )
    )

    assert client.count(collection_name=store.collection_name, exact=True).count == 1
    state = store.inspect([point.video_chunk_id])[point.video_chunk_id]
    assert state.present is True
    assert state.source_revision == "revision-2"
    records = client.retrieve(
        collection_name=store.collection_name,
        ids=[point.video_chunk_id],
        with_payload=True,
        with_vectors=False,
    )
    assert set(records[0].payload) == {
        "seriesId",
        "videoId",
        "videoChunkId",
        "sourceRevision",
        "isFallback",
    }


def test_inspect_reports_present_missing_and_stale_revision() -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    present = _point(source_revision="old-revision")
    missing_id = str(uuid4())
    store.upsert(present)

    states = store.inspect([present.video_chunk_id, missing_id])

    assert states[present.video_chunk_id].present is True
    assert states[present.video_chunk_id].source_revision == "old-revision"
    assert states[missing_id].present is False
    assert states[missing_id].source_revision is None


def test_search_filters_multi_series_scope_before_each_channel_top_k() -> None:
    client = QdrantClient(":memory:")
    store = _store(client, text_dimensions=2, vision_dimensions=2)
    first_series_id = str(uuid4())
    second_series_id = str(uuid4())
    excluded_series_id = str(uuid4())
    text_winner = _point(
        series_id=first_series_id,
        text_vector=[0.8, 0.6],
        vision_vector=[0.0, 1.0],
    )
    vision_winner = _point(
        series_id=second_series_id,
        text_vector=[0.6, 0.8],
        vision_vector=[1.0, 0.0],
    )
    excluded_global_winner = _point(
        series_id=excluded_series_id,
        text_vector=[1.0, 0.0],
        vision_vector=[1.0, 0.0],
    )
    excluded_same_series_winner = _point(
        series_id=first_series_id,
        text_vector=[1.0, 0.0],
        vision_vector=[1.0, 0.0],
    )
    for point in (
        text_winner,
        vision_winner,
        excluded_global_winner,
        excluded_same_series_winner,
    ):
        store.upsert(point)

    scope = [first_series_id, second_series_id]
    video_scope = [text_winner.video_id, vision_winner.video_id]
    text = store.search_text(
        [1.0, 0.0], series_ids=scope, video_ids=video_scope, limit=1
    )
    vision = store.search_vision(
        [1.0, 0.0], series_ids=scope, video_ids=video_scope, limit=1
    )

    assert [item.video_chunk_id for item in text] == [text_winner.video_chunk_id]
    assert [item.video_chunk_id for item in vision] == [vision_winner.video_chunk_id]
    assert text[0].rank == vision[0].rank == 1
    assert {text[0].series_id, vision[0].series_id} == set(scope)
    assert excluded_global_winner.video_chunk_id not in {
        text[0].video_chunk_id,
        vision[0].video_chunk_id,
    }
    assert excluded_same_series_winner.video_chunk_id not in {
        text[0].video_chunk_id,
        vision[0].video_chunk_id,
    }


def test_search_rejects_a_payload_id_that_disagrees_with_point_id() -> None:
    client = QdrantClient(":memory:")
    store = _store(client, text_dimensions=2, vision_dimensions=2)
    store.ensure_collection()
    point_id = str(uuid4())
    series_id = str(uuid4())
    video_id = str(uuid4())
    client.upsert(
        collection_name=store.collection_name,
        points=[
            models.PointStruct(
                id=point_id,
                vector={"text": [1.0, 0.0], "vision": [1.0, 0.0]},
                payload={
                    "seriesId": series_id,
                    "videoId": video_id,
                    "videoChunkId": str(uuid4()),
                    "sourceRevision": "revision-1",
                    "isFallback": False,
                },
            )
        ],
        wait=True,
    )

    with pytest.raises(QdrantPointConsistencyError, match="does not match point ID"):
        store.search_text(
            [1.0, 0.0],
            series_ids=[series_id],
            video_ids=[video_id],
            limit=1,
        )


def test_search_rejects_empty_scope_and_missing_collection() -> None:
    store = _store(
        QdrantClient(":memory:"),
        text_dimensions=2,
        vision_dimensions=2,
    )

    with pytest.raises(ValueError, match="series_ids must not be empty"):
        store.search_text(
            [1.0, 0.0], series_ids=[], video_ids=[str(uuid4())], limit=1
        )
    with pytest.raises(ValueError, match="video_ids must not be empty"):
        store.search_text(
            [1.0, 0.0], series_ids=[str(uuid4())], video_ids=[], limit=1
        )
    with pytest.raises(QdrantCollectionNotFoundError, match="does not exist"):
        store.search_text(
            [1.0, 0.0],
            series_ids=[str(uuid4())],
            video_ids=[str(uuid4())],
            limit=1,
        )


def test_delete_is_idempotent_and_waits_for_completion() -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    point = _point()
    store.upsert(point)
    original_delete = client.delete
    client.delete = Mock(wraps=original_delete)

    store.delete(point.video_chunk_id)
    store.delete(point.video_chunk_id)

    state = store.inspect([point.video_chunk_id])[point.video_chunk_id]
    assert state.present is False
    assert client.delete.call_count == 2
    assert all(call.kwargs["wait"] is True for call in client.delete.call_args_list)


@pytest.mark.parametrize(
    ("environment", "collection_name"),
    [
        (DatabaseEnvironment.TEST, "chunks"),
        (DatabaseEnvironment.DEVELOPMENT, "chunks_test"),
        (DatabaseEnvironment.PRODUCTION, "chunks_test"),
        (DatabaseEnvironment.PRODUCTION, "chunks_development"),
    ],
)
def test_collection_name_must_match_environment(
    environment: DatabaseEnvironment,
    collection_name: str,
) -> None:
    with pytest.raises(ValueError, match="collection"):
        QdrantVideoChunkStore(
            QdrantClient(":memory:"),
            collection_name=collection_name,
            environment=environment,
            text_dimensions=2,
            vision_dimensions=3,
        )


def test_production_accepts_a_name_without_nonproduction_suffix() -> None:
    store = QdrantVideoChunkStore(
        QdrantClient(":memory:"),
        collection_name="video_chunks",
        environment=DatabaseEnvironment.PRODUCTION,
        text_dimensions=2,
        vision_dimensions=3,
    )

    assert store.collection_name == "video_chunks"


@pytest.mark.parametrize(
    "video_chunk_id",
    [
        "not-a-uuid",
        "00000000-0000-0000-0000-00000000000A",
        "{00000000-0000-0000-0000-00000000000a}",
    ],
)
def test_point_id_requires_a_canonical_uuid(video_chunk_id: str) -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        _store(QdrantClient(":memory:")).upsert(_point(video_chunk_id))


@pytest.mark.parametrize(
    ("text_vector", "vision_vector", "field_name"),
    [
        ([1.0], [0.0, 1.0, 0.0], "text_vector"),
        ([1.0, 0.0], [0.0, 1.0], "vision_vector"),
    ],
)
def test_upsert_validates_contract_dimensions_before_creating_collection(
    text_vector: list[float],
    vision_vector: list[float],
    field_name: str,
) -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    point = replace(
        _point(),
        text_vector=text_vector,
        vision_vector=vision_vector,
    )

    with pytest.raises(ValueError, match=f"{field_name} must have exactly"):
        store.upsert(point)

    assert client.collection_exists(store.collection_name) is False


def test_upsert_rejects_bool_vector_values_before_creating_collection() -> None:
    client = QdrantClient(":memory:")
    store = _store(client)
    point = replace(_point(), text_vector=[True, 0.0])

    with pytest.raises(ValueError, match="text_vector must contain numeric values"):
        store.upsert(point)

    assert client.collection_exists(store.collection_name) is False


@pytest.mark.parametrize(
    "text_config",
    [
        models.VectorParams(
            size=3,
            distance=models.Distance.COSINE,
            datatype=models.Datatype.FLOAT32,
        ),
        models.VectorParams(
            size=2,
            distance=models.Distance.DOT,
            datatype=models.Datatype.FLOAT32,
        ),
    ],
)
def test_existing_collection_schema_must_match_dimensions_and_distance(
    text_config: models.VectorParams,
) -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="video_chunks_test",
        vectors_config={
            "text": text_config,
            "vision": models.VectorParams(
                size=3,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT32,
            ),
        },
    )

    with pytest.raises(QdrantCollectionSchemaError, match="vector schema"):
        _store(client).ensure_collection()


def test_existing_collection_rejects_unnamed_or_reduced_precision_vectors() -> None:
    unnamed_client = QdrantClient(":memory:")
    unnamed_client.create_collection(
        collection_name="video_chunks_test",
        vectors_config=models.VectorParams(
            size=2,
            distance=models.Distance.COSINE,
            datatype=models.Datatype.FLOAT32,
        ),
    )
    with pytest.raises(QdrantCollectionSchemaError, match="named vectors"):
        _store(unnamed_client).ensure_collection()

    reduced_client = QdrantClient(":memory:")
    reduced_client.create_collection(
        collection_name="video_chunks_test",
        vectors_config={
            "text": models.VectorParams(
                size=2,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT16,
            ),
            "vision": models.VectorParams(
                size=3,
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT32,
            ),
        },
    )
    with pytest.raises(QdrantCollectionSchemaError, match="vector schema"):
        _store(reduced_client).ensure_collection()


@pytest.mark.parametrize(
    ("text_dimensions", "vision_dimensions"),
    [(0, 3), (2, -1), (2.5, 3), (True, 3)],
)
def test_vector_dimensions_must_be_positive_integers(
    text_dimensions,
    vision_dimensions,
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        QdrantVideoChunkStore(
            QdrantClient(":memory:"),
            collection_name="video_chunks_test",
            environment=DatabaseEnvironment.TEST,
            text_dimensions=text_dimensions,
            vision_dimensions=vision_dimensions,
        )
