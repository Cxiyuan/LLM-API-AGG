#!/bin/sh
set -eu

curl -fsS --max-time 3 http://127.0.0.1:8787/health >/dev/null
curl -fsS --max-time 3 http://127.0.0.1:8788/healthz >/dev/null
