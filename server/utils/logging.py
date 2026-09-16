from __future__ import annotations

import logging
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config.logging import LoggingConfig
from ..database import DatabaseEnvironment

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_context.get()
        return True


def configure_logging(
    process_name: str,
    *,
    environment: DatabaseEnvironment | str,
    config: LoggingConfig,
) -> Path:
    if process_name not in {"api", "mq"}:
        raise ValueError("log process name must be api or mq")
    directory = config.directory.expanduser().resolve() / DatabaseEnvironment(environment).value
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{process_name}.log"
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            log_path, maxBytes=config.maximum_file_size_bytes,
            backupCount=config.backup_count, encoding="utf-8",
        ),
    ]
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    for handler in handlers:
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=5 if config.level == "trace" else config.level.upper(),
        handlers=handlers,
        force=True,
    )
    return log_path
