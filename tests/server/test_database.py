from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import pytest

from server.database import (
    DatabaseEnvironment,
    SchemaUpgradeRequiredError,
    create_database_engine,
    create_schema,
)
from server.model.video import VideoChunkInfoRecord, VideoChunkRecord


def test_database_environment_requires_physically_separate_databases() -> None:
    development = create_database_engine(
        "mysql+pymysql://user:password@127.0.0.1/anivag_development",
        environment=DatabaseEnvironment.DEVELOPMENT,
    )
    assert development.url.database == "anivag_development"
    assert development.url.query["charset"] == "utf8mb4"

    with pytest.raises(ValueError, match="test database name"):
        create_database_engine(
            "mysql+pymysql://user:password@127.0.0.1/anivag",
            environment=DatabaseEnvironment.TEST,
        )
    with pytest.raises(ValueError, match="SQLite is only allowed"):
        create_database_engine(
            "sqlite+pysqlite:///:memory:",
            environment=DatabaseEnvironment.DEVELOPMENT,
        )
    with pytest.raises(ValueError, match="test database name"):
        create_database_engine(
            "mysql+pymysql://user:password@127.0.0.1/anivag",
            environment="test",
        )
    with pytest.raises(ValueError, match="charset must be utf8mb4"):
        create_database_engine(
            "mysql+pymysql://user:password@127.0.0.1/anivag_test?charset=latin1",
            environment=DatabaseEnvironment.TEST,
        )
    with pytest.raises(ValueError, match="charset must be utf8mb4"):
        create_database_engine(
            "mysql+pymysql://user:password@127.0.0.1/anivag_test"
            "?charset=utf8mb4&charset=latin1",
            environment=DatabaseEnvironment.TEST,
        )


def test_mysql_schema_uses_innodb_json_and_integer_milliseconds() -> None:
    chunk_ddl = str(
        CreateTable(VideoChunkRecord.__table__).compile(dialect=mysql.dialect())
    )
    info_ddl = str(
        CreateTable(VideoChunkInfoRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "ENGINE=InnoDB" in chunk_ddl
    assert "CHARSET=utf8mb4" in chunk_ddl
    assert "start_ms BIGINT" in chunk_ddl
    assert "all_time_events JSON" in info_ddl
    assert "video_compact LONGTEXT" in info_ddl


def test_create_schema_refuses_to_shadow_a_legacy_video_segment_schema() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE video_segment (id INTEGER)"))
        connection.execute(
            text(
                "CREATE TABLE context_snapshot "
                "(after_segment_sequence_no INTEGER)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="controlled VideoChunk upgrade",
    ):
        create_schema(engine)

    assert "video_chunk" not in inspect(engine).get_table_names()
