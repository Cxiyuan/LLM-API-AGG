from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    """Runtime settings for the WorkBuddy pool.

    The model plane has no provider catalog or API keys of its own: models come
    from the WorkBuddy bridge, and the bridge connection is configured through
    environment variables by the container bootstrap.
    """

    db_path: Path


def load_settings(db_path: str | Path | None = None) -> Settings:
    raw = db_path or os.getenv("CATALOGWATCH_DB") or "data/catalog.db"
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return Settings(db_path=path)
