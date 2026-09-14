from __future__ import annotations

import argparse
import os

import uvicorn


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
        default=os.getenv("ANIVAG_LOG_LEVEL", "info"),
    )
    args = parser.parse_args()
    uvicorn.run(
        "server.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
