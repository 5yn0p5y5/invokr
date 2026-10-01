"""Entry point for the invokr server: ``python run.py``."""

from __future__ import annotations

import argparse

import uvicorn

from invokr.config import CONFIG


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the invokr server.")
    parser.add_argument("--host", default=CONFIG.host, help="bind address")
    parser.add_argument("--port", type=int, default=CONFIG.port, help="bind port")
    parser.add_argument(
        "--reload", action="store_true", help="auto-reload on source changes"
    )
    args = parser.parse_args()

    uvicorn.run(
        "invokr.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
