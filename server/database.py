from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class DatabaseEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Base(DeclarativeBase):
    pass


def create_database_engine(
    database_url: str,
    *,
    environment: DatabaseEnvironment | str,
) -> Engine:
    environment = DatabaseEnvironment(environment)
    url = make_url(database_url)
    _validate_database_url(url.drivername, url.database, environment)
    if url.get_backend_name() == "mysql":
        charset = url.query.get("charset")
        if charset is None:
            url = url.update_query_dict({"charset": "utf8mb4"})
        elif not isinstance(charset, str) or charset.casefold() != "utf8mb4":
            raise ValueError("MySQL connection charset must be utf8mb4")
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    # Importing registers every table on Base.metadata without making the
    # connection module depend on concrete repositories.
    from .model import index_sync as _index_sync_models  # noqa: F401
    from .model import video as _video_models  # noqa: F401

    Base.metadata.create_all(engine)


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
        raise ValueError("AniVAG persistence requires a MySQL database URL")
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
