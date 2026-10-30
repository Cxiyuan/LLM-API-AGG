#!/opt/venv/bin/python
"""Headless WorkBuddy login helper.

Starts Buddy2api's browser/device authorization flow, prints the URL, then polls
until the account is stored. Run it inside the container:

    docker exec -it <container> workbuddy-login --realm cn

The admin token is read from CB_GATEWAY_ADMIN_TOKEN or /data/secrets/admin_token.
No token or credential is printed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _admin_token() -> str:
    token = os.environ.get("CB_GATEWAY_ADMIN_TOKEN", "").strip()
    if token:
        return token
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return _read(data_dir / "secrets" / "admin_token")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Log in to WorkBuddy through Buddy2api")
    parser.add_argument("--realm", choices=("cn", "global"), default="cn")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787"),
    )
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument(
        "--catalog-url",
        default=os.environ.get("CATALOGWATCH_URL", "http://127.0.0.1:8788"),
        help="CatalogWatch base URL, used for a best-effort refresh after login",
    )
    args = parser.parse_args(argv)

    token = _admin_token()
    if not token:
        print(
            "admin token not found: set CB_GATEWAY_ADMIN_TOKEN or mount /data/secrets/admin_token",
            file=sys.stderr,
        )
        return 2

    headers = {"Authorization": f"Bearer {token}"}
    base_url = args.base_url.rstrip("/")

    try:
        with httpx.Client(base_url=base_url, timeout=30.0, headers=headers) as client:
            response = client.post(
                "/admin/workbuddy/login/start", json={"realm": args.realm}
            )
            if response.status_code >= 400:
                print(
                    f"could not start login: HTTP {response.status_code}: "
                    f"{response.text[:300]}",
                    file=sys.stderr,
                )
                return 1

            started = response.json()
            url = str(started.get("url") or "").strip()
            state = str(started.get("state") or "").strip()
            if not url or not state:
                print("the bridge did not return a login URL/state", file=sys.stderr)
                return 1

            print("Open this URL in a browser and finish signing in:")
            print(url)
            print("Waiting for authorization...", file=sys.stderr, flush=True)

            deadline = time.monotonic() + args.timeout
            last_message = ""
            while time.monotonic() < deadline:
                poll = client.get(
                    "/admin/workbuddy/login/poll", params={"state": state}
                )
                if poll.status_code == 404:
                    print(
                        "the login session expired; run the command again",
                        file=sys.stderr,
                    )
                    return 1
                if poll.status_code >= 400:
                    print(
                        f"login poll failed: HTTP {poll.status_code}: "
                        f"{poll.text[:300]}",
                        file=sys.stderr,
                    )
                    return 1

                result = poll.json()
                if result.get("done"):
                    nickname = result.get("nickname") or "unknown"
                    masked = result.get("uid_masked") or ""
                    print(f"Logged in as {nickname} {masked}".rstrip())
                    try:
                        with httpx.Client(timeout=60.0) as catalog:
                            refreshed = catalog.post(
                                f"{args.catalog_url.rstrip('/')}/poll",
                                params={"provider": "workbuddy", "notify": "false"},
                            )
                        print(f"CatalogWatch refresh: HTTP {refreshed.status_code}")
                    except httpx.HTTPError as exc:
                        print(
                            f"CatalogWatch refresh skipped: {exc}",
                            file=sys.stderr,
                        )
                    return 0

                message = str(result.get("message") or "")
                if message and message != last_message:
                    print(message, file=sys.stderr, flush=True)
                    last_message = message
                time.sleep(args.poll_interval)

    except httpx.HTTPError as exc:
        print(f"login request failed: {exc}", file=sys.stderr)
        return 1

    print("timed out waiting for authorization", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
