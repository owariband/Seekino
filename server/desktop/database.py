"""Open one application-managed SQLite library for the lifetime of its desktop host."""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
import fcntl
from pathlib import Path

from sqlalchemy import Engine, URL, create_engine, event

from ..database import SchemaUpgradeRequiredError, create_schema
from ..utils.database import SQLiteSessionFactory

_SCHEMA_VERSION = 1
_APPLICATION_ID = int.from_bytes(b"SKNO", "big")


@dataclass
class DesktopDatabase:
    engine: Engine
    session_factory: SQLiteSessionFactory
    _resources: ExitStack

    def close(self) -> None:
        self._resources.close()


def open_desktop_database(database_path: Path) -> DesktopDatabase:
    """Exclusively own a file-backed library until its consumers have stopped."""
    path = database_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources:
        # Never unlink the inode: a second opener must always lock the same file.
        lock = resources.enter_context(path.with_name(path.name + ".lock").open("ab"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Seekino library is already open: {path}") from exc
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(path)), connect_args={"timeout": 5.0},
        )
        resources.callback(engine.dispose)
        event.listen(engine, "connect", _configure_connection)
        _initialize_library(engine)
        factory = SQLiteSessionFactory(engine, expire_on_commit=False)
        return DesktopDatabase(engine, factory, resources.pop_all())


def _configure_connection(dbapi_connection, _record) -> None:
    # SQLAlchemy controls BEGIN, including read snapshots and transactional DDL.
    dbapi_connection.isolation_level = None
    with closing(dbapi_connection.cursor()) as cursor:
        cursor.execute("PRAGMA foreign_keys = ON")


def _initialize_library(engine: Engine) -> None:
    # journal_mode cannot be changed inside a transaction. Check ownership before
    # changing it so opening an unrelated or newer database leaves it untouched.
    with engine.connect() as connection:
        version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        application_id = connection.exec_driver_sql("PRAGMA application_id").scalar_one()
        if version == 0 and application_id == 0:
            if connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
            ).first():
                raise SchemaUpgradeRequiredError("cannot open an unversioned database as a Seekino library")
        elif application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
            raise SchemaUpgradeRequiredError(f"unsupported Seekino desktop database (schema version {version})")
        if connection.exec_driver_sql("PRAGMA journal_mode = WAL").scalar_one() != "wal":
            raise RuntimeError("Seekino desktop database requires SQLite WAL")

    @event.listens_for(engine, "begin")
    def begin_transaction(connection):
        immediate = connection.get_execution_options().get("sqlite_begin_immediate", False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    if version == 0:
        with engine.connect().execution_options(sqlite_begin_immediate=True) as connection, connection.begin():
            create_schema(connection)
            connection.exec_driver_sql(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.exec_driver_sql(f"PRAGMA user_version = {_SCHEMA_VERSION}")
