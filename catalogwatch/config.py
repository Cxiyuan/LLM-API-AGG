from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class QuotaSourceConfig:
    label: str
    url: str
    interval_minutes: int = 60
    selector: str | None = None
    fields: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass(slots=True)
class ProviderConfig:
    id: str
    name: str
    adapter: str
    base_url: str
    key_env: str | None = None
    key_required: bool = True
    auth: str = "bearer"
    auth_param: str = "key"
    free_kind: str = "free_tier"
    poll_interval_minutes: int = 30
    models_path: str = "/models"
    enabled: bool = True
    notes: str | None = None
    quota_sources: list[QuotaSourceConfig] = field(default_factory=list)


@dataclass(slots=True)
class NotifyConfig:
    macos: bool = True
    webhook_url_env: str | None = None
    max_events_per_message: int = 5


@dataclass(slots=True)
class ExtractorConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:3001/v1"
    model: str = "auto:fast"
    api_key_env: str | None = "FREELMAPI_KEY"
    max_chars: int = 12000


@dataclass(slots=True)
class Settings:
    providers: list[ProviderConfig]
    notify: NotifyConfig
    extractor: ExtractorConfig
    db_path: Path
    config_path: Path
    user_agent: str = "catalogwatch/0.1 (personal catalog monitor)"


_ALLOWED_AUTH = {"bearer", "query", "header", "none"}
_ALLOWED_FREE_KIND = {"zero_price", "free_tier", "credits"}


def resolve_env(name: str | None) -> str | None:
    if not name:
        return None
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _positive_int(value: Any, default: int, where: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{where} must be >= 1")
    return parsed


def _parse_quota_sources(raw: Any, provider_id: str) -> list[QuotaSourceConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"providers[{provider_id}].quota_sources must be a list")

    sources: list[QuotaSourceConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"providers[{provider_id}].quota_sources[{index}]"
        item = _require_mapping(item, where)
        label = str(item.get("label") or "").strip()
        url = str(item.get("url") or "").strip()
        if not label:
            raise ConfigError(f"{where}.label is required")
        if label in seen:
            raise ConfigError(f"{where}.label {label!r} is duplicated")
        seen.add(label)
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}.url must be an http(s) URL")

        raw_fields = item.get("fields") or []
        if not isinstance(raw_fields, list) or not all(
            isinstance(value, str) and value.strip() for value in raw_fields
        ):
            raise ConfigError(f"{where}.fields must be a list of non-empty strings")

        sources.append(
            QuotaSourceConfig(
                label=label,
                url=url,
                interval_minutes=_positive_int(item.get("interval_minutes"), 60, f"{where}.interval_minutes"),
                selector=(str(item["selector"]) if item.get("selector") else None),
                fields=[value.strip() for value in raw_fields],
                enabled=bool(item.get("enabled", True)),
            )
        )
    return sources


def _parse_provider(raw: Any, index: int) -> ProviderConfig:
    where = f"providers[{index}]"
    item = _require_mapping(raw, where)
    provider_id = str(item.get("id") or "").strip()
    if not provider_id:
        raise ConfigError(f"{where}.id is required")

    adapter = str(item.get("adapter") or "openai_models").strip()
    base_url = str(item.get("base_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}.base_url must be an http(s) URL")

    auth = str(item.get("auth") or "bearer").strip().lower()
    if auth not in _ALLOWED_AUTH:
        raise ConfigError(f"{where}.auth must be one of {sorted(_ALLOWED_AUTH)}")

    free_kind = str(item.get("free_kind") or "free_tier").strip().lower()
    if free_kind not in _ALLOWED_FREE_KIND:
        raise ConfigError(f"{where}.free_kind must be one of {sorted(_ALLOWED_FREE_KIND)}")

    return ProviderConfig(
        id=provider_id,
        name=str(item.get("name") or provider_id).strip(),
        adapter=adapter,
        base_url=base_url.rstrip("/"),
        key_env=(str(item["key_env"]).strip() if item.get("key_env") else None),
        key_required=bool(item.get("key_required", True)),
        auth=auth,
        auth_param=str(item.get("auth_param") or "key"),
        free_kind=free_kind,
        poll_interval_minutes=_positive_int(
            item.get("poll_interval_minutes"), 30, f"{where}.poll_interval_minutes"
        ),
        models_path=str(item.get("models_path") or "/models"),
        enabled=bool(item.get("enabled", True)),
        notes=(str(item["notes"]) if item.get("notes") else None),
        quota_sources=_parse_quota_sources(item.get("quota_sources"), provider_id),
    )


def load_settings(
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Settings:
    load_dotenv()

    raw_path = config_path or os.getenv("CATALOGWATCH_CONFIG") or "config/providers.yaml"
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    data = _require_mapping(data, "config")

    raw_providers = data.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ConfigError("config.providers must be a non-empty list")

    providers = [_parse_provider(item, index) for index, item in enumerate(raw_providers)]

    ids: set[str] = set()
    for provider in providers:
        if provider.id in ids:
            raise ConfigError(f"duplicate provider id: {provider.id}")
        ids.add(provider.id)

    notify_raw = _require_mapping(data.get("notify"), "notify")
    extractor_raw = _require_mapping(data.get("extractor"), "extractor")
    database_raw = _require_mapping(data.get("database"), "database")

    db_raw = db_path or os.getenv("CATALOGWATCH_DB") or database_raw.get("path") or "data/catalog.db"
    resolved_db = Path(str(db_raw)).expanduser()
    if not resolved_db.is_absolute():
        resolved_db = (Path.cwd() / resolved_db).resolve()

    return Settings(
        providers=providers,
        notify=NotifyConfig(
            macos=bool(notify_raw.get("macos", True)),
            webhook_url_env=(str(notify_raw["webhook_url_env"]) if notify_raw.get("webhook_url_env") else None),
            max_events_per_message=_positive_int(
                notify_raw.get("max_events_per_message"), 5, "notify.max_events_per_message"
            ),
        ),
        extractor=ExtractorConfig(
            enabled=bool(extractor_raw.get("enabled", False)),
            base_url=str(extractor_raw.get("base_url") or "http://127.0.0.1:3001/v1").rstrip("/"),
            model=str(extractor_raw.get("model") or "auto:fast"),
            api_key_env=(str(extractor_raw["api_key_env"]) if extractor_raw.get("api_key_env") else None),
            max_chars=_positive_int(extractor_raw.get("max_chars"), 12000, "extractor.max_chars"),
        ),
        db_path=resolved_db,
        config_path=path,
        user_agent=str(data.get("user_agent") or "catalogwatch/0.1 (personal catalog monitor)"),
    )
