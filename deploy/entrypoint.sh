#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

# The entrypoint starts as root only to fix bind-mount ownership, then re-execs
# itself as the unprivileged app user. No long-running process stays as root.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR/buddy2api" "$DATA_DIR/catalogwatch" "$DATA_DIR/secrets"
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$0" "$@"
fi

umask 077
mkdir -p "$DATA_DIR/buddy2api" "$DATA_DIR/catalogwatch" "$DATA_DIR/secrets"

PYTHON=/opt/venv/bin/python
SECRETS_DIR="$DATA_DIR/secrets"

random_secret() {
    "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))'
}

# Use an explicit env value, else the persisted file, else mint and persist one.
load_or_create_secret() {
    file="$1"
    current="$2"
    if [ -n "$current" ]; then
        printf '%s' "$current"
        return
    fi
    if [ -f "$file" ]; then
        cat "$file"
        return
    fi
    value="$(random_secret)"
    printf '%s\n' "$value" > "$file"
    chmod 600 "$file"
    printf '%s' "$value"
}

CB_GATEWAY_ADMIN_TOKEN="$(load_or_create_secret "$SECRETS_DIR/admin_token" "${CB_GATEWAY_ADMIN_TOKEN:-}")"
export CB_GATEWAY_ADMIN_TOKEN

CATALOGWATCH_ADMIN_TOKEN="$(load_or_create_secret "$SECRETS_DIR/catalogwatch_admin_token" "${CATALOGWATCH_ADMIN_TOKEN:-}")"
export CATALOGWATCH_ADMIN_TOKEN

CATALOGWATCH_KEY_PEPPER="$(load_or_create_secret "$SECRETS_DIR/pool_key_pepper" "${CATALOGWATCH_KEY_PEPPER:-}")"
export CATALOGWATCH_KEY_PEPPER

export DATA_DIR
export CB_GATEWAY_DB_PATH="${CB_GATEWAY_DB_PATH:-$DATA_DIR/buddy2api/buddy2api.db}"
export CB_GATEWAY_PROVIDERS="${CB_GATEWAY_PROVIDERS:-workbuddy,workbuddy-i}"
export CB_GATEWAY_AUTO_IMPORT="${CB_GATEWAY_AUTO_IMPORT:-0}"
export CB_GATEWAY_ALLOW_UNAUTHENTICATED_API="${CB_GATEWAY_ALLOW_UNAUTHENTICATED_API:-0}"
export CB_DOCKER=1
export CATALOGWATCH_DB="${CATALOGWATCH_DB:-$DATA_DIR/catalogwatch/catalog.db}"
export CATALOGWATCH_CONFIG_BASE="${CATALOGWATCH_CONFIG_BASE:-/app/config/providers.yaml}"
export CATALOGWATCH_PORT="${CATALOGWATCH_PORT:-8788}"
export BUDDY2API_URL="${BUDDY2API_URL:-http://127.0.0.1:8787}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

cat <<EOF

[entrypoint] data directory:  $DATA_DIR
[entrypoint] WorkBuddy UI:   http://127.0.0.1:8787/  (bridge debug UI)
[entrypoint] Pool console:   http://127.0.0.1:8788/
[entrypoint] CatalogWatch:   http://127.0.0.1:8788/healthz
[entrypoint] bridge admin:   $SECRETS_DIR/admin_token (or CB_GATEWAY_ADMIN_TOKEN)
[entrypoint] console token:  $SECRETS_DIR/catalogwatch_admin_token (or CATALOGWATCH_ADMIN_TOKEN)
[entrypoint] WorkBuddy login: docker exec -it <container> workbuddy-login --realm cn
[entrypoint] Publish these ports on 127.0.0.1 only; never expose them publicly.

EOF

exec /opt/venv/bin/supervisord -c /app/deploy/supervisord.conf
