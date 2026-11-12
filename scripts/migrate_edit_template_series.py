"""Bind template storage to series; preview by default and preserve unassigned rows."""

import argparse
import json
import os

from sqlalchemy import inspect, text

from server.caller.mysql import create_database_engine
from server.database import DatabaseEnvironment


def migrate(engine, *, apply=False, writers_stopped=False):
    if apply and not writers_stopped:
        raise ValueError("stop old writers and pass --writers-stopped before applying")
    inspector = inspect(engine)
    if not inspector.has_table("edit_template"):
        return {"apply": apply, "add_series_id": False, "unassigned_templates": 0}
    columns = {column["name"] for column in inspector.get_columns("edit_template")}
    missing = "series_id" not in columns
    with engine.connect() as connection:
        predicate = "" if missing else " WHERE series_id IS NULL"
        unassigned = connection.scalar(text("SELECT COUNT(*) FROM edit_template" + predicate))
    plan = {"apply": apply, "add_series_id": missing, "unassigned_templates": unassigned}
    if not apply:
        return plan
    if engine.dialect.name not in {"mysql", "sqlite"}:
        raise ValueError("this migration supports MySQL and SQLite")
    if missing:
        reference = " REFERENCES series(id) ON DELETE CASCADE" if engine.dialect.name == "sqlite" else ""
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE edit_template ADD COLUMN series_id VARCHAR(36) NULL" + reference))
    inspector = inspect(engine)
    if engine.dialect.name == "mysql" and not any(
        foreign["constrained_columns"] == ["series_id"] for foreign in inspector.get_foreign_keys("edit_template")
    ):
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE edit_template ADD CONSTRAINT fk_edit_template_series "
                                    "FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE"))
    if not any(index["column_names"] == ["series_id"] for index in inspect(engine).get_indexes("edit_template")):
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX ix_edit_template_series_id ON edit_template (series_id)"))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    engine = create_database_engine(
        os.environ["SEEKINO_DATABASE_URL"],
        environment=DatabaseEnvironment(os.environ.get("SEEKINO_ENVIRONMENT", "development")),
    )
    try:
        print(json.dumps(migrate(engine, apply=args.apply, writers_stopped=args.writers_stopped)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
