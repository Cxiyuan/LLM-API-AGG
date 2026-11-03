# syntax=docker/dockerfile:1
#
# One image, three processes (supervised):
#   - Buddy2api: WorkBuddy browser/device login + OpenAI-compatible gateway (:8787)
#   - Unified gateway: API + account/catalog scheduler (:8790, internal)
#   - Hono/htmx console: enterprise UI + reverse proxy (:8788, public)
#
# Publish both ports on the host loopback only. The bridge is an unofficial
# personal-use integration; see THIRD_PARTY_NOTICES.md.

FROM node:22-bookworm-slim AS ui-deps
WORKDIR /opt/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --omit=dev
COPY ui/server.js ./
COPY ui/public ./public

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/app \
    DATA_DIR=/data \
    BUDDY2API_URL=http://127.0.0.1:8787 \
    CATALOGWATCH_PORT=8790 \
    UI_PORT=8788 \
    API_ORIGIN=http://127.0.0.1:8790

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
        libstdc++6 \
        tzdata \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

WORKDIR /app

# Pool console first, then the bridge's pinned dependency set.
COPY pyproject.toml ./
COPY catalogwatch ./catalogwatch
RUN pip install --upgrade pip && pip install .

COPY vendor/buddy2api/requirements.txt /tmp/buddy2api-requirements.txt
RUN pip install -r /tmp/buddy2api-requirements.txt

# Supervisor 4.2.5 imports pkg_resources. setuptools >=81 removed that module,
# and Python 3.12 venvs no longer ship setuptools, so pin it explicitly.
RUN pip install "setuptools==80.9.0" "supervisor==4.2.5"

COPY vendor/buddy2api /opt/buddy2api
COPY deploy /app/deploy
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=ui-deps /opt/ui /opt/ui

RUN chmod 0755 /app/deploy/entrypoint.sh /app/deploy/healthcheck.sh /app/deploy/start_catalogwatch.py \
    && install -m 0755 /app/deploy/workbuddy-login.py /usr/local/bin/workbuddy-login \
    && groupadd --gid 10001 app \
    && useradd --create-home --uid 10001 --gid app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data /app /opt/buddy2api

VOLUME ["/data"]

EXPOSE 8787 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/deploy/healthcheck.sh"]

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
