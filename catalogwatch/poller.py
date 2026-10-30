from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .adapters import AdapterError, AuthError, MissingKeyError, build_adapter
from .config import ProviderConfig, Settings, resolve_env
from .db import Database
from .models import Event

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PollResult:
    provider_id: str
    status: str
    model_count: int = 0
    free_model_count: int = 0
    new_models: int = 0
    removed_models: int = 0
    error: str | None = None

    def line(self) -> str:
        if self.status != "ok":
            return f"{self.provider_id:<14} {self.status:<12} {self.error or ''}"
        return (
            f"{self.provider_id:<14} ok           "
            f"{self.model_count:>4} models, {self.free_model_count:>4} free, "
            f"+{self.new_models} new, -{self.removed_models} removed"
        )


class ModelPoller:
    def __init__(self, settings: Settings, db: Database, key_resolver=None):
        self.settings = settings
        self.db = db
        self.key_resolver = key_resolver

    async def poll(self, cfg: ProviderConfig) -> tuple[PollResult, list[Event]]:
        # Console-stored key first, environment second.
        api_key = (
            self.key_resolver(cfg)
            if self.key_resolver is not None
            else resolve_env(cfg.key_env)
        )

        if cfg.auth != "none" and cfg.key_required and not api_key:
            error = f"{cfg.key_env or 'API key'} is not set"
            self.db.mark_provider_poll(cfg.id, "needs_key", error)
            return PollResult(cfg.id, "needs_key", error=error), []

        timeout = httpx.Timeout(30.0, connect=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
            ) as client:
                adapter = build_adapter(cfg, client, api_key)
                records = await adapter.fetch_models()
        except MissingKeyError as exc:
            self.db.mark_provider_poll(cfg.id, "needs_key", str(exc))
            return PollResult(cfg.id, "needs_key", error=str(exc)), []
        except AuthError as exc:
            log.warning("%s: auth rejected: %s", cfg.id, exc)
            self.db.mark_provider_poll(cfg.id, "auth_error", str(exc))
            return PollResult(cfg.id, "auth_error", error=str(exc)), []
        except AdapterError as exc:
            log.warning("%s: %s", cfg.id, exc)
            self.db.mark_provider_poll(cfg.id, "error", str(exc))
            return PollResult(cfg.id, "error", error=str(exc)), []
        except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the loop
            log.exception("%s: unexpected polling failure", cfg.id)
            self.db.mark_provider_poll(cfg.id, "error", f"{type(exc).__name__}: {exc}")
            return PollResult(cfg.id, "error", error=f"{type(exc).__name__}: {exc}"), []

        if not records:
            if cfg.id == "workbuddy":
                error = "no WorkBuddy account is logged in; run workbuddy-login"
                status = "needs_login"
            else:
                error = "provider returned an empty model list; keeping the existing catalog"
                status = "error"
            self.db.mark_provider_poll(cfg.id, status, error)
            return PollResult(cfg.id, status, error=error), []

        previous = self.db.present_model_count(cfg.id)
        if previous >= 20 and len(records) < previous * 0.5:
            error = (
                f"refusing a suspicious snapshot: {len(records)} models vs "
                f"{previous} previously; keeping the existing catalog"
            )
            self.db.mark_provider_poll(cfg.id, "error", error)
            return PollResult(cfg.id, "error", error=error), []

        baseline = not self.db.has_model_history(cfg.id)
        events = self.db.apply_model_snapshot(
            cfg.id, cfg.name, records, baseline=baseline
        )
        self.db.mark_provider_poll(cfg.id, "ok", None)

        free_count = sum(1 for record in records if record.is_free)
        new_count = sum(1 for event in events if event.kind == "model.new")
        removed_count = sum(1 for event in events if event.kind == "model.removed")
        if baseline:
            new_count = len(records)

        return (
            PollResult(
                cfg.id,
                "ok",
                model_count=len(records),
                free_model_count=free_count,
                new_models=new_count,
                removed_models=removed_count,
            ),
            events,
        )
