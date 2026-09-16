from __future__ import annotations

import argparse
import os
from dataclasses import replace

import uvicorn

from ..utils.logging import configure_logging
from ..config import ServerSettings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m server.api",
        description="Run the AniVAG HTTP API process.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("ANIVAG_HTTP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("ANIVAG_HTTP_PORT", "9621")),
    )
    parser.add_argument(
        "--log-level",
        type=str.casefold,
        default=os.getenv("ANIVAG_LOG_LEVEL", "info"),
    )
    args = parser.parse_args()
    settings = ServerSettings.from_environment()
    configure_logging("api", environment=settings.environment, config=replace(settings.logging, level=args.log_level.casefold()))
    uvicorn.run(
        "server.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
