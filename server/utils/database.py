from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


@contextmanager
def serialized_transaction(
    session_factory: sessionmaker[Session], name: str,
) -> Iterator[Session | None]:
    """Keep a database-scoped MySQL mutex through commit, on one connection."""
    with session_factory() as probe:
        engine = probe.get_bind()
    if engine.dialect.name != "mysql":
        # SQLite is used by sequential unit tests, not capacity competition tests.
        with session_factory.begin() as session:
            yield session
        return

    lock_name = sha256(f"{engine.url.database}:{name}".encode()).hexdigest()
    with engine.connect() as connection:
        acquired = False
        try:
            result = connection.scalar(
                text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name},
            )
            acquired = result == 1
            connection.commit()
            if result == 0:
                yield None
                return
            if not acquired:
                raise RuntimeError("MySQL could not acquire the transaction mutex")
            with session_factory(bind=connection) as session:
                with session.begin():
                    yield session
        except BaseException:
            if not acquired:
                # A lost acquisition response may still have taken the lock.
                connection.invalidate()
            raise
        finally:
            # Commit/rollback does not release GET_LOCK. Never pool a connection
            # while it still owns the mutex, including after an operation fails.
            if acquired:
                try:
                    connection.rollback()
                    connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                    connection.commit()
                except BaseException:
                    connection.invalidate()
                    raise
