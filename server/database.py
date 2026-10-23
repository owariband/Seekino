from __future__ import annotations

import re
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Engine,
    JSON,
    String,
    Table,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.dialects.mysql import DATETIME, TIMESTAMP
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class DatabaseEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Base(DeclarativeBase):
    pass


class SchemaUpgradeRequiredError(RuntimeError):
    pass


_UNIX_TIME_MS_COLUMNS = {
    "agent_conversation": {"create_time", "last_message_time", "update_time"},
    "agent_message": {"create_time", "update_time"},
    "chunk_parse_content": {"create_time", "update_time"},
    "chunk_parse_task": {"create_time", "update_time"},
    "context_snapshot": {"create_time", "update_time"},
    "index_sync_intent": {
        "create_time",
        "next_execute_time",
        "sync_time",
        "update_time",
    },
    "interval_parse_content": {"create_time", "update_time"},
    "interval_parse_task": {"create_time", "update_time"},
    "model_usage": {"create_time", "update_time"},
    "parse_model_call": {"create_time", "update_time"},
    "parse_usage_attempt": {"create_time", "update_time"},
    "series": {"create_time", "update_time"},
    "series_parse_job": {
        "create_time",
        "finish_time",
        "heartbeat_time",
        "lease_expire_time",
        "next_execute_time",
        "start_time",
        "update_time",
    },
    "tenant": {"create_time", "update_time"},
    "upload_segment": {"create_time", "update_time"},
    "upload_session": {"create_time", "update_time"},
    "user": {"create_time", "update_time"},
    "video": {"create_time", "update_time"},
    "video_chunk": {"create_time", "update_time"},
    "video_chunk_checkpoint": {"create_time", "update_time"},
    "video_chunk_embedding": {"create_time", "update_time"},
    "video_chunk_info": {"create_time", "parse_time", "update_time"},
    "video_parse_task": {"create_time", "update_time"},
    "video_parse_usage_attempt": {"create_time", "update_time"},
}

_UTC_TIMESTAMP_COLUMNS = {
    "chunk_parse_task": {
        "finish_time",
        "lease_expire_time",
        "next_execute_time",
        "start_time",
    },
    "interval_parse_task": {
        "finish_time",
        "lease_expire_time",
        "next_execute_time",
        "start_time",
    },
    "model_usage": {"start_time"},
    "parse_model_call": {"start_time"},
    "parse_usage_attempt": {"start_time"},
    "upload_session": {"completion_start_time", "expire_time", "finish_time"},
    "video_parse_task": {
        "finish_time",
        "heartbeat_time",
        "lease_expire_time",
        "next_execute_time",
        "start_time",
    },
    "video_parse_usage_attempt": {"start_time"},
}

_V2_TASK_COLUMNS = {
    "video_parse_task": {
        "attempt_count",
        "chunk_count",
        "create_date",
        "create_time",
        "detail",
        "finish_time",
        "heartbeat_time",
        "id",
        "is_deleted",
        "is_test",
        "last_error",
        "lease_expire_time",
        "lease_owner",
        "lease_version",
        "max_attempts",
        "next_execute_time",
        "parse_config",
        "pipeline_version",
        "processing_duration_ms",
        "processing_timing_complete",
        "start_time",
        "status",
        "update_date",
        "update_time",
        "version",
        "video_id",
    },
    "chunk_parse_task": {
        "phase", "indexed_content_version",
        "chunk_id",
        "create_date",
        "create_time",
        "detail",
        "finish_time",
        "id",
        "last_error",
        "lease_expire_time",
        "lease_owner",
        "lease_version",
        "next_execute_time",
        "retry_count",
        "start_time",
        "status",
        "update_date",
        "update_time",
        "video_task_id",
    },
    "chunk_parse_content": {
        "chunk_task_id",
        "content",
        "create_date",
        "create_time",
        "detail",
        "id",
        "update_date",
        "update_time",
        "version",
    },
    "interval_parse_task": {
        "chunk_count",
        "chunk_task_id",
        "chunk_task_ids",
        "create_date",
        "create_time",
        "finish_time",
        "id",
        "last_error",
        "lease_expire_time",
        "lease_owner",
        "lease_version",
        "next_execute_time",
        "ready_mask",
        "retry_count",
        "start_sequence_no",
        "start_time",
        "status",
        "update_date",
        "update_time",
        "video_task_id",
    },
    "interval_parse_content": {
        "content",
        "create_date",
        "create_time",
        "detail",
        "interval_task_id",
        "update_date",
        "update_time",
    },
}

_V2_LINK_COLUMNS = {
    "upload_session": {"detail", "initial_task_id", "parse_config"},
    "parse_usage_attempt": {"video_task_id"},
    "parse_model_call": {"video_task_id"},
}

_LEGACY_FIELD_NAMES = {
    "agent_conversation": {
        "conversation_id",
        "created_at",
        "last_message_at",
        "owner_user_id",
        "updated_at",
    },
    "agent_message": {"created_at", "message_id", "model_name", "updated_at"},
    "chunk_parse_content": {"chunk_parse_task_id"},
    "chunk_parse_task": {"video_chunk_id", "video_parse_task_id"},
    "context_snapshot": {"created_at", "created_from_video_chunk_id", "snapshot_id"},
    "index_sync_intent": {
        "available_at",
        "created_at",
        "synced_at",
        "updated_at",
        "video_chunk_id",
    },
    "interval_parse_content": {"interval_parse_task_id"},
    "interval_parse_task": {
        "chunk_parse_task_id",
        "chunk_parse_task_ids",
        "video_parse_task_id",
    },
    "model_usage": {"actor_user_id", "call_id", "created_at", "started_at"},
    "parse_model_call": {
        "call_id",
        "created_at",
        "started_at",
        "updated_at",
        "video_chunk_id",
        "video_parse_task_id",
    },
    "parse_usage_attempt": {"started_at", "video_parse_task_id"},
    "series": {"created_at", "series_id", "updated_at"},
    "series_parse_job": {
        "available_at",
        "completed_at",
        "created_at",
        "created_by_user_id",
        "heartbeat_at",
        "job_id",
        "lease_expires_at",
        "started_at",
        "updated_at",
    },
    "tenant": {"created_at", "owner_user_id", "tenant_id", "updated_at"},
    "upload_segment": {
        "actual_size_bytes",
        "checksum_sha256",
        "created_at",
        "upload_session_id",
    },
    "upload_session": {
        "completed_at",
        "completion_started_at",
        "content_sha256",
        "created_at",
        "created_by_user_id",
        "expires_at",
        "initial_video_parse_task_id",
        "original_filename",
        "preferred_segment_size_bytes",
        "total_segments",
        "total_size_bytes",
        "updated_at",
        "upload_session_id",
    },
    "user": {"created_at", "is_guest", "role", "updated_at", "user_id"},
    "video": {
        "content_hash",
        "created_at",
        "duration_seconds",
        "inference_proxy_uri",
        "original_filename",
        "original_source_uri",
        "total_bytes",
        "updated_at",
        "video_id",
    },
    "video_chunk": {
        "analysis_window_end_seconds",
        "analysis_window_start_seconds",
        "created_at",
        "end_seconds",
        "start_seconds",
        "updated_at",
        "video_chunk_id",
    },
    "video_chunk_checkpoint": {
        "chunk_end_seconds",
        "chunk_start_seconds",
        "updated_at",
        "video_chunk_id",
    },
    "video_chunk_embedding": {"created_at", "updated_at", "video_chunk_id"},
    "video_chunk_info": {"model_name", "model_provider", "parsed_at", "video_chunk_id"},
    "video_parse_task": {"created_by_user_id"},
    "video_parse_usage_attempt": {"attempt_id", "started_at", "video_parse_task_id"},
}


def create_database_engine(
    database_url: str,
    *,
    environment: DatabaseEnvironment | str,
    pool_recycle_seconds: int = 3600,
) -> Engine:
    from .caller.mysql import create_database_engine as create_caller_database_engine

    return create_caller_database_engine(
        database_url, environment=environment,
        pool_recycle_seconds=pool_recycle_seconds,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    # Importing registers every table on Base.metadata without making the
    # connection module depend on concrete repositories.
    from .model import agent as _agent_models  # noqa: F401
    from .model import index_sync as _index_sync_models  # noqa: F401
    from .model import job as _job_models  # noqa: F401
    from .model import parse_usage as _parse_usage_models  # noqa: F401
    from .model import upload as _upload_models  # noqa: F401
    from .model import video as _video_models  # noqa: F401
    from .model import user as _user_models  # noqa: F401
    from .model import tenant as _tenant_models  # noqa: F401

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    legacy_scope = {
        f"{table_name}.workspace_id"
        for table_name in {"series", "upload_session", "agent_conversation", "model_usage", "parse_model_call"}.intersection(existing_tables)
        if "workspace_id" in {column["name"] for column in inspector.get_columns(table_name)}
    }
    if legacy_scope:
        raise SchemaUpgradeRequiredError(
            "identity scope requires 20261010_user_tenant_identity.sql before init-db; legacy: "
            + ", ".join(sorted(legacy_scope))
        )
    if engine.dialect.name == "mysql" and "video" in existing_tables:
        duration = next(
            (column for column in inspector.get_columns("video")
             if column["name"] in {"duration_seconds", "duration_ms"}),
            None,
        )
        if duration is not None and duration.get("comment") != "media_time_milliseconds":
            raise SchemaUpgradeRequiredError(
                "media timeline requires 20261008_video_timeline_milliseconds.sql "
                "before init-db; video.duration_ms is not marked as milliseconds"
            )
    legacy_schema = existing_tables.intersection(
        {
            "video_segment",
            "video_segment_info",
            "video_segment_parse_chunk",
        }
    )
    for table_name, column_names in {
        "context_snapshot": {
            "after_segment_sequence_no",
            "created_from_video_segment_id",
        },
        "index_sync_intent": {"video_segment_id"},
    }.items():
        if table_name not in existing_tables:
            continue
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        legacy_schema.update(
            f"{table_name}.{column_name}"
            for column_name in column_names.intersection(existing_columns)
        )
    if legacy_schema:
        found = ", ".join(sorted(legacy_schema))
        raise SchemaUpgradeRequiredError(
            "legacy VideoSegment schema requires a controlled VideoChunk "
            f"upgrade before init-db; found: {found}"
        )

    legacy_agent_schema = existing_tables.intersection({"agent_session"})
    if "agent_conversation" in existing_tables:
        conversation_columns = {
            column["name"] for column in inspector.get_columns("agent_conversation")
        }
        if "session_id" in conversation_columns:
            legacy_agent_schema.add("agent_conversation.session_id")
        if "tenant_id" not in conversation_columns:
            legacy_agent_schema.add("agent_conversation missing tenant_id")
    if legacy_agent_schema:
        found = ", ".join(sorted(legacy_agent_schema))
        raise SchemaUpgradeRequiredError(
            "legacy Agent Session schema requires a controlled Workspace / "
            f"Conversation upgrade before init-db; found: {found}"
        )

    legacy_message_scope: set[str] = set()
    if "agent_conversation" in existing_tables:
        if "series_ids" in conversation_columns:
            legacy_message_scope.add("agent_conversation.series_ids")
    if "agent_message" in existing_tables:
        message_columns = {
            column["name"] for column in inspector.get_columns("agent_message")
        }
        legacy_message_scope.update(
            f"agent_message missing {column_name}"
            for column_name in {"series_ids", "requested_series_ids"} - message_columns
        )
    if legacy_message_scope:
        found = ", ".join(sorted(legacy_message_scope))
        raise SchemaUpgradeRequiredError(
            "legacy Agent scope schema requires a controlled Agent "
            f"message-scope migration before init-db; found: {found}"
        )

    legacy_message_timing: set[str] = set()
    if "agent_message_timing" in existing_tables:
        legacy_message_timing.add("agent_message_timing")
    if "agent_message" in existing_tables and "detail" not in message_columns:
        legacy_message_timing.add("agent_message missing detail")
    if legacy_message_timing:
        found = ", ".join(sorted(legacy_message_timing))
        raise SchemaUpgradeRequiredError(
            "legacy Agent message timing schema requires a controlled "
            f"migration before init-db; found: {found}"
        )

    if "agent_message" in existing_tables:
        legacy_message_columns = {
            "idempotency_key", "reply_to_message_id", "retrieval_query",
        }.intersection(message_columns)
        if legacy_message_columns:
            found = ", ".join(
                f"agent_message.{name}" for name in sorted(legacy_message_columns)
            )
            raise SchemaUpgradeRequiredError(
                "legacy Agent message columns require a controlled migration "
                f"before init-db; found: {found}"
            )
        if engine.dialect.name == "mysql":
            idempotency_index = next(
                (
                    index
                    for index in inspector.get_indexes("agent_message")
                    if index["name"] == "uq_agent_message_conversation_idempotency"
                ),
                None,
            )
            if idempotency_index is None or not idempotency_index["unique"]:
                raise SchemaUpgradeRequiredError(
                    "agent_message requires the unique detail request "
                    "idempotency index before init-db"
                )

    legacy_fields: set[str] = set()
    for table_name in existing_tables.intersection(_LEGACY_FIELD_NAMES):
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        legacy_fields.update(
            f"{table_name}.{name}" for name in actual.intersection(_LEGACY_FIELD_NAMES[table_name])
        )
    if legacy_fields:
        raise SchemaUpgradeRequiredError(
            "field names require 20261010_base_model_field_names.sql before init-db; legacy: "
            + ", ".join(sorted(legacy_fields))
        )
    _check_base_model_schema(inspector, existing_tables, engine.dialect.name)

    if "agent_message" in existing_tables:
        attachment_column = next((column for column in inspector.get_columns("agent_message")
                                  if column["name"] == "attachments"), None)
        if (
            attachment_column is None
            or not isinstance(attachment_column["type"], JSON)
            or not attachment_column["nullable"]
        ):
            raise SchemaUpgradeRequiredError(
                "agent_message.attachments requires nullable JSON; apply "
                "20261016_agent_message_attachments.sql before init-db"
            )

    missing_view_columns: set[str] = set()
    for table_name, required_columns in {
        "series": {"transcript_language_hint", "display_name"},
        "agent_conversation": {"title_is_auto"},
        "agent_message": {
            "phase", "version", "reference", "requested_run_options",
            "last_error_code", "last_error_message", "extra_context",
        },
        "series_parse_job": {
            "version",
            "processing_duration_ms",
            "processing_timing_complete",
        },
        "video": {"duration_ms", "display_name", "description", "cover_uri"},
        "video_chunk": {
            "start_ms",
            "end_ms",
            "window_start_ms",
            "window_end_ms",
        },
        "video_chunk_checkpoint": {"start_ms", "end_ms"},
        "upload_session": {"sha256", "completion_stage", "version", "job_id", "last_error_code", "request_fingerprint"},
    }.items():
        if table_name not in existing_tables:
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing_view_columns.update(f"{table_name}.{name}" for name in required_columns - actual_columns)
    if missing_view_columns:
        raise SchemaUpgradeRequiredError(
            "UI view schema requires a controlled migration before init-db; missing: "
            + ", ".join(sorted(missing_view_columns))
        )

    has_existing_core_schema = bool(
        existing_tables.intersection(
            {"series", "video", "upload_session", "series_parse_job"}
        )
    )
    missing_v2_schema: set[str] = set()
    if has_existing_core_schema:
        for table_name, required_columns in _V2_TASK_COLUMNS.items():
            if table_name not in existing_tables:
                missing_v2_schema.add(table_name)
                continue
            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            missing_v2_schema.update(
                f"{table_name}.{name}"
                for name in required_columns - actual_columns
            )
        for table_name, required_columns in _V2_LINK_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            if table_name == "upload_session" and "video_parse_config" in actual_columns:
                raise SchemaUpgradeRequiredError(
                    "Upload parse configuration requires the controlled migration "
                    "scripts/migrate_video_parse_chunk_tasks.py before startup; "
                    "retire upload_session.video_parse_config after preserving its snapshot"
                )
            missing_v2_schema.update(
                f"{table_name}.{name}"
                for name in required_columns - actual_columns
            )
    if missing_v2_schema:
        raise SchemaUpgradeRequiredError(
            "Video Parse V2 schema requires a controlled migration before init-db; missing: "
            + ", ".join(sorted(missing_v2_schema))
        )

    from .model.job import ChunkTask, IntervalParseTask, VideoParseTask
    from .model.parse_usage import (
        ModelUsage,
        ParseModelCall,
        VideoParseUsageAttempt,
    )

    worker_runtime_tables = (
        VideoParseUsageAttempt.__table__,
        ParseModelCall.__table__,
    )
    if has_existing_core_schema or existing_tables.intersection(
        table.name for table in worker_runtime_tables
    ):
        incompatible_worker_runtime = _schema_structure_differences(
            inspector,
            existing_tables,
            worker_runtime_tables,
        )
        if incompatible_worker_runtime:
            raise SchemaUpgradeRequiredError(
                "Video Parse V2 worker runtime schema requires "
                "20260928_v2_worker_runtime.sql before init-db; incompatible: "
                + ", ".join(sorted(incompatible_worker_runtime))
            )

    if ModelUsage.__tablename__ in existing_tables:
        table = ModelUsage.__table__
        incompatible_usage = _schema_structure_differences(
            inspector, existing_tables, (table,),
        )
        if engine.dialect.name == "mysql":
            options = inspector.get_table_options(table.name)
            if options.get("mysql_collate") != "utf8mb4_bin":
                incompatible_usage.add(f"{table.name}.collation")
        for column in inspector.get_columns(table.name):
            name, column_type = column["name"], column["type"]
            if name in {"id", "tenant_id", "task_type", "run_id"}:
                expected = table.c[name].type
                if not isinstance(column_type, String) or column_type.length != expected.length:
                    incompatible_usage.add(f"{table.name}.{name}.type")
                if engine.dialect.name == "mysql" and getattr(
                    column_type, "collation", None,
                ) not in {None, "utf8mb4_bin"}:
                    incompatible_usage.add(f"{table.name}.{name}.collation")
            elif name in {"usage", "metadata"} and not isinstance(column_type, JSON):
                incompatible_usage.add(f"{table.name}.{name}.type")
        if incompatible_usage:
            raise SchemaUpgradeRequiredError(
                "model_usage schema requires a controlled upgrade before init-db; incompatible: "
                + ", ".join(sorted(incompatible_usage))
            )

    incompatible_task_checks: set[str] = set()
    for model in (VideoParseTask, ChunkTask, IntervalParseTask):
        table = model.__table__
        if table.name not in existing_tables:
            continue
        actual_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints(table.name)
        }
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            actual = actual_checks.get(constraint.name, "")
            if _normalize_task_check(actual) != _normalize_task_check(str(constraint.sqltext)):
                incompatible_task_checks.add(f"{table.name}.{constraint.name}")
    if incompatible_task_checks:
        raise SchemaUpgradeRequiredError(
            "Video Parse V2 task indexing CHECK constraints require "
            "scripts/migrate_video_parse_chunk_tasks.py before init-db; incompatible: "
            + ", ".join(sorted(incompatible_task_checks))
        )

    legacy_timestamp_columns: set[str] = set()
    for table_name, timestamp_columns in _UNIX_TIME_MS_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        for column in inspector.get_columns(table_name):
            column_type = column["type"]
            if (
                column["name"] in timestamp_columns
                and (
                    not isinstance(column_type, BigInteger)
                    or bool(getattr(column_type, "unsigned", False))
                )
            ):
                legacy_timestamp_columns.add(f"{table_name}.{column['name']}")
    if legacy_timestamp_columns:
        found = ", ".join(sorted(legacy_timestamp_columns))
        raise SchemaUpgradeRequiredError(
            "absolute timestamps require the controlled signed-BIGINT "
            "Unix-millisecond "
            f"BIGINT migration before init-db; found: {found}"
        )

    invalid_utc_timestamp_columns: set[str] = set()
    for table_name, timestamp_columns in _UTC_TIMESTAMP_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        for column in inspector.get_columns(table_name):
            if column["name"] not in timestamp_columns:
                continue
            column_type = column["type"]
            is_timestamp = isinstance(column_type, DateTime)
            if engine.dialect.name == "mysql":
                is_timestamp = (
                    isinstance(column_type, TIMESTAMP)
                    and getattr(column_type, "fsp", None) == 3
                )
            if not is_timestamp:
                invalid_utc_timestamp_columns.add(
                    f"{table_name}.{column['name']}"
                )
    if invalid_utc_timestamp_columns:
        found = ", ".join(sorted(invalid_utc_timestamp_columns))
        raise SchemaUpgradeRequiredError(
            "absolute timestamps in the Video/Upload/Parse V2 scope require "
            f"TIMESTAMP(3) UTC migration before init-db; found: {found}"
        )

    _check_identity_schema(inspector, existing_tables)
    _check_series_visibility_schema(inspector, existing_tables)

    task_tables = tuple(model.__table__ for model in (VideoParseTask, ChunkTask, IntervalParseTask)
                        if model.__tablename__ in existing_tables)
    task_differences = {
        item for item in _schema_structure_differences(inspector, existing_tables, task_tables)
        if not item.endswith(".checks")  # Compared above with MySQL-aware task normalization.
    }
    if task_differences:
        raise SchemaUpgradeRequiredError(
            "Video Parse V2 task schema requires scripts/migrate_video_parse_chunk_tasks.py; incompatible: "
            + ", ".join(sorted(task_differences))
        )

    if "agent_conversation" in existing_tables and "detail" not in conversation_columns:
        raise SchemaUpgradeRequiredError(
            "agent_conversation missing detail; apply "
            "20261010_agent_conversation_detail.sql before init-db"
        )

    Base.metadata.create_all(engine)


def _check_base_model_schema(inspector: Inspector, existing_tables: set[str], dialect_name: str) -> None:
    differences: set[str] = set()
    for table_name in existing_tables.intersection(Base.metadata.tables):
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        indexes = {
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name) if not index.get("unique")
        }
        for name in ("create_time", "create_date", "update_time", "update_date"):
            column = columns.get(name)
            if column is None:
                differences.add(f"{table_name}.{name}.missing")
                continue
            value_type = column["type"]
            if name.endswith("_time"):
                valid = isinstance(value_type, BigInteger) and not getattr(value_type, "unsigned", False)
            else:
                valid = isinstance(value_type, DateTime)
                if dialect_name == "mysql":
                    valid = isinstance(value_type, DATETIME) and value_type.fsp == 3
            if not valid:
                differences.add(f"{table_name}.{name}.type")
            if not column["nullable"]:
                differences.add(f"{table_name}.{name}.nullable")
            if (name,) not in indexes:
                differences.add(f"{table_name}.{name}.index")
    if differences:
        raise SchemaUpgradeRequiredError(
            "BaseModel schema requires 20261010_base_model_field_names.sql before init-db; incompatible: "
            + ", ".join(sorted(differences))
        )


def _check_identity_schema(inspector: Inspector, existing_tables: set[str]) -> None:
    from .model.tenant import Tenant
    from .model.user import User

    required = {
        "series": {"tenant_id"},
        "upload_session": {"tenant_id", "creator_id"},
        "agent_conversation": {"tenant_id", "owner_id"},
        "model_usage": {"tenant_id", "user_id"},
        "parse_model_call": {"tenant_id"},
        "series_parse_job": {"creator_id"},
        "video_parse_task": {"creator_id"},
    }
    if not existing_tables.intersection({*required, "video", "user", "tenant"}):
        return
    differences = _schema_structure_differences(inspector, existing_tables, (User.__table__, Tenant.__table__))
    for table_name, columns in required.items():
        if table_name in existing_tables:
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            differences.update(f"{table_name}.{name}" for name in columns - actual)
    for table_name, expected in {
        "agent_conversation": ("tenant_id", "owner_id", "idempotency_key"),
        "upload_session": ("tenant_id", "creator_id", "idempotency_key"),
    }.items():
        if table_name not in existing_tables:
            continue
        unique = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints(table_name)}
        unique.update(tuple(item.get("column_names") or ()) for item in inspector.get_indexes(table_name) if item.get("unique"))
        if expected not in unique:
            differences.add(f"{table_name}.identity_idempotency")
    if differences:
        migration = (
            "20261010_base_model_field_names.sql"
            if {"user", "tenant"}.issubset(existing_tables)
            else "20261010_user_tenant_identity.sql"
        )
        raise SchemaUpgradeRequiredError(
            f"user / tenant identity schema requires {migration} before init-db; incompatible: "
            + ", ".join(sorted(differences))
        )


def _check_series_visibility_schema(inspector: Inspector, existing_tables: set[str]) -> None:
    if "series" not in existing_tables:
        return
    from .model.video import Series, SeriesVisibility

    columns = {column["name"]: column for column in inspector.get_columns("series")}
    visibility = columns.get("visibility")
    differences: set[str] = set()
    if "is_published" in columns:
        differences.add("series.is_published")
    if visibility is None:
        differences.add("series.visibility.missing")
    else:
        if not isinstance(visibility["type"], String) or visibility["type"].length != 16:
            differences.add("series.visibility.type")
        if visibility["nullable"]:
            differences.add("series.visibility.nullable")
        if str(visibility["default"]).strip("'\"") != SeriesVisibility.PRIVATE:
            differences.add("series.visibility.default")
    expected_check = next(constraint for constraint in Series.__table__.constraints
                          if constraint.name == "ck_series_visibility")
    checks = {_normalize_task_check(str(item.get("sqltext") or ""))
              for item in inspector.get_check_constraints("series")}
    if _normalize_task_check(str(expected_check.sqltext)) not in checks:
        differences.add("series.visibility.check")
    indexes = {tuple(item.get("column_names") or ()) for item in inspector.get_indexes("series")
               if not item.get("unique")}
    if ("visibility", "create_time", "id") not in indexes:
        differences.add("series.visibility.index")
    if differences:
        raise SchemaUpgradeRequiredError(
            "Series visibility requires 20261019_series_visibility.sql before init-db; incompatible: "
            + ", ".join(sorted(differences))
        )


def _normalize_task_check(expression: str) -> str:
    # MySQL reflection adds identifier quotes, character-set introducers and
    # parentheses around operands in these status/lease expressions.
    expression = re.sub(r"(?<![\w'])_[a-zA-Z0-9]+(?=')", "", expression)
    return re.sub(r"[\s`()]+", "", expression).casefold()


def _schema_structure_differences(
    inspector: Inspector,
    existing_tables: set[str],
    tables: tuple[Table, ...],
) -> set[str]:
    differences: set[str] = set()
    for table in tables:
        table_name = table.name
        if table_name not in existing_tables:
            differences.add(table_name)
            continue

        reflected_columns = inspector.get_columns(table_name)
        actual_columns = {
            column["name"]: bool(column["nullable"])
            for column in reflected_columns
        }
        expected_columns = {
            column.name: bool(column.nullable)
            for column in table.columns
        }
        if actual_columns.keys() != expected_columns.keys():
            differences.add(f"{table_name}.columns")
        elif actual_columns != expected_columns:
            differences.add(f"{table_name}.nullable")

        actual_primary_key = tuple(
            inspector.get_pk_constraint(table_name).get(
                "constrained_columns"
            ) or ()
        )
        expected_primary_key = tuple(table.primary_key.columns.keys())
        if actual_primary_key != expected_primary_key:
            differences.add(f"{table_name}.primary_key")

        reflected_indexes = inspector.get_indexes(table_name)
        actual_unique = {
            tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        actual_unique.update(
            tuple(index.get("column_names") or ())
            for index in reflected_indexes
            if bool(index.get("unique"))
        )
        expected_unique = {
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected_unique.update(
            tuple(index.columns.keys())
            for index in table.indexes
            if index.unique
        )
        if actual_unique != expected_unique:
            differences.add(f"{table_name}.unique")

        actual_checks = {
            _normalize_task_check(str(constraint.get("sqltext") or ""))
            for constraint in inspector.get_check_constraints(table_name)
        }
        expected_checks = {
            _normalize_task_check(str(constraint.sqltext))
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        if actual_checks != expected_checks:
            differences.add(f"{table_name}.checks")

        actual_foreign_keys = {
            (
                tuple(constraint.get("constrained_columns") or ()),
                constraint.get("referred_table"),
                tuple(constraint.get("referred_columns") or ()),
                _normalize_ondelete(
                    (constraint.get("options") or {}).get("ondelete")
                ),
            )
            for constraint in inspector.get_foreign_keys(table_name)
        }
        expected_foreign_keys = {
            (
                tuple(constraint.columns.keys()),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                _normalize_ondelete(constraint.ondelete),
            )
            for constraint in table.foreign_key_constraints
        }
        if actual_foreign_keys != expected_foreign_keys:
            differences.add(f"{table_name}.foreign_keys")

        actual_indexes = {
            tuple(index.get("column_names") or ())
            for index in reflected_indexes
            if not bool(index.get("unique"))
        }
        expected_indexes = {
            tuple(index.columns.keys())
            for index in table.indexes
            if not index.unique
        }
        fk_support = {tuple(constraint.columns.keys()) for constraint in table.foreign_key_constraints}
        # InnoDB may create exactly these supporting indexes when a foreign key
        # has no declared index prefix. Their presence is engine-managed.
        if expected_indexes - actual_indexes or (actual_indexes - expected_indexes) - fk_support:
            differences.add(f"{table_name}.indexes")
    return differences


def _normalize_ondelete(value: object) -> str:
    normalized = str(value or "RESTRICT").replace("_", " ").upper()
    return "RESTRICT" if normalized == "NO ACTION" else normalized
