from __future__ import annotations

from catalogwatch.config import ProviderConfig
from catalogwatch.db import Database
from catalogwatch.models import ModelRecord
from catalogwatch.util import parse_since


def _provider(provider_id: str = "p") -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name="Provider",
        adapter="openai_models",
        base_url="https://example.com/v1",
    )


def _model(model_id: str, **overrides) -> ModelRecord:
    values = {
        "provider_id": "p",
        "model_id": model_id,
        "name": model_id,
        "context_length": 8192,
        "max_output": 1024,
        "is_free": True,
        "free_kind": "free_tier",
        "pricing": None,
    }
    values.update(overrides)
    return ModelRecord(**values)


def test_baseline_import_does_not_emit_per_model_events(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.sync_providers([_provider()])

    events = db.apply_model_snapshot("p", "Provider", [_model("a"), _model("b")], baseline=True)

    assert [event.kind for event in events] == ["provider.baseline"]
    assert db.present_model_count("p") == 2
    stored = db.changes(parse_since("24h"))
    assert [row["kind"] for row in stored] == ["provider.baseline"]
    db.close()


def test_new_changed_removed_and_reappearing_models(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.sync_providers([_provider()])
    db.apply_model_snapshot("p", "Provider", [_model("a"), _model("b")], baseline=True)

    events = db.apply_model_snapshot(
        "p",
        "Provider",
        [
            _model("a", context_length=16384),  # changed
            _model("c"),                        # new
        ],
    )
    kinds = sorted(event.kind for event in events)
    assert kinds == ["model.changed", "model.new", "model.removed"]
    assert db.present_model_count("p") == 2
    assert {row["model_id"] for row in db.models(provider="p")} == {"a", "c"}

    # A removed model that reappears is announced again and is present once more.
    events = db.apply_model_snapshot(
        "p",
        "Provider",
        [_model("a", context_length=16384), _model("b"), _model("c")],
    )
    assert [event.kind for event in events] == ["model.new"]
    assert db.present_model_count("p") == 3
    db.close()


def test_price_only_change_updates_silently(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.sync_providers([_provider()])
    db.apply_model_snapshot(
        "p",
        "Provider",
        [_model("a", pricing={"prompt": "0.000001", "completion": "0.000002"})],
        baseline=True,
    )

    events = db.apply_model_snapshot(
        "p",
        "Provider",
        [_model("a", pricing={"prompt": "0.000003", "completion": "0.000004"})],
    )
    assert events == []
    stored = db.models(provider="p")[0]
    assert stored["pricing"]["prompt"] == "0.000003"
    db.close()


def test_stale_fingerprint_does_not_emit_events(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.sync_providers([_provider()])
    db.apply_model_snapshot("p", "Provider", [_model("a")], baseline=True)

    # Simulate a DB written by an older fingerprint algorithm.
    with db.conn:
        db.conn.execute(
            "UPDATE models SET fingerprint = 'stale' WHERE provider_id = 'p' AND model_id = 'a'"
        )

    events = db.apply_model_snapshot("p", "Provider", [_model("a")])
    assert events == []
    assert db.models(provider="p")[0]["model_id"] == "a"
    stored = db.conn.execute(
        "SELECT fingerprint FROM models WHERE provider_id='p' AND model_id='a'"
    ).fetchone()[0]
    assert stored != "stale"
    db.close()


def test_model_query_filters_and_export(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.sync_providers([_provider()])
    db.apply_model_snapshot(
        "p",
        "Provider",
        [
            _model("free-one"),
            _model("paid-one", is_free=False, free_kind="paid"),
        ],
        baseline=True,
    )

    free = db.models(free=True)
    assert [row["model_id"] for row in free] == ["free-one"]

    exported = db.export_catalog(free_only=True)
    assert exported["providers"][0]["models"][0]["model_id"] == "free-one"
    assert db.stats()["models_present"] == 2
    db.close()
