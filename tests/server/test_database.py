from sqlalchemy import BigInteger, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import pytest

from server.database import (
    DatabaseEnvironment,
    SchemaUpgradeRequiredError,
    create_database_engine,
    create_schema,
)
from server.model.agent import (
    AgentConversationRecord,
    AgentMessageRecord,
    AgentMessageTimingRecord,
)
from server.model.video import VideoChunkInfoRecord, VideoChunkRecord
from server.model.job import VideoChunkCheckpointRecord


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
    checkpoint_ddl = str(
        CreateTable(VideoChunkCheckpointRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "ENGINE=InnoDB" in chunk_ddl
    assert "CHARSET=utf8mb4" in chunk_ddl
    assert "start_ms BIGINT" in chunk_ddl
    assert "all_time_events JSON" in info_ddl
    assert "video_compact LONGTEXT" in info_ddl
    assert "chunk_start_ms BIGINT" in checkpoint_ddl
    assert "chunk_end_ms BIGINT" in checkpoint_ddl
    assert "ck_video_chunk_checkpoint_range" in checkpoint_ddl


def test_mysql_agent_schema_persists_scope_messages_and_model_provenance() -> None:
    conversation_ddl = str(
        CreateTable(AgentConversationRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    message_ddl = str(
        CreateTable(AgentMessageRecord.__table__).compile(dialect=mysql.dialect())
    )
    timing_ddl = str(
        CreateTable(AgentMessageTimingRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "ENGINE=InnoDB" in conversation_ddl
    assert "workspace_id VARCHAR(64) NOT NULL" in conversation_ddl
    assert "session_id" not in conversation_ddl
    assert "series_ids" not in conversation_ddl
    assert "content LONGTEXT" in message_ddl
    assert "series_ids JSON NOT NULL" in message_ddl
    assert "requested_series_ids JSON" in message_ddl
    assert "model_name VARCHAR(255)" in message_ddl
    assert "search_result JSON" in message_ddl
    assert "requested_top_k INTEGER" in message_ddl
    assert "ON DELETE CASCADE" in message_ddl
    assert "total_duration_ms BIGINT" in timing_ddl
    assert "started_at BIGINT NOT NULL" in timing_ddl
    assert "stages JSON NOT NULL" in timing_ddl
    assert "ck_agent_message_timing_total_duration" in timing_ddl
    assert "ON DELETE CASCADE" in timing_ddl


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


@pytest.mark.parametrize("legacy_session_table", [False, True])
def test_create_schema_refuses_to_shadow_a_legacy_agent_session_schema(
    legacy_session_table: bool,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE agent_conversation (conversation_id TEXT, session_id TEXT)")
        )
        connection.execute(
            text("INSERT INTO agent_conversation VALUES ('existing-conversation', 'old-session')")
        )
        if legacy_session_table:
            connection.execute(text("CREATE TABLE agent_session (session_id TEXT)"))

    with pytest.raises(SchemaUpgradeRequiredError, match="controlled Workspace / Conversation upgrade"):
        create_schema(engine)

    assert "agent_message" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT conversation_id FROM agent_conversation")).scalar_one() == "existing-conversation"


@pytest.mark.parametrize(
    "conversation_scope,message_columns",
    [
        (True, "series_ids JSON, requested_series_ids JSON"),
        (False, "requested_series_ids JSON"),
        (False, "series_ids JSON"),
        (False, "content TEXT"),
    ],
)
def test_create_schema_requires_controlled_message_scope_migration(
    conversation_scope: bool,
    message_columns: str,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    scope_column = ", series_ids JSON" if conversation_scope else ""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE agent_conversation "
                f"(conversation_id TEXT, workspace_id TEXT{scope_column})"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_conversation (conversation_id, workspace_id) "
                "VALUES ('existing-conversation', 'local-default')"
            )
        )
        connection.execute(
            text(f"CREATE TABLE agent_message (message_id TEXT, {message_columns})")
        )
        connection.execute(
            text("INSERT INTO agent_message (message_id) VALUES ('existing-message')")
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="controlled Agent message-scope migration",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {
        "agent_conversation",
        "agent_message",
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT conversation_id FROM agent_conversation")
        ).scalar_one() == "existing-conversation"
        assert connection.execute(
            text("SELECT message_id FROM agent_message")
        ).scalar_one() == "existing-message"


def test_create_schema_accepts_current_message_scope_schema_repeatedly() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    create_schema(engine)

    inspector = inspect(engine)
    conversation_columns = {
        column["name"] for column in inspector.get_columns("agent_conversation")
    }
    message_columns = {
        column["name"] for column in inspector.get_columns("agent_message")
    }
    assert "series_ids" not in conversation_columns
    assert {"series_ids", "requested_series_ids"} <= message_columns
    assert {
        "message_id", "started_at", "total_duration_ms", "stages",
    } == {
        column["name"]
        for column in inspector.get_columns("agent_message_timing")
    }


def test_create_schema_requires_ui_view_migration_and_preserves_existing_rows() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE upload_session (upload_session_id TEXT, status TEXT)"))
        connection.execute(text("INSERT INTO upload_session VALUES ('existing-upload', 'COMPLETED')"))
    with pytest.raises(SchemaUpgradeRequiredError, match="UI view schema requires a controlled migration"):
        create_schema(engine)
    assert set(inspect(engine).get_table_names()) == {"upload_session"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM upload_session")).scalar_one() == "COMPLETED"


def test_create_schema_requires_checkpoint_range_migration() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE video_chunk_checkpoint "
                "(job_id TEXT, sequence_no INTEGER, stage TEXT)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="video_chunk_checkpoint.chunk_end_ms",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {"video_chunk_checkpoint"}


def test_create_schema_requires_series_language_hint_migration() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE series "
                "(series_id TEXT, workspace_id TEXT, name TEXT, description TEXT)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="series.transcript_language_hint",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {"series"}


def test_create_schema_requires_unix_millisecond_bigint_timestamps() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE agent_message_timing "
                "(message_id TEXT, started_at DATETIME, "
                "total_duration_ms BIGINT, stages JSON)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="agent_message_timing.started_at",
    ):
        create_schema(engine)

    columns = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("agent_message_timing")
    }
    assert not isinstance(columns["started_at"], BigInteger)
