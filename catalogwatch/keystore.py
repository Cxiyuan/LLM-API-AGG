"""Provider API keys entered in the console, encrypted at rest.

The original deployment only read provider keys from environment variables.
That is fine for a compose file but not for a web console: operators should be
able to paste a key, test it, and rotate it without editing files or
restarting. This store keeps those keys in SQLite as Fernet ciphertext, keyed
by a secret persisted under ``/data/secrets``.

Resolution order is console key first, environment second. An environment key
still works for headless deployments and remains visible as its source.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from typing import Any

from .config import ProviderConfig, resolve_env
from .db import Database
from .util import utcnow_iso

log = logging.getLogger(__name__)

MAX_KEY_CHARS = 8_192
_SECRET_ENVS = ("CATALOGWATCH_PROVIDER_KEY_SECRET", "CATALOGWATCH_KEY_PEPPER")

try:  # cryptography is a hard dependency in the image; stay importable without it
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised only in minimal dev setups
    Fernet = None  # type: ignore[assignment]

    class InvalidToken(Exception):  # type: ignore[no-redef]
        pass


def _fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(
        ("llm-agg/provider-keys/v1|" + secret).encode("utf-8")
    ).digest()
    return base64.urlsafe_b64encode(digest)


class ProviderKeyStore:
    """Encrypted per-provider API keys, with environment fallback."""

    def __init__(self, db: Database, secret: str | None = None):
        self.db = db
        if Fernet is None:
            log.warning(
                "provider key storage disabled: the cryptography package is not installed"
            )
            self._fernet = None
            return
        value = secret
        if not value:
            for name in _SECRET_ENVS:
                value = os.environ.get(name)
                if value:
                    break
        if not value:
            # Dev-only fallback: keys survive the process, not a restart.
            value = secrets.token_urlsafe(48)
            log.warning(
                "no CATALOGWATCH_PROVIDER_KEY_SECRET/CATALOGWATCH_KEY_PEPPER set; "
                "console-stored provider keys will not survive a restart"
            )
        self._fernet = Fernet(_fernet_key(value))

    # ------------------------------------------------------------------ storage

    def get(self, provider_id: str) -> str | None:
        if self._fernet is None:
            return None
        row = self.db.conn.execute(
            "SELECT ciphertext FROM provider_keys WHERE provider_id=?", (provider_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(str(row["ciphertext"]).encode("utf-8")).decode(
                "utf-8"
            )
        except (InvalidToken, ValueError):
            log.warning(
                "stored key for provider %s cannot be decrypted (secret changed?); ignoring",
                provider_id,
            )
            return None

    def set(self, provider_id: str, value: str) -> None:
        if self._fernet is None:
            raise RuntimeError("provider key storage is unavailable: cryptography is not installed")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API key must not be empty")
        if len(cleaned) > MAX_KEY_CHARS:
            raise ValueError("API key is too long")
        ciphertext = self._fernet.encrypt(cleaned.encode("utf-8")).decode("utf-8")
        with self.db.write_lock, self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO provider_keys (provider_id, ciphertext, updated_at)
                VALUES (?,?,?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    ciphertext=excluded.ciphertext,
                    updated_at=excluded.updated_at
                """,
                (provider_id, ciphertext, utcnow_iso()),
            )

    def unset(self, provider_id: str) -> None:
        with self.db.write_lock, self.db.conn:
            self.db.conn.execute(
                "DELETE FROM provider_keys WHERE provider_id=?", (provider_id,)
            )

    def describe(self, cfg: ProviderConfig) -> dict[str, Any]:
        """Configured state and where the effective key comes from."""
        stored = self.get(cfg.id) is not None
        ambient = resolve_env(cfg.key_env) is not None
        source = "console" if stored else ("env" if ambient else None)
        return {"configured": stored or ambient, "source": source}

    # ------------------------------------------------------------------ resolve

    def resolve(self, cfg: ProviderConfig) -> str | None:
        """Bookmark-aware key lookup: console first, environment second."""
        value = self.get(cfg.id)
        if value is not None:
            return value
        return resolve_env(cfg.key_env)
