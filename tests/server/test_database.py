from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import re

from sqlalchemy import BigInteger, CheckConstraint, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
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
from server.model.parse_usage import (
    ParseModelCallRecord,
    ParseUsageAttemptRecord,
    VideoParseUsageAttemptRecord,
)
from server.model.upload import UploadSegmentRecord, UploadSessionRecord
from server.model.video import (
    SeriesRecord,
    UTC_TIMESTAMP,
    VideoChunkInfoRecord,
    VideoChunkRecord,
    VideoRecord,
)
from server.model.job import (
    ChunkParseContentRecord,
    ChunkParseTaskRecord,
    IntervalParseContentRecord,
    IntervalParseTaskRecord,
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoParseTaskRecord,
    interval_full_ready_mask,
    interval_ready_mask_bit,
    interval_sequence_range,
    validate_interval_parse_task_members,
)


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


def test_mysql_schema_uses_innodb_json_and_integer_media_seconds() -> None:
    video_ddl = str(
        CreateTable(VideoRecord.__table__).compile(dialect=mysql.dialect())
    )
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
    assert "duration_seconds BIGINT" in video_ddl
    assert "duration_ms" not in video_ddl
    assert "start_seconds BIGINT" in chunk_ddl
    assert "all_time_events JSON" in info_ddl
    assert "video_compact LONGTEXT" in info_ddl
    assert "chunk_start_seconds BIGINT" in checkpoint_ddl
    assert "chunk_end_seconds BIGINT" in checkpoint_ddl
    assert "ck_video_chunk_checkpoint_range" in checkpoint_ddl


def test_mysql_video_parse_v2_schema_uses_timestamp_leases_and_task_scoped_content() -> None:
    video_task_ddl = str(
        CreateTable(VideoParseTaskRecord.__table__).compile(dialect=mysql.dialect())
    )
    chunk_task_ddl = str(
        CreateTable(ChunkParseTaskRecord.__table__).compile(dialect=mysql.dialect())
    )
    chunk_content_ddl = str(
        CreateTable(ChunkParseContentRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    interval_task_ddl = str(
        CreateTable(IntervalParseTaskRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    interval_content_ddl = str(
        CreateTable(IntervalParseContentRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "next_execute_time TIMESTAMP(3) NOT NULL" in video_task_ddl
    assert "lease_expire_time TIMESTAMP(3)" in video_task_ddl
    assert "uq_video_parse_task_video_version" in video_task_ddl
    assert "revision" not in video_task_ddl
    assert "ck_video_parse_task_lease_state" in video_task_ddl
    assert "uq_chunk_parse_task_parent_chunk" in chunk_task_ddl
    assert "uq_chunk_parse_content_task" in chunk_content_ddl
    assert "ck_chunk_parse_content_version" in chunk_content_ddl
    assert "chunk_parse_task_ids JSON NOT NULL" in interval_task_ddl
    assert "fk_interval_parse_task_anchor_parent" in interval_task_ddl
    assert "FOREIGN KEY(chunk_parse_task_id, video_parse_task_id)" in interval_task_ddl
    assert "PRIMARY KEY (interval_parse_task_id)" in interval_content_ddl


def test_create_schema_does_not_create_or_require_es_publication_tables() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    create_schema(engine)
    assert not {
        "es_index_build", "es_index_build_chunk", "es_video_publication",
    }.intersection(inspect(engine).get_table_names())


@pytest.mark.parametrize(
    ("status", "lease", "allowed"),
    [
        ("PREPARING", "none", True), ("PREPARING", "full", True),
        ("PARSING", "none", True), ("PARSING", "full", True),
        ("INDEXING", "none", True), ("INDEXING", "full", True),
        ("INDEXING", "partial", False),
        ("COMPLETE", "none", True), ("COMPLETE", "full", False),
        ("PARSING_FAILED", "none", True), ("PARSING_FAILED", "full", False),
        ("INDEXING_FAILED", "none", True), ("INDEXING_FAILED", "full", False),
        ("RUNNING", "none", False), ("FAILED", "none", False),
    ],
)
def test_video_task_database_enforces_status_and_lease(status, lease, allowed) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    VideoParseTaskRecord.__table__.create(engine)
    current = datetime(2026, 9, 28, tzinfo=UTC)
    values = dict(
        id="task", video_id="video", pipeline_version="v2", status=status,
        parse_config={}, chunk_count=1, version=1, max_attempts=3,
        lease_owner="worker" if lease != "none" else None,
        lease_expire_time=current if lease == "full" else None,
        heartbeat_time=current if lease == "full" else None,
    )
    with engine.begin() as connection:
        if allowed:
            connection.execute(VideoParseTaskRecord.__table__.insert().values(**values))
        else:
            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                connection.execute(VideoParseTaskRecord.__table__.insert().values(**values))


@pytest.mark.parametrize(
    ("status", "lease", "allowed"),
    [
        ("WAITING", "none", True), ("WAITING", "full", False),
        ("REFINING", "none", False), ("REFINING", "full", True),
        ("INDEXING", "none", True), ("INDEXING", "full", True),
        ("INDEXING", "partial", False),
        ("COMPLETE", "none", True), ("COMPLETE", "full", False),
        ("PARSING_FAILED", "none", True), ("PARSING_FAILED", "full", False),
        ("INDEXING_FAILED", "none", True), ("INDEXING_FAILED", "full", False),
        ("FAILED", "none", False),
    ],
)
def test_interval_task_database_enforces_status_and_lease(status, lease, allowed) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    IntervalParseTaskRecord.__table__.create(engine)
    values = dict(
        id="interval", video_parse_task_id="task", chunk_parse_task_id="chunk",
        chunk_parse_task_ids=["chunk"], start_sequence_no=0, chunk_count=1,
        status=status, lease_owner="worker" if lease != "none" else None,
        lease_expire_time=datetime(2026, 9, 28, tzinfo=UTC) if lease == "full" else None,
    )
    with engine.begin() as connection:
        if allowed:
            connection.execute(IntervalParseTaskRecord.__table__.insert().values(**values))
        else:
            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                connection.execute(IntervalParseTaskRecord.__table__.insert().values(**values))


def test_task_indexing_migration_guards_scope_and_matches_model_checks() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "docs/migrations/20260928_task_indexing_contract.sql"
    ).read_text()
    first_alter = migration.index("    ALTER TABLE video_parse_task")
    guards = migration[:first_alter]
    for table_name in (
        "video_parse_task", "chunk_parse_task", "chunk_parse_content",
        "interval_parse_task", "interval_parse_content",
        "es_index_build", "es_index_build_chunk", "es_video_publication",
    ):
        assert f"EXISTS (SELECT 1 FROM {table_name} LIMIT 1)" in guards
    assert "IF source_count <> 5" in guards
    assert "IF check_count <> 4" in guards
    assert set(re.findall(r"ALTER TABLE (\w+)", migration)) == {
        "video_parse_task", "interval_parse_task",
    }
    assert re.findall(r"DROP TABLE IF EXISTS (\w+)", migration) == [
        "es_video_publication", "es_index_build_chunk", "es_index_build",
    ]
    for model in (VideoParseTaskRecord, IntervalParseTaskRecord):
        for constraint in model.__table__.constraints:
            if not isinstance(constraint, CheckConstraint) or constraint.name not in {
                f"ck_{model.__tablename__}_status", f"ck_{model.__tablename__}_lease_state",
            }:
                continue
            match = re.search(
                rf"ADD CONSTRAINT {constraint.name} CHECK \((.*?)\n        \)",
                migration, re.DOTALL,
            )
            assert match is not None
            assert "".join(match[1].split()) == "".join(str(constraint.sqltext).split())


def test_mysql_timestamp_migration_scope_does_not_change_unrelated_models() -> None:
    video_ddl = str(
        CreateTable(VideoRecord.__table__).compile(dialect=mysql.dialect())
    )
    chunk_ddl = str(
        CreateTable(VideoChunkRecord.__table__).compile(dialect=mysql.dialect())
    )
    upload_ddl = str(
        CreateTable(UploadSessionRecord.__table__).compile(dialect=mysql.dialect())
    )
    segment_ddl = str(
        CreateTable(UploadSegmentRecord.__table__).compile(dialect=mysql.dialect())
    )
    attempt_ddl = str(
        CreateTable(ParseUsageAttemptRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    video_attempt_ddl = str(
        CreateTable(VideoParseUsageAttemptRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    call_ddl = str(
        CreateTable(ParseModelCallRecord.__table__).compile(dialect=mysql.dialect())
    )
    series_ddl = str(
        CreateTable(SeriesRecord.__table__).compile(dialect=mysql.dialect())
    )
    legacy_job_ddl = str(
        CreateTable(SeriesParseJobRecord.__table__).compile(dialect=mysql.dialect())
    )

    assert "created_at TIMESTAMP(3) NOT NULL" in video_ddl
    assert "updated_at TIMESTAMP(3) NOT NULL" in chunk_ddl
    assert "expires_at TIMESTAMP(3) NOT NULL" in upload_ddl
    assert "created_at TIMESTAMP(3) NOT NULL" in segment_ddl
    assert "started_at TIMESTAMP(3) NOT NULL" in attempt_ddl
    assert "started_at TIMESTAMP(3) NOT NULL" in video_attempt_ddl
    assert "updated_at TIMESTAMP(3) NOT NULL" in call_ddl
    assert "created_at BIGINT NOT NULL" in series_ddl
    assert "available_at BIGINT NOT NULL" in legacy_job_ddl


def test_utc_timestamp_bind_normalizes_timezone_and_millisecond_precision() -> None:
    local = datetime(
        2026,
        9,
        15,
        20,
        30,
        0,
        123_999,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert UTC_TIMESTAMP.process_bind_param(local, mysql.dialect()) == datetime(
        2026,
        9,
        15,
        12,
        30,
        0,
        123_000,
    )
    assert UTC_TIMESTAMP.process_result_value(
        datetime(2026, 9, 15, 12, 30, 0, 123_000),
        mysql.dialect(),
    ) == datetime(2026, 9, 15, 12, 30, 0, 123_000, tzinfo=UTC)


def test_video_parse_v2_migration_checks_full_timestamp3_range_and_precision() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "docs/migrations/20260927_video_parse_v2_tasks.sql"
    ).read_text()

    assert "minimum_timestamp_ms BIGINT DEFAULT 1000" in migration
    assert "maximum_timestamp_ms BIGINT DEFAULT 2147483647999" in migration
    assert "source_column_count <> 14" in migration
    assert "column_type NOT LIKE '%unsigned%'" in migration
    assert "data_type = 'timestamp' AND datetime_precision = 3" in migration
    assert "actual_timestamp_column_count <> 37" in migration


def test_interval_members_are_ordered_half_open_and_mask_bits_follow_list_order() -> None:
    members = [
        (
            ChunkParseTaskRecord(
                id=f"chunk-{sequence_no}",
                video_parse_task_id="parse-task",
            ),
            sequence_no,
        )
        for sequence_no in range(3, 6)
    ]
    member_ids = [member.id for member, _sequence_no in members]

    validate_interval_parse_task_members(
        chunk_parse_task_id="chunk-3",
        video_parse_task_id="parse-task",
        start_sequence_no=3,
        chunk_count=3,
        chunk_parse_task_ids=member_ids,
        members=members,
    )

    assert list(interval_sequence_range(3, 3)) == [3, 4, 5]
    assert [interval_ready_mask_bit(index, 3) for index in range(3)] == [1, 2, 4]
    assert interval_full_ready_mask(3) == 0b111


@pytest.mark.parametrize(
    ("member_ids", "message"),
    [
        (["chunk-3", "chunk-4"], "length"),
        (["chunk-3", "chunk-3", "chunk-5"], "unique"),
        (["chunk-4", "chunk-3", "chunk-5"], "first member"),
    ],
)
def test_interval_member_validator_rejects_invalid_identity_lists(
    member_ids: list[str],
    message: str,
) -> None:
    ordered_members = [
        (
            ChunkParseTaskRecord(
                id=f"chunk-{sequence_no}",
                video_parse_task_id="parse-task",
            ),
            sequence_no,
        )
        for sequence_no in range(3, 6)
    ]
    with pytest.raises(ValueError, match=message):
        validate_interval_parse_task_members(
            chunk_parse_task_id="chunk-3",
            video_parse_task_id="parse-task",
            start_sequence_no=3,
            chunk_count=3,
            chunk_parse_task_ids=member_ids,
            members=ordered_members,
        )


def test_interval_member_validator_rejects_parent_order_and_sequence_mismatch() -> None:
    wrong_parent = [
        (ChunkParseTaskRecord(id="chunk-3", video_parse_task_id="other"), 3),
        (ChunkParseTaskRecord(id="chunk-4", video_parse_task_id="parse-task"), 4),
        (ChunkParseTaskRecord(id="chunk-5", video_parse_task_id="parse-task"), 5),
    ]
    with pytest.raises(ValueError, match="belong"):
        validate_interval_parse_task_members(
            chunk_parse_task_id="chunk-3",
            video_parse_task_id="parse-task",
            start_sequence_no=3,
            chunk_count=3,
            chunk_parse_task_ids=["chunk-3", "chunk-4", "chunk-5"],
            members=wrong_parent,
        )

    wrong_sequence = [
        (ChunkParseTaskRecord(id="chunk-3", video_parse_task_id="parse-task"), 3),
        (ChunkParseTaskRecord(id="chunk-4", video_parse_task_id="parse-task"), 5),
        (ChunkParseTaskRecord(id="chunk-5", video_parse_task_id="parse-task"), 6),
    ]
    with pytest.raises(ValueError, match="contiguous"):
        validate_interval_parse_task_members(
            chunk_parse_task_id="chunk-3",
            video_parse_task_id="parse-task",
            start_sequence_no=3,
            chunk_count=3,
            chunk_parse_task_ids=["chunk-3", "chunk-4", "chunk-5"],
            members=wrong_sequence,
        )


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
    assert "requested_run_options JSON" in message_ddl
    assert "extra_context JSON" in message_ddl
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


def test_create_schema_requires_controlled_video_parse_v2_migration() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE interval_parse_content"))

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="Video Parse V2 schema requires a controlled migration",
    ):
        create_schema(engine)


@pytest.mark.parametrize(
    "missing_table",
    ["video_parse_usage_attempt", "parse_model_call"],
)
def test_create_schema_rejects_partial_v2_worker_runtime_without_filling_it(
    missing_table: str,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    before = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {missing_table}"))

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="Video Parse V2 worker runtime schema requires",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == before - {missing_table}


@pytest.mark.parametrize(
    ("difference", "table_name"),
    [
        ("columns", "parse_model_call"),
        ("nullable", "parse_model_call"),
        ("primary_key", "parse_model_call"),
        ("unique", "video_parse_usage_attempt"),
        ("checks", "parse_model_call"),
        ("foreign_keys", "parse_model_call"),
        ("indexes", "parse_model_call"),
    ],
)
def test_create_schema_rejects_v2_worker_runtime_structure_drift(
    monkeypatch,
    difference: str,
    table_name: str,
) -> None:
    import server.database as database

    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    inspector = inspect(engine)

    if difference in {"columns", "nullable"}:
        real_get_columns = inspector.get_columns

        def get_columns(reflected_table_name):
            columns = [
                dict(column)
                for column in real_get_columns(reflected_table_name)
            ]
            if reflected_table_name != table_name:
                return columns
            if difference == "columns":
                return [
                    column
                    for column in columns
                    if column["name"] != "execution_task_id"
                ]
            next(
                column
                for column in columns
                if column["name"] == "execution_kind"
            )["nullable"] = False
            return columns

        monkeypatch.setattr(inspector, "get_columns", get_columns)
    elif difference == "primary_key":
        real_get_primary_key = inspector.get_pk_constraint

        def get_primary_key(reflected_table_name):
            primary_key = dict(real_get_primary_key(reflected_table_name))
            if reflected_table_name == table_name:
                primary_key["constrained_columns"] = []
            return primary_key

        monkeypatch.setattr(inspector, "get_pk_constraint", get_primary_key)
    elif difference == "unique":
        real_get_unique = inspector.get_unique_constraints

        def get_unique(reflected_table_name):
            if reflected_table_name == table_name:
                return []
            return real_get_unique(reflected_table_name)

        monkeypatch.setattr(inspector, "get_unique_constraints", get_unique)
    elif difference == "checks":
        real_get_checks = inspector.get_check_constraints

        def get_checks(reflected_table_name):
            checks = real_get_checks(reflected_table_name)
            if reflected_table_name == table_name:
                return checks[1:]
            return checks

        monkeypatch.setattr(inspector, "get_check_constraints", get_checks)
    elif difference == "foreign_keys":
        real_get_foreign_keys = inspector.get_foreign_keys

        def get_foreign_keys(reflected_table_name):
            foreign_keys = [
                {**foreign_key, "options": dict(foreign_key.get("options") or {})}
                for foreign_key in real_get_foreign_keys(reflected_table_name)
            ]
            if reflected_table_name == table_name:
                target = next(
                    foreign_key
                    for foreign_key in foreign_keys
                    if foreign_key["constrained_columns"]
                    == ["video_parse_task_id"]
                )
                target["options"]["ondelete"] = "SET NULL"
            return foreign_keys

        monkeypatch.setattr(inspector, "get_foreign_keys", get_foreign_keys)
    else:
        real_get_indexes = inspector.get_indexes

        def get_indexes(reflected_table_name):
            indexes = real_get_indexes(reflected_table_name)
            if reflected_table_name != table_name:
                return indexes
            return [
                index
                for index in indexes
                if index["column_names"]
                != [
                    "video_parse_task_id",
                    "execution_kind",
                    "execution_task_id",
                    "started_at",
                ]
            ]

        monkeypatch.setattr(inspector, "get_indexes", get_indexes)

    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(
        SchemaUpgradeRequiredError,
        match=rf"{table_name}\.{difference}",
    ):
        create_schema(engine)


def test_create_schema_compares_v2_worker_runtime_structure_not_names_or_defaults(
    monkeypatch,
) -> None:
    import server.database as database

    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    inspector = inspect(engine)
    target_tables = {"video_parse_usage_attempt", "parse_model_call"}

    real_get_columns = inspector.get_columns

    def get_columns(table_name):
        columns = [dict(column) for column in real_get_columns(table_name)]
        if table_name in target_tables:
            for column in columns:
                column["default"] = "a different server default"
        return columns

    monkeypatch.setattr(inspector, "get_columns", get_columns)
    for method_name in (
        "get_unique_constraints",
        "get_check_constraints",
        "get_foreign_keys",
        "get_indexes",
    ):
        real_method = getattr(inspector, method_name)

        def renamed(table_name, _real_method=real_method, _method=method_name):
            rows = [dict(row) for row in _real_method(table_name)]
            if table_name in target_tables:
                for index, row in enumerate(rows):
                    row["name"] = f"renamed_{_method}_{index}"
            return rows

        monkeypatch.setattr(inspector, method_name, renamed)

    real_get_primary_key = inspector.get_pk_constraint

    def get_primary_key(table_name):
        primary_key = dict(real_get_primary_key(table_name))
        if table_name in target_tables:
            primary_key["name"] = f"renamed_{table_name}_primary"
        return primary_key

    monkeypatch.setattr(inspector, "get_pk_constraint", get_primary_key)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    create_schema(engine)


def test_create_schema_checks_v2_usage_started_at_as_utc_timestamp(
    monkeypatch,
) -> None:
    import server.database as database

    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    inspector = inspect(engine)
    real_get_columns = inspector.get_columns

    def get_columns(table_name):
        columns = [dict(column) for column in real_get_columns(table_name)]
        if table_name == "video_parse_usage_attempt":
            next(
                column for column in columns if column["name"] == "started_at"
            )["type"] = BigInteger()
        return columns

    monkeypatch.setattr(inspector, "get_columns", get_columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="video_parse_usage_attempt.started_at",
    ):
        create_schema(engine)


@pytest.mark.parametrize(
    ("model", "suffix", "legacy_expression"),
    [
        (VideoParseTaskRecord, "status",
         "status IN ('PENDING', 'PREPARING', 'RUNNING', 'COMPLETE', 'FAILED')"),
        (VideoParseTaskRecord, "lease_state",
         "(status IN ('PREPARING', 'RUNNING') AND lease_owner IS NOT NULL "
         "AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR "
         "(status IN ('PENDING', 'COMPLETE', 'FAILED') AND lease_owner IS NULL "
         "AND lease_expire_time IS NULL AND heartbeat_time IS NULL)"),
        (IntervalParseTaskRecord, "status",
         "status IN ('WAITING', 'REFINING', 'COMPLETE', 'FAILED')"),
        (IntervalParseTaskRecord, "lease_state",
         "(status = 'REFINING' AND lease_owner IS NOT NULL AND lease_expire_time IS NOT NULL) "
         "OR (status <> 'REFINING' AND lease_owner IS NULL AND lease_expire_time IS NULL)"),
    ],
)
def test_create_schema_rejects_old_task_checks_with_current_columns(
    model, suffix, legacy_expression,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    table = model.__table__
    constraint_name = f"ck_{table.name}_{suffix}"
    constraint = next(item for item in table.constraints if item.name == constraint_name)
    ddl = str(CreateTable(table).compile(dialect=engine.dialect))
    old_ddl = ddl.replace(str(constraint.sqltext), legacy_expression)
    assert old_ddl != ddl
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table.name}"))
        connection.execute(text(old_ddl))

    with pytest.raises(SchemaUpgradeRequiredError, match=constraint_name):
        create_schema(engine)
    reflected = inspect(engine).get_check_constraints(table.name)
    assert next(item["sqltext"] for item in reflected if item["name"] == constraint_name) == legacy_expression


def test_create_schema_accepts_mysql_check_reflection_format(monkeypatch) -> None:
    import server.database as database

    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    inspector = inspect(engine)
    real_get_checks = inspector.get_check_constraints

    def mysql_style_checks(table_name):
        checks = real_get_checks(table_name)
        for item in checks:
            expression = item["sqltext"]
            expression = re.sub(r"'([A-Z_]+)'", r"_utf8mb4'\1'", expression)
            expression = re.sub(
                r"\b(status|lease_owner|lease_expire_time|heartbeat_time)\b",
                r"(`\1`)", expression,
            )
            item["sqltext"] = f"(({expression}))"
        return checks

    monkeypatch.setattr(inspector, "get_check_constraints", mysql_style_checks)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    create_schema(engine)


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


def test_create_schema_requires_extra_context_migration_and_preserves_messages() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE agent_message ("
            "message_id TEXT, series_ids JSON, requested_series_ids JSON, "
            "phase TEXT, version INTEGER, reference JSON, requested_run_options JSON, "
            "last_error_code TEXT, last_error_message TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO agent_message (message_id, series_ids, requested_series_ids) "
            "VALUES ('existing-message', '[]', '[]')"
        ))

    with pytest.raises(SchemaUpgradeRequiredError, match="agent_message.extra_context"):
        create_schema(engine)

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT message_id FROM agent_message")
        ).scalar_one() == "existing-message"


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
        match="video_chunk_checkpoint.chunk_end_seconds",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {"video_chunk_checkpoint"}


@pytest.mark.parametrize(
    ("table_name", "legacy_columns", "required_column"),
    [
        ("video", "video_id TEXT, duration_ms BIGINT", "video.duration_seconds"),
        (
            "video_chunk",
            "video_chunk_id TEXT, start_ms BIGINT, end_ms BIGINT, "
            "analysis_window_start_ms BIGINT, analysis_window_end_ms BIGINT",
            "video_chunk.start_seconds",
        ),
    ],
)
def test_create_schema_requires_media_seconds_migration(
    table_name: str,
    legacy_columns: str,
    required_column: str,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {table_name} ({legacy_columns})"))

    with pytest.raises(SchemaUpgradeRequiredError, match=required_column):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {table_name}


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
