from __future__ import annotations

import argparse

from server.config import ServerSettings
from server.main import init_application
from server.utils.logging import configure_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.manual.server_mq_once",
        description="Run at most one due seekino parse job for diagnostics.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one due job and then exit.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.once:
        parser.error("pass --once to run the single-job diagnostic")
    settings = ServerSettings.from_environment()
    configure_logging("mq", environment=settings.environment, config=settings.logging)
    runtime = init_application(settings)
    services = runtime.services
    try:
        if services.parse_consumer is None:
            raise RuntimeError("parse consumer is not configured")
        run_once_sync = getattr(services.parse_consumer, "run_once_sync", None)
        if run_once_sync is not None:
            run_once_sync()
        else:
            services.parse_consumer.run_once()
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
