from __future__ import annotations

import argparse
import logging

from .config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalogwatch",
        description="WorkBuddy multi-account pool gateway and console.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API and pool scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8788)
    serve.add_argument("--db", help="override the SQLite database path")
    serve.add_argument("--log-level", default="info")
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    settings = load_settings(args.db)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
