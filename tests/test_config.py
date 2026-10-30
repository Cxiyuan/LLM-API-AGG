from __future__ import annotations

import pytest

from catalogwatch.config import ConfigError, load_settings
from catalogwatch.extract import _parse_json_object


def test_load_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
database:
  path: db.sqlite
providers:
  - id: demo
    name: Demo
    adapter: openai_models
    base_url: https://example.com/v1
    key_env: TEST_PROVIDER_KEY
    key_required: false
    free_kind: zero_price
    poll_interval_minutes: 15
    quota_sources:
      - label: limits
        url: https://example.com/limits
        interval_minutes: 30
        fields: ["requestsPerMinute", "requestsPerDay"]
""",
        encoding="utf-8",
    )

    settings = load_settings(config, db_path=tmp_path / "override.db")
    assert settings.db_path == (tmp_path / "override.db").resolve()
    assert settings.providers[0].id == "demo"
    assert settings.providers[0].key_required is False
    assert settings.providers[0].free_kind == "zero_price"
    assert settings.providers[0].quota_sources[0].label == "limits"
    assert settings.providers[0].quota_sources[0].fields == [
        "requestsPerMinute",
        "requestsPerDay",
    ]


def test_duplicate_provider_id_rejected(tmp_path):
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  - id: demo
    base_url: https://example.com/v1
  - id: demo
    base_url: https://example.org/v1
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate provider id"):
        load_settings(config, db_path=tmp_path / "db.sqlite")


def test_invalid_free_kind_rejected(tmp_path):
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
providers:
  - id: demo
    base_url: https://example.com/v1
    free_kind: whatever
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="free_kind"):
        load_settings(config, db_path=tmp_path / "db.sqlite")


def test_parse_llm_json_object():
    assert _parse_json_object('```json\n{"plans": []}\n```') == {"plans": []}
    assert _parse_json_object("not json") is None
    assert _parse_json_object('{"a": 1} trailing') == {"a": 1}
