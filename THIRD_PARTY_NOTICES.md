# Third-party notices

## Buddy2api

This repository vendors the source of **Buddy2api** to provide the optional WorkBuddy
bridge in the container image.

- Project: https://github.com/wicm84266964/Buddy2api
- Version: v2.2.12
- Commit: `16a18af04fde8cd22c3e69ccfb1cce9045ac0ac1`
- License: MIT — the full text is at `vendor/buddy2api/LICENSE`.
- Vendored files are under `vendor/buddy2api/`.

Buddy2api is an unofficial client-side bridge. It is not affiliated with, endorsed by,
or sponsored by WorkBuddy, CodeBuddy, Tencent, or Zhipu. It reuses the login you obtain
through WorkBuddy's official CLI device-authorization flow and consumes that account's
quota. Using it may be subject to the upstream service terms; verify compliance before
use. Do not use it for multi-account pooling, resale, or public hosting.

## Other dependencies

CatalogWatch's Python dependencies are declared in `pyproject.toml`; the bridge's pinned
dependencies are in `vendor/buddy2api/requirements.txt`. Their licenses are available
from their respective packages.
