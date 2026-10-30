from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import yaml

from .config import ConfigError, load_settings, resolve_env
from .service import CatalogService
from .util import parse_since


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", help="path to providers.yaml")
    parser.add_argument("--db", help="override the SQLite database path")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="catalogwatch",
        description="Near-real-time free LLM model and quota catalog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="create/sync the database and print providers")

    poll = sub.add_parser("poll", parents=[common], help="poll provider model lists now")
    poll.add_argument("--provider", help="poll a single provider id")
    poll.add_argument("--quota", action="store_true", help="also check quota documentation")
    poll.add_argument("--no-notify", action="store_true", help="do not send notifications")

    quotas = sub.add_parser(
        "check-quotas", parents=[common], help="check quota documentation pages now"
    )
    quotas.add_argument("--no-notify", action="store_true")

    serve = sub.add_parser("serve", parents=[common], help="run the HTTP API and scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--log-level", default="info")

    export = sub.add_parser("export", parents=[common], help="export the catalog as JSON/YAML")
    export.add_argument("--format", choices=("json", "yaml"), default="json")
    export.add_argument("--all", action="store_true", help="include paid models too")
    export.add_argument("-o", "--output", help="write to a file instead of stdout")

    changes = sub.add_parser("changes", parents=[common], help="show recent change events")
    changes.add_argument("--since", default="24h", help="window such as 90m/24h/7d or an ISO time")
    changes.add_argument("--limit", type=int, default=100)
    changes.add_argument("--kind", help="comma separated event kinds")

    sub.add_parser("sources", parents=[common], help="show quota sources and their status")
    sub.add_parser("status", parents=[common], help="show provider status and totals")
    sub.add_parser("check-config", parents=[common], help="validate config and report missing keys")
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # One INFO line per HTTP request drowns out the actual poll results.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _cmd_init(settings) -> int:
    service = CatalogService(settings)
    try:
        print(f"config:   {settings.config_path}")
        print(f"database: {service.db.path}")
        for provider in service.db.providers():
            print(
                f"  {provider['id']:<14} {provider['name']:<20} "
                f"{provider['model_count']:>4} models "
                f"(free {provider['free_model_count']:>4}, kind={provider['free_kind']})"
            )
        print(json.dumps(service.db.stats(), indent=2))
    finally:
        service.close()
    return 0


def _cmd_poll(settings, args) -> int:
    async def run() -> int:
        service = CatalogService(settings)
        try:
            if args.provider:
                try:
                    result, events = await service.poll_provider(
                        args.provider, notify=not args.no_notify
                    )
                except KeyError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                results, event_count = [result], len(events)
            else:
                results, events = await service.poll_all(
                    force=True, notify=not args.no_notify
                )
                event_count = len(events)

            for result in results:
                print(result.line())
            print(f"{event_count} event(s)")

            if args.quota:
                quota_events = await service.check_quotas(
                    force=True, notify=not args.no_notify
                )
                for event in quota_events:
                    print(f"  quota: {event.summary}")
                print(f"{len(quota_events)} quota event(s)")
            return 0
        finally:
            service.close()

    return asyncio.run(run())


def _cmd_check_quotas(settings, args) -> int:
    async def run() -> int:
        service = CatalogService(settings)
        try:
            events = await service.check_quotas(force=True, notify=not args.no_notify)
            for event in events:
                print(f"{event.kind:<16} {event.summary}")
            print(f"{len(events)} quota event(s)")
            return 0
        finally:
            service.close()

    return asyncio.run(run())


def _cmd_serve(settings, args) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_export(settings, args) -> int:
    service = CatalogService(settings)
    try:
        data = service.db.export_catalog(free_only=not args.all)
    finally:
        service.close()

    if args.format == "yaml":
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(data, indent=2, ensure_ascii=False)

    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(text)
    return 0


def _cmd_changes(settings, args) -> int:
    service = CatalogService(settings)
    try:
        since = parse_since(args.since)
        kinds = [item.strip() for item in args.kind.split(",")] if args.kind else None
        rows = service.db.changes(since, limit=args.limit, kinds=kinds)
    finally:
        service.close()

    if not rows:
        print("no changes in this window")
        return 0
    for row in rows:
        print(f"{row['ts']}  {row['kind']:<16} {row['summary']}")
    return 0


def _cmd_sources(settings) -> int:
    service = CatalogService(settings)
    try:
        rows = service.db.quota_overview()
    finally:
        service.close()

    if not rows:
        print("no quota sources configured")
        return 0
    for row in rows:
        status = row["last_error"] or ("changed" if row["last_changed_at"] else "ok")
        checked = row["last_checked_at"] or "never"
        print(
            f"{row['id']:<28} {status:<24} checked={checked} "
            f"snapshot={'yes' if row['extracted'] else 'no'}"
        )
        print(f"  {row['url']}")
    return 0


def _cmd_status(settings) -> int:
    service = CatalogService(settings)
    try:
        print(f"database: {service.db.path}")
        for provider in service.db.providers():
            print(
                f"{provider['id']:<14} {provider['last_status'] or 'never':<12} "
                f"{provider['model_count']:>4} models/{provider['free_model_count']:>4} free  "
                f"last={provider['last_polled_at'] or 'never'}"
            )
            if provider["last_error"]:
                print(f"  error: {provider['last_error']}")
        print(json.dumps(service.db.stats(), indent=2))
    finally:
        service.close()
    return 0


def _cmd_check_config(settings) -> int:
    print(f"config: {settings.config_path}")
    print(f"database: {settings.db_path}")
    missing = 0
    for provider in settings.providers:
        key = resolve_env(provider.key_env)
        if not provider.enabled:
            state = "disabled"
        elif provider.key_required and provider.auth != "none" and not key:
            missing += 1
            state = "MISSING"
        elif key:
            state = "set"
        else:
            state = "optional/unset"
        print(
            f"  {provider.id:<14} adapter={provider.adapter:<14} "
            f"key={provider.key_env or '-':<22} {state}"
        )
        for source in provider.quota_sources:
            print(f"      quota: {source.label:<16} {source.url}")
    if missing:
        print(f"{missing} required key(s) missing; those providers will report needs_key.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, args.db)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "init":
        return _cmd_init(settings)
    if args.command == "poll":
        return _cmd_poll(settings, args)
    if args.command == "check-quotas":
        return _cmd_check_quotas(settings, args)
    if args.command == "serve":
        return _cmd_serve(settings, args)
    if args.command == "export":
        return _cmd_export(settings, args)
    if args.command == "changes":
        return _cmd_changes(settings, args)
    if args.command == "sources":
        return _cmd_sources(settings)
    if args.command == "status":
        return _cmd_status(settings)
    if args.command == "check-config":
        return _cmd_check_config(settings)

    parser.error(f"unknown command: {args.command}")
    return 2
