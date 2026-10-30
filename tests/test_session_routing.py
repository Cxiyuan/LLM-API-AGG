from __future__ import annotations

from catalogwatch.db import Database
from catalogwatch.workbuddy.catalog import WorkBuddyCatalog
from catalogwatch.workbuddy.load import LoadTracker
from catalogwatch.workbuddy.router import PoolRouter, extract_intent
from catalogwatch.workbuddy.store import WorkBuddyStore


def _setup(tmp_path, model_count: int = 3):
    db = Database(tmp_path / "catalog.db")
    store = WorkBuddyStore(db)
    store.upsert_account(
        {
            "id": 1,
            "provider": "workbuddy",
            "name": "main",
            "status": "active",
            "weight": 1,
            "priority": 0,
        }
    )
    store.record_credit_snapshot(
        1, {"ok": True, "available_total": 100, "total_dosage": 100}
    )
    store.upsert_models(
        "workbuddy",
        [
            {
                "id": f"m{index}",
                "credits": 0,
                "contextWindow": 100000,
                "supportsToolCall": True,
            }
            for index in range(model_count)
        ],
    )
    load = LoadTracker()
    router = PoolRouter(store, WorkBuddyCatalog(store, None), load)
    key = {"id": 1, "allowed_models": None, "max_attempts": 3}
    intent = extract_intent(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    )
    return db, store, load, router, key, intent


def test_sessions_are_distributed_across_models(tmp_path):
    db, _store, _load, router, key, intent = _setup(tmp_path)
    chosen = [
        router.plan(intent, key, session_id=f"session-{index}")[0].model_id
        for index in range(30)
    ]
    assert len(set(chosen)) > 1
    db.close()


def test_same_session_sticks_to_the_same_model(tmp_path):
    db, store, _load, router, key, intent = _setup(tmp_path)
    first = router.plan(intent, key, session_id="sticky-session")
    model = first[0].model_id
    binding = store.get_session("sticky-session")
    assert binding is not None and binding["model_id"] == model

    second = router.plan(intent, key, session_id="sticky-session")
    assert second[0].model_id == model

    # If the bound model disappears, the session is rebound to a usable model.
    with store.lock, store.conn:
        store.conn.execute(
            "UPDATE wb_models SET present=0 WHERE provider='workbuddy' AND model_id=?",
            (model,),
        )
    third = router.plan(intent, key, session_id="sticky-session")
    assert third[0].model_id != model
    assert store.get_session("sticky-session")["model_id"] == third[0].model_id
    db.close()


def test_inflight_load_steers_new_sessions_away(tmp_path):
    db, _store, load, router, key, intent = _setup(tmp_path, model_count=2)
    for _ in range(100):
        load.enter("workbuddy", "m0", 1)
    attempts = router.plan(intent, key, session_id="load-aware")
    assert attempts[0].model_id == "m1"
    db.close()
