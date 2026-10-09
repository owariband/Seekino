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
from sqlalchemy.dialects.mysql import TIMESTAMP
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
    "user": {"created_at", "updated_at"},
    "tenant": {"created_at", "updated_at"},
    "agent_conversation": {"last_message_at", "created_at", "updated_at"},
    "agent_message": {"created_at", "updated_at"},
    "context_snapshot": {"created_at"},
    "index_sync_intent": {"available_at", "synced_at", "created_at", "updated_at"},
    "series": {"created_at", "updated_at"},
    "series_parse_job": {
        "available_at",
        "lease_expires_at",
        "heartbeat_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "video_chunk_checkpoint": {"updated_at"},
    "video_chunk_embedding": {"created_at", "updated_at"},
    "video_chunk_info": {"parsed_at"},
}

_UTC_TIMESTAMP_COLUMNS = {
    "video": {"created_at", "updated_at"},
    "video_chunk": {"created_at", "updated_at"},
    "upload_session": {
        "completion_started_at",
        "expires_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "upload_segment": {"created_at"},
    "parse_usage_attempt": {"started_at"},
    "video_parse_usage_attempt": {"started_at"},
    "parse_model_call": {"started_at", "created_at", "updated_at"},
    "model_usage": {"started_at", "created_at"},
    "video_parse_task": {
        "next_execute_time",
        "lease_expire_time",
        "heartbeat_time",
        "start_time",
        "finish_time",
        "create_time",
        "update_time",
    },
    "chunk_parse_task": {
        "next_execute_time",
        "lease_expire_time",
        "start_time",
        "finish_time",
        "create_time",
        "update_time",
    },
    "chunk_parse_content": {"create_time", "update_time"},
    "interval_parse_task": {
        "next_execute_time",
        "lease_expire_time",
        "start_time",
        "finish_time",
        "create_time",
        "update_time",
    },
    "interval_parse_content": {"create_time", "update_time"},
}

_V2_TASK_COLUMNS = {
    "video_parse_task": {
        "id",
        "video_id",
        "pipeline_version",
        "status",
        "parse_config",
        "chunk_count",
        "version",
        "next_execute_time",
        "attempt_count",
        "max_attempts",
        "lease_owner",
        "lease_version",
        "lease_expire_time",
        "heartbeat_time",
        "start_time",
        "finish_time",
        "processing_duration_ms",
        "processing_timing_complete",
        "last_error",
        "is_test",
        "is_deleted",
        "detail",
        "create_time",
        "update_time",
    },
    "chunk_parse_task": {
        "id",
        "video_parse_task_id",
        "video_chunk_id",
        "status",
        "next_execute_time",
        "retry_count",
        "lease_owner",
        "lease_version",
        "lease_expire_time",
        "start_time",
        "finish_time",
        "last_error",
        "detail",
        "create_time",
        "update_time",
    },
    "chunk_parse_content": {
        "id",
        "chunk_parse_task_id",
        "content",
        "version",
        "detail",
        "create_time",
        "update_time",
    },
    "interval_parse_task": {
        "id",
        "chunk_parse_task_id",
        "video_parse_task_id",
        "chunk_parse_task_ids",
        "start_sequence_no",
        "chunk_count",
        "ready_mask",
        "status",
        "next_execute_time",
        "retry_count",
        "lease_owner",
        "lease_version",
        "lease_expire_time",
        "start_time",
        "finish_time",
        "last_error",
        "create_time",
        "update_time",
    },
    "interval_parse_content": {
        "interval_parse_task_id",
        "content",
        "detail",
        "create_time",
        "update_time",
    },
}

_V2_LINK_COLUMNS = {
    "upload_session": {
        "video_parse_config",
        "detail",
        "initial_video_parse_task_id",
    },
    "parse_usage_attempt": {"video_parse_task_id"},
    "parse_model_call": {"video_parse_task_id"},
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
             if column["name"] == "duration_seconds"),
            None,
        )
        if duration is not None and duration.get("comment") != "media_time_milliseconds":
            raise SchemaUpgradeRequiredError(
                "media timeline requires 20261008_video_timeline_milliseconds.sql "
                "before init-db; video.duration_seconds is not marked as milliseconds"
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

    missing_view_columns: set[str] = set()
    for table_name, required_columns in {
        "series": {"transcript_language_hint"},
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
        "video": {"duration_seconds"},
        "video_chunk": {
            "start_seconds",
            "end_seconds",
            "analysis_window_start_seconds",
            "analysis_window_end_seconds",
        },
        "video_chunk_checkpoint": {"chunk_start_seconds", "chunk_end_seconds"},
        "upload_session": {"content_sha256", "completion_stage", "version", "job_id", "last_error_code", "request_fingerprint"},
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
            missing_v2_schema.update(
                f"{table_name}.{name}"
                for name in required_columns - actual_columns
            )
    if missing_v2_schema:
        raise SchemaUpgradeRequiredError(
            "Video Parse V2 schema requires a controlled migration before init-db; missing: "
            + ", ".join(sorted(missing_v2_schema))
        )

    from .model.job import IntervalParseTask, VideoParseTask
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
            if name in {"call_id", "tenant_id", "task_type", "run_id"}:
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
    for model in (VideoParseTask, IntervalParseTask):
        table = model.__table__
        if table.name not in existing_tables:
            continue
        actual_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints(table.name)
        }
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint) or constraint.name not in {
                f"ck_{table.name}_status", f"ck_{table.name}_lease_state",
            }:
                continue
            actual = actual_checks.get(constraint.name, "")
            if _normalize_task_check(actual) != _normalize_task_check(str(constraint.sqltext)):
                incompatible_task_checks.add(f"{table.name}.{constraint.name}")
    if incompatible_task_checks:
        raise SchemaUpgradeRequiredError(
            "Video Parse V2 task indexing CHECK constraints require "
            "20260928_task_indexing_contract.sql before init-db; incompatible: "
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

    Base.metadata.create_all(engine)


def _check_identity_schema(inspector: Inspector, existing_tables: set[str]) -> None:
    from .model.tenant import Tenant
    from .model.user import User

    required = {
        "series": {"tenant_id", "is_published"},
        "upload_session": {"tenant_id", "created_by_user_id"},
        "agent_conversation": {"tenant_id", "owner_user_id"},
        "model_usage": {"tenant_id", "actor_user_id"},
        "parse_model_call": {"tenant_id"},
        "series_parse_job": {"created_by_user_id"},
        "video_parse_task": {"created_by_user_id"},
    }
    if not existing_tables.intersection({*required, "video", "user", "tenant"}):
        return
    differences = _schema_structure_differences(inspector, existing_tables, (User.__table__, Tenant.__table__))
    for table_name, columns in required.items():
        if table_name in existing_tables:
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            differences.update(f"{table_name}.{name}" for name in columns - actual)
    for table_name, expected in {
        "agent_conversation": ("tenant_id", "owner_user_id", "idempotency_key"),
        "upload_session": ("tenant_id", "created_by_user_id", "idempotency_key"),
    }.items():
        if table_name not in existing_tables:
            continue
        unique = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints(table_name)}
        unique.update(tuple(item.get("column_names") or ()) for item in inspector.get_indexes(table_name) if item.get("unique"))
        if expected not in unique:
            differences.add(f"{table_name}.identity_idempotency")
    if differences:
        raise SchemaUpgradeRequiredError(
            "user / tenant identity schema requires 20261010_user_tenant_identity.sql before init-db; incompatible: "
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
        if actual_indexes != expected_indexes:
            differences.add(f"{table_name}.indexes")
    return differences


def _normalize_ondelete(value: object) -> str:
    normalized = str(value or "RESTRICT").replace("_", " ").upper()
    return "RESTRICT" if normalized == "NO ACTION" else normalized
