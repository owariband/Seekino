import json
from copy import deepcopy

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import StatementError

from server.model.job import ChunkParseContent
from server.model.video import VideoChunkEmbedding


VECTOR = [
    -0.00010395050048828125, 1.9311904907226562e-05,
    -0.0, 0.0, 0.1, 5e-324, 1.7976931348623157e308,
]
CACHE_KEY = "_index_embeddings_v2"


@pytest.fixture
def storage():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    table = Table(
        "numeric_payloads", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("vector", VideoChunkEmbedding.__table__.c.text_vector.type),
        Column("detail", ChunkParseContent.__table__.c.detail.type),
    )
    table.create(engine)
    yield engine, table
    engine.dispose()


def test_vector_is_a_database_json_string_and_restores_identical_float_bits(storage):
    engine, table = storage
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, vector=VECTOR))
        stored = json.loads(connection.execute(text(
            "SELECT vector FROM numeric_payloads WHERE id = 1"
        )).scalar_one())
        assert isinstance(stored, str)
        restored = connection.execute(select(table.c.vector)).scalar_one()
        assert [value.hex() for value in restored] == [value.hex() for value in VECTOR]
        # Whole-column comparisons must use the same encoded bind value.
        assert connection.execute(select(table.c.id).where(
            table.c.vector == VECTOR
        )).scalar_one() == 1


def test_only_representations_are_strings_and_mysql_binding_keeps_receipts_queryable(storage):
    engine, table = storage
    representations = {
        "text": {"channel": "semantic", "vector": VECTOR},
        "video": {"channel": "vision", "vector": VECTOR[::-1]},
        "sparse": {"channel": "sparse", "weights": {"17": VECTOR[1], "42": 0.1}},
    }
    detail = {
        "source": "fixture",
        CACHE_KEY: {"content_version": 2, "receipt": {"index_generation": 3},
                    "representations": representations},
    }
    original = deepcopy(detail)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, detail=detail))
        raw = json.loads(connection.execute(text(
            "SELECT detail FROM numeric_payloads WHERE id = 1"
        )).scalar_one())
        assert isinstance(raw[CACHE_KEY]["representations"], str)
        assert raw[CACHE_KEY]["content_version"] == 2
        assert raw[CACHE_KEY]["receipt"] == {"index_generation": 3}
        assert connection.execute(select(
            table.c.detail[CACHE_KEY]["receipt"]["index_generation"].as_integer()
        )).scalar_one() == 3
        restored = connection.execute(select(table.c.detail)).scalar_one()
        assert restored == original
        assert [value.hex() for value in restored[CACHE_KEY]["representations"]["text"]["vector"]] == [
            value.hex() for value in VECTOR
        ]
    assert detail == original  # Binding must not replace live arrays with strings.
    for column in (table.c.vector, table.c.detail):
        assert column.type.compile(dialect=mysql.dialect()) == "JSON"
    dialect = mysql.dialect()
    bound = table.c.detail.type.bind_processor(dialect)(detail)
    assert isinstance(json.loads(bound)[CACHE_KEY]["representations"], str)
    assert table.c.detail.type.result_processor(dialect, None)(bound) == original


@pytest.mark.parametrize("encoded", [False, True])
def test_legacy_values_and_preencoded_payloads_remain_readable(storage, encoded):
    engine, table = storage
    representations = {"sparse": {"channel": "sparse", "weights": {"17": 0.1}}}
    detail = {CACHE_KEY: {"representations": representations}}
    if encoded:
        vector = json.dumps(VECTOR)
        detail[CACHE_KEY]["representations"] = json.dumps(representations)
    else:
        vector = VECTOR
    with engine.begin() as connection:
        # Raw SQL models rows created before the new ORM serialization boundary.
        connection.execute(text(
            "INSERT INTO numeric_payloads (id, vector, detail) VALUES (1, :vector, :detail)"
        ), {"vector": json.dumps(vector), "detail": json.dumps(detail)})
        row = connection.execute(select(table)).one()
        assert [value.hex() for value in row.vector] == [value.hex() for value in VECTOR]
        assert row.detail[CACHE_KEY]["representations"] == representations
        # Rewriting either format produces exactly one string encoding layer.
        connection.execute(table.update().values(vector=vector, detail=detail))
        row = connection.execute(select(table)).one()
        assert row.vector == VECTOR
        assert row.detail[CACHE_KEY]["representations"] == representations


def test_nulls_and_details_without_vectors_keep_the_existing_shape(storage):
    engine, table = storage
    with engine.begin() as connection:
        connection.execute(table.insert(), [
            {"id": 1, "vector": None, "detail": None},
            {"id": 2, "vector": None, "detail": {"source": "understanding"}},
        ])
        rows = connection.execute(select(table).order_by(table.c.id)).all()
        assert rows[0].vector is rows[0].detail is None
        assert rows[1].vector is None
        assert rows[1].detail == {"source": "understanding"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_vectors_cannot_be_persisted_as_json_strings(storage, value):
    engine, table = storage
    with engine.begin() as connection:
        with pytest.raises(StatementError, match="Out of range float values"):
            connection.execute(table.insert().values(id=1, vector=[value]))
