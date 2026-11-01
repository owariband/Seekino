"""Retire V1 Job/Checkpoint storage; preview by default, no model execution.

Back up the database and stop writers before --apply --writers-stopped.
Old Job records and their usage are removed; Video and V2 results are preserved.
MySQL DDL commits independently, so every step inspects the current schema.
"""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import CheckConstraint, inspect
from sqlalchemy.schema import CreateColumn

from server.caller.mysql import create_database_engine
from server.database import _normalize_task_check
from server.model.parse_usage import ParseModelCall


RETIRED_TABLES = ("video_chunk_checkpoint", "parse_usage_attempt", "series_parse_job")
LINK_TABLES = ("upload_session", "parse_model_call")
IDENTITY_COLUMNS = ("video_task_id", "execution_kind", "execution_task_id")


def _columns(connection, table):
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _count(connection, table, where=""):
    name = connection.dialect.identifier_preparer.quote(table)
    return int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {name} {where}").scalar_one())


def _preview(connection):
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    legacy_tables = {name: _count(connection, name) for name in RETIRED_TABLES if name in tables}
    links = {
        name: _count(connection, name, "WHERE job_id IS NOT NULL")
        for name in LINK_TABLES if name in tables and "job_id" in _columns(connection, name)
    }
    unexpected = []
    for table in tables:
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] not in RETIRED_TABLES or table in RETIRED_TABLES:
                continue
            if table not in LINK_TABLES or fk["constrained_columns"] != ["job_id"]:
                unexpected.append(f"{table}.{fk['name']}")
    if unexpected:
        raise ValueError("unexpected foreign keys to retired tables: " + ", ".join(sorted(unexpected)))
    current_identity = True
    if "parse_model_call" in tables:
        columns = _columns(connection, "parse_model_call")
        if not set(IDENTITY_COLUMNS).issubset(columns):
            raise ValueError("upgrade current V2 usage identity before retiring V1 models")
        has_job = "job_id" in columns
        if has_job and _count(connection, "parse_model_call", "WHERE job_id IS NOT NULL AND video_task_id IS NOT NULL"):
            raise ValueError("ambiguous usage rows reference both a legacy Job and a V2 task")
        current_rows = "job_id IS NULL AND " if has_job else ""
        invalid = _count(connection, "parse_model_call", "WHERE " + current_rows + "("
                         "video_task_id IS NULL OR execution_kind IS NULL OR execution_task_id IS NULL OR "
                         "execution_kind NOT IN ('video', 'chunk', 'interval', 'prefix'))")
        if invalid:
            raise ValueError(f"{invalid} V2 usage rows have incomplete execution identity; no data changed")
        checks = {c['name']: c['sqltext'] for c in inspector.get_check_constraints('parse_model_call')}
        expected_kind = next(c for c in ParseModelCall.__table__.constraints
                             if isinstance(c, CheckConstraint) and c.name == 'ck_parse_model_call_execution_kind')
        current_identity = (
            not any(columns[name]['nullable'] for name in IDENTITY_COLUMNS)
            and 'ck_parse_model_call_execution_identity' not in checks
            and _normalize_task_check(checks.get(expected_kind.name, ''))
            == _normalize_task_check(str(expected_kind.sqltext))
        )
    return {"retired_tables": legacy_tables, "legacy_links": links,
            "current_usage_identity": current_identity}


def migrate(engine, *, apply=False, writers_stopped=False):
    if engine.dialect.name != "mysql":
        raise ValueError("this migration requires MySQL; initialize new SQLite tests with create_schema")
    if apply and not writers_stopped:
        raise ValueError("stop writers and pass --writers-stopped before applying")
    with engine.connect() as connection:
        before = _preview(connection)
        if not apply:
            return {"apply": False, **before}
        if not before['retired_tables'] and not before['legacy_links'] and before['current_usage_identity']:
            return {"apply": True, "before": before, "after": before}
        quote = connection.dialect.identifier_preparer.quote
        tables = set(inspect(connection).get_table_names())
        # Only V1 call rows are removed. No Video, V2 task or result row is updated.
        if "parse_model_call" in tables and "job_id" in _columns(connection, "parse_model_call"):
            connection.exec_driver_sql("DELETE FROM parse_model_call WHERE job_id IS NOT NULL")
            connection.commit()
        for table in LINK_TABLES:
            if table not in tables or "job_id" not in _columns(connection, table):
                continue
            inspector = inspect(connection)
            for constraint in inspector.get_check_constraints(table):
                if "job_id" in constraint.get("sqltext", ""):
                    connection.exec_driver_sql(f"ALTER TABLE {quote(table)} DROP CHECK {quote(constraint['name'])}")
            for fk in inspector.get_foreign_keys(table):
                if "job_id" in fk["constrained_columns"]:
                    connection.exec_driver_sql(f"ALTER TABLE {quote(table)} DROP FOREIGN KEY {quote(fk['name'])}")
            for index in inspect(connection).get_indexes(table):
                if "job_id" in index.get("column_names", ()):
                    connection.exec_driver_sql(f"ALTER TABLE {quote(table)} DROP INDEX {quote(index['name'])}")
            connection.exec_driver_sql(f"ALTER TABLE {quote(table)} DROP COLUMN job_id")
            connection.commit()
        if "parse_model_call" in tables:
            # Drop the former dual-mode check and rebuild the current kind check.
            for constraint in inspect(connection).get_check_constraints("parse_model_call"):
                if constraint["name"] in {"ck_parse_model_call_execution_identity", "ck_parse_model_call_execution_kind"}:
                    connection.exec_driver_sql(f"ALTER TABLE parse_model_call DROP CHECK {quote(constraint['name'])}")
            for name in IDENTITY_COLUMNS:
                if _columns(connection, "parse_model_call")[name]["nullable"]:
                    column = str(CreateColumn(ParseModelCall.__table__.c[name]).compile(dialect=connection.dialect))
                    connection.exec_driver_sql(f"ALTER TABLE parse_model_call MODIFY COLUMN {column}")
            kind_check = next(c for c in ParseModelCall.__table__.constraints
                              if isinstance(c, CheckConstraint) and c.name == "ck_parse_model_call_execution_kind")
            connection.exec_driver_sql(
                f"ALTER TABLE parse_model_call ADD CONSTRAINT {quote(kind_check.name)} CHECK ({kind_check.sqltext})"
            )
            connection.commit()
        for table in RETIRED_TABLES:
            if inspect(connection).has_table(table):
                connection.exec_driver_sql(f"DROP TABLE {quote(table)}")
                connection.commit()
        after = _preview(connection)
        assert not after["retired_tables"] and not after["legacy_links"] and after['current_usage_identity']
        return {"apply": True, "before": before, "after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    engine = create_database_engine(os.environ["SEEKINO_DATABASE_URL"],
                                    environment=os.environ.get("SEEKINO_ENVIRONMENT", "development"))
    try:
        print(json.dumps(migrate(engine, apply=args.apply, writers_stopped=args.writers_stopped), indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
