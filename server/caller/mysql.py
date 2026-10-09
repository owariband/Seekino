from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from ..database import DatabaseEnvironment, create_session_factory


def create_database_engine(
    database_url: str,
    *,
    environment: DatabaseEnvironment | str,
    pool_recycle_seconds: int = 3600,
) -> Engine:
    url = _validated_database_url(database_url, DatabaseEnvironment(environment))
    return _create_engine(url, pool_recycle_seconds=pool_recycle_seconds)


def create_admin_database_engine(
    database_url: str,
    *,
    environment: DatabaseEnvironment | str,
    pool_recycle_seconds: int = 3600,
) -> Engine:
    selected = DatabaseEnvironment(environment)
    if selected is DatabaseEnvironment.PRODUCTION:
        raise ValueError("database administration is not allowed in production")
    url = _validated_database_url(database_url, selected)
    if url.get_backend_name() != "mysql":
        raise ValueError("database administration requires a MySQL database URL")
    return _create_engine(
        url.set(database=""), pool_recycle_seconds=pool_recycle_seconds,
        connect_args={"connect_timeout": 5},
    )


def close_database_engine(engine: Engine) -> None:
    engine.dispose()


def build_database_storage(
    database_url: str, *, environment: DatabaseEnvironment,
    pool_recycle_seconds: int,
) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_database_engine(
        database_url, environment=environment,
        pool_recycle_seconds=pool_recycle_seconds,
    )
    try:
        return engine, create_session_factory(engine)
    except BaseException:
        close_database_engine(engine)
        raise


@contextmanager
def open_database_storage(
    database_url: str, *, environment: DatabaseEnvironment,
    pool_recycle_seconds: int,
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine, session_factory = build_database_storage(
        database_url, environment=environment,
        pool_recycle_seconds=pool_recycle_seconds,
    )
    try:
        yield engine, session_factory
    finally:
        close_database_engine(engine)


def _validated_database_url(database_url: str, environment: DatabaseEnvironment) -> URL:
    url = make_url(database_url)
    _validate_database_url(url.drivername, url.database, environment)
    if url.get_backend_name() == "mysql":
        charset = url.query.get("charset")
        if charset is None:
            url = url.update_query_dict({"charset": "utf8mb4"})
        elif not isinstance(charset, str) or charset.casefold() != "utf8mb4":
            raise ValueError("MySQL connection charset must be utf8mb4")
    return url


def _create_engine(
    url: URL, *, pool_recycle_seconds: int,
    connect_args: dict[str, object] | None = None,
) -> Engine:
    options = {
        "pool_pre_ping": True,
        "pool_recycle": pool_recycle_seconds,
    }
    if connect_args is not None:
        options["connect_args"] = connect_args
    engine = create_engine(url, **options)
    try:
        if url.get_backend_name() == "mysql":
            event.listen(engine, "connect", _set_mysql_session_timezone_utc)
        return engine
    except BaseException:
        close_database_engine(engine)
        raise


def _set_mysql_session_timezone_utc(
    dbapi_connection: object,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("SET time_zone = '+00:00'")
    finally:
        cursor.close()


def _validate_database_url(
    drivername: str,
    database_name: str | None,
    environment: DatabaseEnvironment,
) -> None:
    backend = drivername.split("+", 1)[0]
    if backend == "sqlite":
        if environment is not DatabaseEnvironment.TEST:
            raise ValueError("SQLite is only allowed for isolated tests")
        return
    if backend != "mysql":
        raise ValueError("anivag persistence requires a MySQL database URL")
    if not database_name:
        raise ValueError("database URL must select a database")

    normalized = database_name.casefold()
    is_test = normalized.endswith("_test")
    is_development = normalized.endswith(("_dev", "_development"))
    if environment is DatabaseEnvironment.TEST and not is_test:
        raise ValueError("test database name must end with '_test'")
    if environment is DatabaseEnvironment.DEVELOPMENT and not is_development:
        raise ValueError(
            "development database name must end with '_dev' or '_development'"
        )
    if environment is DatabaseEnvironment.PRODUCTION and (is_test or is_development):
        raise ValueError("production cannot use a test or development database")
