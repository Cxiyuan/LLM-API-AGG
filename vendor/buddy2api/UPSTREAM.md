# Vendored upstream: Buddy2api

- Upstream: https://github.com/wicm84266964/Buddy2api
- Version: v2.2.12
- Commit: `16a18af04fde8cd22c3e69ccfb1cce9045ac0ac1`
- Commit date: 2026-10-27
- License: MIT (see `LICENSE` in this directory)

## What was copied

The Python source, `providers/`, `web/`, `requirements.txt`, `README*.md`, `SECURITY.md`,
and `LICENSE`. Upstream `docs/`, `tests/`, `assets/`, Docker/Compose files, and Windows
start scripts are not vendored.

## Local modifications

None to the upstream Python source. Container wiring lives in this repository's
`deploy/` directory:

- `deploy/start_catalogwatch.py` waits for the bridge, provisions an internal gateway
  key, and enables the `workbuddy` provider in a generated CatalogWatch config.
- `deploy/workbuddy-login.py` wraps the browser/device login endpoints for headless use.
- `deploy/entrypoint.sh` + `deploy/supervisord.conf` run both processes in one image.

## Updating

1. Re-vendor a newer upstream commit into this directory (keep the directory layout).
2. Update the version/commit above.
3. Run `uv run pytest` and the container smoke test in CI.
4. Re-check `providers/workbuddy/oauth.py` and the admin login endpoints if the upstream
   changes its login flow.

This bridge is unofficial and is not affiliated with WorkBuddy/CodeBuddy. It uses your
own account's login and quota. Use it only with accounts you are entitled to use, and
keep it on loopback.
