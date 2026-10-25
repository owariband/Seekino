from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine, make_url

from gateway.agent_model.deepagents import DeepAgentSessionStore


def create_agent_checkpoint_store(
    database_url: str, *, setup: bool = False, engine: Engine | None = None,
) -> DeepAgentSessionStore:
    """Create the application-owned MySQL saver without implicit schema migration."""
    url = engine.url if engine is not None else make_url(database_url)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "pymysql":
        raise ValueError("Deep Agents checkpoints require a mysql+pymysql database URL")
    if not url.database:
        raise ValueError("Deep Agents checkpoints require a selected database")
    charset = url.query.get("charset", "utf8mb4")
    if charset.casefold() != "utf8mb4":
        raise ValueError("Deep Agents checkpoints require utf8mb4")

    import pymysql
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    connection = None
    try:
        if engine is not None:
            # PyMySQLSaver accepts a connection factory. Checking out per cursor
            # preserves the existing pool's pre-ping and recycle behavior.
            saver = PyMySQLSaver(engine.raw_connection)
        else:
            options: dict[str, Any] = {
                "host": url.host or "localhost",
                "user": url.username,
                "password": url.password or "",
                "database": url.database,
                "port": url.port or 3306,
                "charset": charset,
                "autocommit": True,
            }
            if unix_socket := url.query.get("unix_socket"):
                options["unix_socket"] = unix_socket
            connection = pymysql.connect(**options)
            saver = PyMySQLSaver(connection)
        if setup:
            saver.setup()
        return DeepAgentSessionStore(
            saver, on_close=connection.close if connection is not None else None,
        )
    except BaseException:
        if connection is not None:
            connection.close()
        raise


__all__ = ["create_agent_checkpoint_store"]
