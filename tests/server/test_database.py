from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import re

from sqlalchemy import BigInteger, CheckConstraint, JSON, String, inspect, text
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
    AgentConversation,
    AgentMessage,
)
from server.model.parse_usage import (
    ModelUsage,
    ParseModelCall,
    ParseUsageAttempt,
    VideoParseUsageAttempt,
)
from server.model.upload import UploadSegment, UploadSession
from server.model.video import (
    Series,
    SeriesVisibility,
    UTC_TIMESTAMP,
    VideoChunkInfo,
    VideoChunk,
    Video,
)
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    SeriesParseJob,
    VideoChunkCheckpoint,
    VideoParseTask,
    interval_full_ready_mask,
    interval_ready_mask_bit,
    interval_sequence_range,
    validate_interval_parse_task_members,
)
from server.utils.time import datetime_to_unix_time_ms


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


def test_model_usage_mysql_ddl_has_identity_and_common_audit_columns() -> None:
    from sqlalchemy.schema import CreateIndex

    table = ModelUsage.__table__
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    assert tuple(table.columns.keys()) == (
        "id", "tenant_id", "user_id", "task_type", "run_id", "start_time",
        "usage", "metadata", "create_time", "create_date", "update_time", "update_date",
    )
    nullable = {"user_id", "create_time", "create_date", "update_time", "update_date"}
    assert {column.name for column in table.columns if column.nullable} == nullable
    assert not table.foreign_keys
    assert "start_time TIMESTAMP(3) NOT NULL" in ddl
    assert "create_time BIGINT" in ddl
    assert "create_date DATETIME(3)" in ddl
    assert "`usage` JSON NOT NULL" in ddl and "metadata JSON NOT NULL" in ddl
    assert "COLLATE utf8mb4_bin" in ddl and "ENGINE=InnoDB" in ddl
    assert "ck_model_usage_task_type" in ddl
    indexes = {str(CreateIndex(index).compile(dialect=mysql.dialect())) for index in table.indexes}
    assert indexes == {
        "CREATE INDEX ix_model_usage_tenant_started ON model_usage (tenant_id, start_time, id)",
        "CREATE INDEX ix_model_usage_tenant_run_started ON model_usage (tenant_id, run_id, start_time)",
        "CREATE INDEX ix_model_usage_tenant_actor_started ON model_usage (tenant_id, user_id, start_time)",
        *{
            f"CREATE INDEX ix_model_usage_{name} ON model_usage ({name})"
            for name in ("create_time", "create_date", "update_time", "update_date")
        },
    }


def test_create_schema_registers_twenty_four_tables_and_identity_is_repeatable() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    create_schema(engine)
    tables = inspect(engine).get_table_names()
    assert len(tables) == 24 and {"user", "tenant"}.issubset(tables)


def test_conversation_detail_is_nullable_json() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    detail = next(column for column in inspect(engine).get_columns("agent_conversation")
                  if column["name"] == "detail")
    assert detail["nullable"] is True
    assert isinstance(detail["type"], JSON)
    ddl = str(CreateTable(AgentConversation.__table__).compile(dialect=mysql.dialect()))
    assert "detail JSON" in ddl and "detail JSON NOT NULL" not in ddl


def test_message_attachments_are_nullable_json() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    column = next(column for column in inspect(engine).get_columns("agent_message")
                  if column["name"] == "attachments")
    assert column["nullable"] is True
    assert isinstance(column["type"], JSON)
    ddl = str(CreateTable(AgentMessage.__table__).compile(dialect=mysql.dialect()))
    assert "attachments JSON" in ddl and "attachments JSON NOT NULL" not in ddl


def test_attachment_schema_upgrade_preserves_existing_messages() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(AgentConversation.__table__.insert().values(
            id="existing-conversation", tenant_id="local-default", title="Existing conversation",
            idempotency_key="existing-conversation", next_message_sequence=1,
        ))
        connection.execute(AgentMessage.__table__.insert().values(
            id="existing-message", conversation_id="existing-conversation", sequence_no=0,
            role="USER", status="COMPLETED", content="原始历史消息", series_ids=[],
            detail={"request": {"idempotencyKey": "existing-key"}},
            create_time=1000, update_time=2000,
        ))
        connection.execute(text("ALTER TABLE agent_message DROP COLUMN attachments"))
        before = dict(connection.execute(text("SELECT * FROM agent_message")).mappings().one())

    with pytest.raises(SchemaUpgradeRequiredError, match="20261016_agent_message_attachments.sql"):
        create_schema(engine)

    assert "attachments" not in {column["name"] for column in inspect(engine).get_columns("agent_message")}
    with engine.begin() as connection:
        assert dict(connection.execute(text("SELECT * FROM agent_message")).mappings().one()) == before
        connection.execute(text("ALTER TABLE agent_message ADD COLUMN attachments JSON NULL"))
    create_schema(engine)
    create_schema(engine)
    with engine.connect() as connection:
        after = dict(connection.execute(text("SELECT * FROM agent_message")).mappings().one())
    assert after.pop("attachments") is None
    assert after == before


@pytest.mark.parametrize("difference", ["type", "nullable"])
def test_attachment_schema_rejects_incompatible_existing_column(monkeypatch, difference) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    columns = inspector.get_columns

    def reflected_columns(table_name):
        result = [dict(column) for column in columns(table_name)]
        if table_name == "agent_message":
            attachment = next(column for column in result if column["name"] == "attachments")
            attachment[difference] = String(255) if difference == "type" else False
        return result

    monkeypatch.setattr(inspector, "get_columns", reflected_columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match="agent_message.attachments requires nullable JSON"):
        create_schema(engine)


def test_create_schema_requires_conversation_detail_migration_without_changing_rows() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(AgentConversation.__table__.insert().values(
            id="existing-conversation", tenant_id="local-default", title="Existing conversation",
            title_is_auto=False, idempotency_key="existing-conversation", next_message_sequence=0,
        ))
        connection.execute(text("ALTER TABLE agent_conversation DROP COLUMN detail"))

    with pytest.raises(SchemaUpgradeRequiredError, match="20261010_agent_conversation_detail.sql"):
        create_schema(engine)

    assert "detail" not in {column["name"] for column in inspect(engine).get_columns("agent_conversation")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, title FROM agent_conversation")).all() == [
            ("existing-conversation", "Existing conversation"),
        ]


@pytest.mark.parametrize("column_name", ["create_time", "create_date", "update_time", "update_date"])
@pytest.mark.parametrize("difference", ["missing", "type", "nullable", "index"])
def test_create_schema_rejects_common_audit_drift_without_mutating_rows(
    monkeypatch, column_name: str, difference: str,
) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(Series.__table__.insert().values(
            id="kept", tenant_id="scope", name="kept", status="PENDING",
        ))
    inspector = inspect(engine)
    before_tables = inspector.get_table_names()
    original_columns = inspector.get_columns
    original_indexes = inspector.get_indexes

    def columns(table_name):
        result = [dict(column) for column in original_columns(table_name)]
        if table_name == "series":
            if difference == "missing":
                return [column for column in result if column["name"] != column_name]
            target = next(column for column in result if column["name"] == column_name)
            if difference == "type":
                target["type"] = String(64)
            elif difference == "nullable":
                target["nullable"] = False
        return result

    def indexes(table_name):
        result = original_indexes(table_name)
        if table_name == "series" and difference == "index":
            return [index for index in result if index["column_names"] != [column_name]]
        return result

    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(inspector, "get_indexes", indexes)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match=rf"series\.{column_name}\.{difference}"):
        create_schema(engine)
    assert inspect(engine).get_table_names() == before_tables
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, name FROM series")).one() == ("kept", "kept")


@pytest.mark.parametrize("reflected_type", [mysql.DATETIME(fsp=0), mysql.DATETIME(), mysql.TIMESTAMP(fsp=3)])
def test_mysql_schema_rejects_audit_dates_without_datetime3(monkeypatch, reflected_type) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    Series.__table__.create(engine)
    inspector = inspect(engine)
    original_columns = inspector.get_columns

    def columns(table_name):
        result = [dict(column) for column in original_columns(table_name)]
        for column in result:
            if column["name"] == "create_date":
                column["type"] = reflected_type
            elif column["name"] == "update_date":
                column["type"] = mysql.DATETIME(fsp=3)
        return result

    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    with pytest.raises(SchemaUpgradeRequiredError, match=r"series\.create_date\.type"):
        create_schema(engine)
    assert inspect(engine).get_table_names() == ["series"]


def test_create_schema_rejects_old_twenty_two_tables_without_adding_identity() -> None:
    from server.model.tenant import Tenant
    from server.model.user import User

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    Tenant.__table__.drop(engine)
    User.__table__.drop(engine)
    assert len(inspect(engine).get_table_names()) == 22
    with pytest.raises(SchemaUpgradeRequiredError, match="20261010_user_tenant_identity.sql"):
        create_schema(engine)
    assert len(inspect(engine).get_table_names()) == 22


def test_create_schema_requires_new_migration_for_existing_identity_profile_drift(monkeypatch) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    original_columns = inspector.get_columns
    before_tables = inspector.get_table_names()

    def columns(table_name):
        result = original_columns(table_name)
        return [column for column in result if table_name != "user" or column["name"] != "nickname"]

    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(
        SchemaUpgradeRequiredError,
        match=r"20261010_base_model_field_names.sql.*user\.columns",
    ):
        create_schema(engine)
    assert inspect(engine).get_table_names() == before_tables


@pytest.mark.parametrize("table_name", ["series", "upload_session", "agent_conversation", "model_usage", "parse_model_call"])
def test_create_schema_rejects_legacy_workspace_scope_before_mutation(table_name) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {table_name} (workspace_id VARCHAR(64))"))
        connection.execute(text(f"INSERT INTO {table_name} VALUES ('local-default')"))
    with pytest.raises(SchemaUpgradeRequiredError, match="20261010_user_tenant_identity.sql"):
        create_schema(engine)
    assert inspect(engine).get_table_names() == [table_name]
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT workspace_id FROM {table_name}")).scalar_one() == "local-default"


@pytest.mark.parametrize("table_name,column_name", [
    ("series", "tenant_id"), ("agent_conversation", "owner_id"),
    ("upload_session", "creator_id"), ("series_parse_job", "creator_id"),
    ("video_parse_task", "creator_id"),
])
def test_create_schema_requires_identity_business_columns(monkeypatch, table_name, column_name) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    columns = inspector.get_columns
    monkeypatch.setattr(inspector, "get_columns", lambda table: [column for column in columns(table) if not (table == table_name and column["name"] == column_name)])
    monkeypatch.setattr(database, "inspect", lambda _: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match=column_name):
        create_schema(engine)


def test_series_visibility_defaults_private_and_rejects_unknown_values() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO series (id, tenant_id, name, status, character_bible) "
            "VALUES ('private-series', 'local-default', 'Private', 'EMPTY', '[]')"
        ))
        assert connection.execute(text("SELECT visibility FROM series")).scalar_one() == SeriesVisibility.PRIVATE
        connection.execute(text("UPDATE series SET visibility = 'public' WHERE id = 'private-series'"))
        with pytest.raises(IntegrityError, match="ck_series_visibility"):
            connection.execute(text("UPDATE series SET visibility = 'team' WHERE id = 'private-series'"))
    ddl = str(CreateTable(Series.__table__).compile(dialect=mysql.dialect()))
    assert "visibility VARCHAR(16) NOT NULL DEFAULT 'private'" in ddl
    assert "ck_series_visibility CHECK (visibility IN ('private', 'public'))" in ddl
    assert "is_published" not in ddl
    assert ("visibility", "create_time", "id") in {
        tuple(index.columns.keys()) for index in Series.__table__.indexes
    }


def test_create_schema_rejects_legacy_publication_without_changing_rows() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    ddl = str(CreateTable(Series.__table__).compile(dialect=engine.dialect))
    legacy_ddl = ddl.replace(
        "visibility VARCHAR(16) DEFAULT 'private' NOT NULL",
        "is_published BOOLEAN DEFAULT 0 NOT NULL",
    ).replace(
        ", \n\tCONSTRAINT ck_series_visibility CHECK (visibility IN ('private', 'public'))", "",
    )
    assert "visibility" not in legacy_ddl
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE series"))
        connection.execute(text(legacy_ddl))
        for index in Series.__table__.indexes:
            if "visibility" not in index.columns.keys():
                index.create(connection)
        connection.execute(text(
            "INSERT INTO series (id, tenant_id, name, status, character_bible, is_published) VALUES "
            "('private-series', 'local-default', 'Private', 'EMPTY', '[]', 0), "
            "('public-series', 'platform-public', 'Public', 'READY', '[]', 1)"
        ))
        before = connection.execute(text("SELECT * FROM series ORDER BY id")).all()

    with pytest.raises(SchemaUpgradeRequiredError, match="20261019_series_visibility.sql.*series.is_published"):
        create_schema(engine)

    assert "visibility" not in {column["name"] for column in inspect(engine).get_columns("series")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM series ORDER BY id")).all() == before


@pytest.mark.parametrize("difference", ["missing", "type", "nullable", "default", "check", "index"])
def test_create_schema_requires_visibility_contract(monkeypatch, difference: str) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    if difference in {"missing", "type", "nullable", "default"}:
        real_get_columns = inspector.get_columns

        def columns(table_name):
            result = [dict(column) for column in real_get_columns(table_name)]
            if table_name != "series":
                return result
            if difference == "missing":
                return [column for column in result if column["name"] != "visibility"]
            visibility = next(column for column in result if column["name"] == "visibility")
            visibility[difference] = {"type": String(255), "nullable": True, "default": "'public'"}[difference]
            return result

        monkeypatch.setattr(inspector, "get_columns", columns)
    elif difference == "check":
        real_get_checks = inspector.get_check_constraints
        monkeypatch.setattr(inspector, "get_check_constraints", lambda table: [] if table == "series" else real_get_checks(table))
    else:
        real_get_indexes = inspector.get_indexes
        monkeypatch.setattr(inspector, "get_indexes", lambda table: [
            index for index in real_get_indexes(table)
            if not (table == "series" and index["column_names"] == ["visibility", "create_time", "id"])
        ])
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match=rf"20261019_series_visibility.sql.*series.visibility.{difference}"):
        create_schema(engine)


def test_create_schema_requires_owner_in_conversation_idempotency(monkeypatch) -> None:
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    unique = inspector.get_unique_constraints

    def old_unique(table):
        values = [dict(item) for item in unique(table)]
        if table == "agent_conversation":
            for item in values:
                item["column_names"] = [column for column in item["column_names"] if column != "owner_id"]
        return values

    monkeypatch.setattr(inspector, "get_unique_constraints", old_unique)
    monkeypatch.setattr(database, "inspect", lambda _: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match="identity_idempotency"):
        create_schema(engine)


def test_create_schema_adds_model_usage_to_existing_current_schema() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    ModelUsage.__table__.drop(engine)
    create_schema(engine)
    assert "model_usage" in inspect(engine).get_table_names()
    create_schema(engine)


def test_create_schema_requires_upload_snapshot_migration():
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    assert "parse_config" in UploadSession.__table__.columns
    assert "video_parse_config" not in UploadSession.__table__.columns
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE upload_session ADD COLUMN video_parse_config JSON"))
    with pytest.raises(SchemaUpgradeRequiredError, match="retire upload_session.video_parse_config"):
        create_schema(engine)
    engine.dispose()


@pytest.mark.parametrize("column_name", ["start_time", "create_time", "usage", "run_id"])
def test_create_schema_rejects_model_usage_type_drift(monkeypatch, column_name) -> None:
    import server.database as database
    from sqlalchemy import String

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    inspector = inspect(engine)
    original = inspector.get_columns

    def columns(table_name):
        result = [dict(column) for column in original(table_name)]
        if table_name == "model_usage":
            next(column for column in result if column["name"] == column_name)["type"] = String(8)
        return result

    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match=f"model_usage.{column_name}"):
        create_schema(engine)


def test_model_usage_database_rejects_unsupported_task_type() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    ModelUsage.__table__.create(engine)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(ModelUsage.__table__.insert().values(
                id="00000000-0000-0000-0000-000000000001", tenant_id="w",
                task_type="retrieval", run_id="r", start_time=datetime.now(UTC),
                usage={}, metadata={}, create_time=datetime_to_unix_time_ms(datetime.now(UTC)),
            ))


@pytest.mark.parametrize("column_override", [False, True])
def test_existing_model_usage_rejects_case_insensitive_mysql_collation(monkeypatch, column_override):
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    ModelUsage.__table__.create(engine)
    inspector = inspect(engine)
    original = inspector.get_columns

    def columns(table_name):
        result = [dict(column) for column in original(table_name)]
        for column in result:
            if column["name"] in {"create_date", "update_date"}:
                column["type"] = mysql.DATETIME(fsp=3)
            elif column["name"] == "start_time":
                column["type"] = mysql.TIMESTAMP(fsp=3)
        if table_name == "model_usage" and column_override:
            next(column for column in result if column["name"] == "tenant_id")["type"] = mysql.VARCHAR(64, collation="utf8mb4_general_ci")
        return result

    monkeypatch.setattr(engine.dialect, "name", "mysql")
    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(inspector, "get_table_options", lambda _table: {
        "mysql_collate": "utf8mb4_bin" if column_override else "utf8mb4_general_ci",
    })
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match="model_usage.*collation"):
        create_schema(engine)


def test_mysql_schema_uses_innodb_json_and_integer_media_milliseconds() -> None:
    video_ddl = str(
        CreateTable(Video.__table__).compile(dialect=mysql.dialect())
    )
    chunk_ddl = str(
        CreateTable(VideoChunk.__table__).compile(dialect=mysql.dialect())
    )
    info_ddl = str(
        CreateTable(VideoChunkInfo.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    checkpoint_ddl = str(
        CreateTable(VideoChunkCheckpoint.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "ENGINE=InnoDB" in chunk_ddl
    assert "CHARSET=utf8mb4" in chunk_ddl
    assert "duration_ms BIGINT" in video_ddl
    assert "COMMENT 'media_time_milliseconds'" in video_ddl
    assert "duration_seconds" not in video_ddl
    assert "start_ms BIGINT" in chunk_ddl
    assert "all_time_events JSON" in info_ddl
    assert "video_compact LONGTEXT" in info_ddl
    assert "start_ms BIGINT" in checkpoint_ddl
    assert "end_ms BIGINT" in checkpoint_ddl
    assert "ck_video_chunk_checkpoint_range" in checkpoint_ddl


@pytest.mark.parametrize("old_rows", [0, 1])
def test_mysql_media_unit_guard_rejects_old_integer_schema_even_when_empty(monkeypatch, old_rows):
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE video (duration_seconds BIGINT NOT NULL)"))
        if old_rows:
            connection.execute(text("INSERT INTO video VALUES (9)"))
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    with pytest.raises(SchemaUpgradeRequiredError, match="20261008_video_timeline_milliseconds.sql"):
        create_schema(engine)
    assert inspect(engine).get_table_names() == ["video"]


def test_mysql_media_unit_guard_accepts_marked_column_before_other_schema_checks(monkeypatch):
    import server.database as database

    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE video (duration_seconds BIGINT NOT NULL)"))
        connection.execute(text("CREATE TABLE video_segment (id BIGINT)"))
    inspector = inspect(engine)
    original = inspector.get_columns

    def columns(table_name):
        result = original(table_name)
        if table_name == "video":
            result[0]["comment"] = "media_time_milliseconds"
        return result

    monkeypatch.setattr(inspector, "get_columns", columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    with pytest.raises(SchemaUpgradeRequiredError, match="legacy VideoSegment"):
        create_schema(engine)


def test_mysql_video_parse_v2_schema_uses_timestamp_leases_and_task_scoped_content() -> None:
    video_task_ddl = str(
        CreateTable(VideoParseTask.__table__).compile(dialect=mysql.dialect())
    )
    chunk_task_ddl = str(
        CreateTable(ChunkParseTask.__table__).compile(dialect=mysql.dialect())
    )
    chunk_content_ddl = str(
        CreateTable(ChunkParseContent.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    interval_task_ddl = str(
        CreateTable(IntervalParseTask.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    interval_content_ddl = str(
        CreateTable(IntervalParseContent.__table__).compile(
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
    assert "chunk_task_ids JSON NOT NULL" in interval_task_ddl
    assert "fk_interval_parse_task_anchor_parent" in interval_task_ddl
    assert "FOREIGN KEY(chunk_task_id, video_task_id)" in interval_task_ddl
    assert "PRIMARY KEY (interval_task_id)" in interval_content_ddl


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
    VideoParseTask.__table__.create(engine)
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
            connection.execute(VideoParseTask.__table__.insert().values(**values))
        else:
            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                connection.execute(VideoParseTask.__table__.insert().values(**values))


@pytest.mark.parametrize(
    ("status", "lease", "allowed"),
    [
        ("WAITING", "none", True), ("WAITING", "full", False),
        ("REFINING", "none", False), ("REFINING", "full", True),
        ("INDEXING", "none", False), ("INDEXING", "full", False),
        ("INDEXING", "partial", False),
        ("COMPLETE", "none", True), ("COMPLETE", "full", False),
        ("PARSING_FAILED", "none", True), ("PARSING_FAILED", "full", False),
        ("INDEXING_FAILED", "none", False), ("INDEXING_FAILED", "full", False),
        ("FAILED", "none", False),
    ],
)
def test_interval_task_database_enforces_status_and_lease(status, lease, allowed) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    IntervalParseTask.__table__.create(engine)
    values = dict(
        id="interval", video_task_id="task", chunk_task_id="chunk",
        chunk_task_ids=["chunk"], start_sequence_no=0, chunk_count=1,
        status=status, lease_owner="worker" if lease != "none" else None,
        lease_expire_time=datetime(2026, 9, 28, tzinfo=UTC) if lease == "full" else None,
        start_time=datetime(2026, 9, 28, tzinfo=UTC) if lease == "full" else None,
    )
    with engine.begin() as connection:
        if allowed:
            connection.execute(IntervalParseTask.__table__.insert().values(**values))
        else:
            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                connection.execute(IntervalParseTask.__table__.insert().values(**values))


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
    # This historical empty-table script is deliberately not the in-place migration.
    assert "INDEXING_FAILED" in migration


def test_mysql_audit_columns_are_common_and_business_timestamp_types_are_preserved() -> None:
    video_ddl = str(
        CreateTable(Video.__table__).compile(dialect=mysql.dialect())
    )
    chunk_ddl = str(
        CreateTable(VideoChunk.__table__).compile(dialect=mysql.dialect())
    )
    upload_ddl = str(
        CreateTable(UploadSession.__table__).compile(dialect=mysql.dialect())
    )
    segment_ddl = str(
        CreateTable(UploadSegment.__table__).compile(dialect=mysql.dialect())
    )
    attempt_ddl = str(
        CreateTable(ParseUsageAttempt.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    video_attempt_ddl = str(
        CreateTable(VideoParseUsageAttempt.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    call_ddl = str(
        CreateTable(ParseModelCall.__table__).compile(dialect=mysql.dialect())
    )
    series_ddl = str(
        CreateTable(Series.__table__).compile(dialect=mysql.dialect())
    )
    legacy_job_ddl = str(
        CreateTable(SeriesParseJob.__table__).compile(dialect=mysql.dialect())
    )

    for ddl in (video_ddl, chunk_ddl, upload_ddl, segment_ddl, attempt_ddl,
                video_attempt_ddl, call_ddl, series_ddl, legacy_job_ddl):
        assert "create_time BIGINT" in ddl
        assert "update_time BIGINT" in ddl
        assert "create_date DATETIME(3)" in ddl
        assert "update_date DATETIME(3)" in ddl
    assert "expire_time TIMESTAMP(3) NOT NULL" in upload_ddl
    assert "start_time TIMESTAMP(3) NOT NULL" in attempt_ddl
    assert "start_time TIMESTAMP(3) NOT NULL" in video_attempt_ddl
    assert "start_time TIMESTAMP(3) NOT NULL" in call_ddl
    assert "next_execute_time BIGINT NOT NULL" in legacy_job_ddl


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
            ChunkParseTask(
                id=f"chunk-{sequence_no}",
                video_task_id="parse-task",
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
            ChunkParseTask(
                id=f"chunk-{sequence_no}",
                video_task_id="parse-task",
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
        (ChunkParseTask(id="chunk-3", video_task_id="other"), 3),
        (ChunkParseTask(id="chunk-4", video_task_id="parse-task"), 4),
        (ChunkParseTask(id="chunk-5", video_task_id="parse-task"), 5),
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
        (ChunkParseTask(id="chunk-3", video_task_id="parse-task"), 3),
        (ChunkParseTask(id="chunk-4", video_task_id="parse-task"), 5),
        (ChunkParseTask(id="chunk-5", video_task_id="parse-task"), 6),
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
        CreateTable(AgentConversation.__table__).compile(
            dialect=mysql.dialect()
        )
    )
    message_ddl = str(
        CreateTable(AgentMessage.__table__).compile(dialect=mysql.dialect())
    )
    assert "ENGINE=InnoDB" in conversation_ddl
    assert "tenant_id VARCHAR(64) NOT NULL" in conversation_ddl
    assert "session_id" not in conversation_ddl
    assert "series_ids" not in conversation_ddl
    assert "content LONGTEXT" in message_ddl
    assert "series_ids JSON NOT NULL" in message_ddl
    assert "requested_series_ids JSON" in message_ddl
    assert "model VARCHAR(255)" in message_ddl
    assert "search_result JSON" in message_ddl
    assert "requested_top_k INTEGER" in message_ddl
    assert "requested_run_options JSON" in message_ddl
    assert "extra_context JSON" in message_ddl
    assert "detail JSON" in message_ddl
    assert "ON DELETE CASCADE" in message_ddl


def test_agent_message_detail_migration_matches_supported_empty_legacy_schema() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "docs/migrations/20260927_agent_message_detail_request.sql"
    ).read_text()
    first_alter = migration.index("ALTER TABLE agent_message")
    guards = migration[:first_alter]

    assert "IF EXISTS (SELECT 1 FROM agent_message LIMIT 1)" in guards
    assert "timing_table_count <> 1" in guards
    assert "IF EXISTS (SELECT 1 FROM agent_message_timing LIMIT 1)" in guards
    assert "detail_column_count <> 0" in guards
    assert "ADD COLUMN detail JSON NULL AFTER extra_context" in migration
    assert migration.index("ADD COLUMN detail JSON") < migration.index(
        "DROP COLUMN idempotency_key"
    )
    assert migration.index("DROP TABLE agent_message_timing") < migration.index(
        "CREATE UNIQUE INDEX uq_agent_message_conversation_idempotency"
    )


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
                f"(conversation_id TEXT, tenant_id TEXT{scope_column})"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_conversation (conversation_id, tenant_id) "
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


def test_create_schema_accepts_current_message_detail_schema_repeatedly() -> None:
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
    assert {"series_ids", "requested_series_ids", "detail"} <= message_columns
    assert not {"idempotency_key", "reply_to_message_id", "retrieval_query"} & message_columns
    assert "agent_message_timing" not in inspector.get_table_names()


@pytest.mark.parametrize(
    "column_name",
    ["idempotency_key", "reply_to_message_id", "retrieval_query"],
)
def test_create_schema_rejects_legacy_agent_message_columns(
    column_name: str,
) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE agent_message ADD COLUMN {column_name} TEXT"))

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match=rf"agent_message\.{column_name}",
    ):
        create_schema(engine)


def test_create_schema_rejects_missing_agent_message_detail() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_agent_message_conversation_idempotency"))
        connection.execute(text("ALTER TABLE agent_message DROP COLUMN detail"))

    with pytest.raises(SchemaUpgradeRequiredError, match="agent_message missing detail"):
        create_schema(engine)

    assert "detail" not in {
        column["name"] for column in inspect(engine).get_columns("agent_message")
    }


def test_create_schema_rejects_legacy_agent_message_timing_table() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE agent_message_timing "
            "(message_id TEXT PRIMARY KEY, started_at BIGINT NOT NULL, "
            "total_duration_ms BIGINT, stages JSON)"
        ))

    with pytest.raises(SchemaUpgradeRequiredError, match="agent_message_timing"):
        create_schema(engine)

    assert "agent_message_timing" in inspect(engine).get_table_names()


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
                    == ["video_task_id"]
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
                    "video_task_id",
                    "execution_kind",
                    "execution_task_id",
                    "start_time",
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


def test_create_schema_checks_v2_usage_start_time_as_utc_timestamp(
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
                column for column in columns if column["name"] == "start_time"
            )["type"] = BigInteger()
        return columns

    monkeypatch.setattr(inspector, "get_columns", get_columns)
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)
    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="video_parse_usage_attempt.start_time",
    ):
        create_schema(engine)


@pytest.mark.parametrize(
    ("model", "suffix", "legacy_expression"),
    [
        (VideoParseTask, "status",
         "status IN ('PENDING', 'PREPARING', 'RUNNING', 'COMPLETE', 'FAILED')"),
        (VideoParseTask, "lease_state",
         "(status IN ('PREPARING', 'RUNNING') AND lease_owner IS NOT NULL "
         "AND lease_expire_time IS NOT NULL AND heartbeat_time IS NOT NULL) OR "
         "(status IN ('PENDING', 'COMPLETE', 'FAILED') AND lease_owner IS NULL "
         "AND lease_expire_time IS NULL AND heartbeat_time IS NULL)"),
        (IntervalParseTask, "status",
         "status IN ('WAITING', 'REFINING', 'COMPLETE', 'FAILED')"),
        (IntervalParseTask, "lease_state",
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
        for index in table.indexes:
            index.create(connection)

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


def test_create_schema_rejects_old_upload_field_names_and_preserves_existing_rows() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE upload_session (upload_session_id TEXT, status TEXT)"))
        connection.execute(text("INSERT INTO upload_session VALUES ('existing-upload', 'COMPLETED')"))
    with pytest.raises(SchemaUpgradeRequiredError, match="field names.*upload_session.upload_session_id"):
        create_schema(engine)
    assert set(inspect(engine).get_table_names()) == {"upload_session"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM upload_session")).scalar_one() == "COMPLETED"


def test_create_schema_rejects_old_message_fields_before_missing_extra_context() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE agent_message ("
            "message_id TEXT, series_ids JSON, requested_series_ids JSON, "
            "phase TEXT, version INTEGER, reference JSON, requested_run_options JSON, "
            "detail JSON, "
            "last_error_code TEXT, last_error_message TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO agent_message (message_id, series_ids, requested_series_ids) "
            "VALUES ('existing-message', '[]', '[]')"
        ))

    with pytest.raises(SchemaUpgradeRequiredError, match="field names.*agent_message.message_id"):
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
        match="BaseModel schema.*video_chunk_checkpoint.create_time.missing",
    ):
        create_schema(engine)

    assert set(inspect(engine).get_table_names()) == {"video_chunk_checkpoint"}


@pytest.mark.parametrize(
    ("table_name", "legacy_columns", "required_column"),
    [
        ("video", "video_id TEXT, duration_ms BIGINT", "video.video_id"),
        (
            "video_chunk",
            "video_chunk_id TEXT, start_ms BIGINT, end_ms BIGINT, "
            "analysis_window_start_ms BIGINT, analysis_window_end_ms BIGINT",
            "video_chunk.video_chunk_id",
        ),
    ],
)
def test_create_schema_rejects_legacy_media_field_names(
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


@pytest.mark.parametrize("column", ["display_name", "description", "cover_uri"])
def test_create_schema_requires_video_metadata_migration(column: str) -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE video DROP COLUMN {column}"))
    with pytest.raises(SchemaUpgradeRequiredError, match=f"video.{column}"):
        create_schema(engine)


def test_create_schema_requires_series_language_hint_migration() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE series "
                "(series_id TEXT, tenant_id TEXT, name TEXT, description TEXT)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="field names.*series.series_id",
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
                "CREATE TABLE agent_conversation "
                "(conversation_id TEXT, tenant_id TEXT, title_is_auto BOOL, "
                "last_message_at DATETIME)"
            )
        )

    with pytest.raises(
        SchemaUpgradeRequiredError,
        match="agent_conversation.last_message_at",
    ):
        create_schema(engine)

    columns = {
        column["name"]: column["type"]
        for column in inspect(engine).get_columns("agent_conversation")
    }
    assert not isinstance(columns["last_message_at"], BigInteger)
