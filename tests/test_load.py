from __future__ import annotations

from catalogwatch.workbuddy.load import LoadTracker


def test_inflight_and_stability():
    load = LoadTracker()
    assert load.stability("workbuddy", "m1") == 0.5

    load.enter("workbuddy", "m1", 1)
    load.enter("workbuddy", "m1", 1)
    assert load.inflight("workbuddy", "m1") == 2
    assert load.account_inflight("workbuddy", "m1", 1) == 2
    load.exit("workbuddy", "m1", 1)
    load.exit("workbuddy", "m1", 1)
    assert load.inflight("workbuddy", "m1") == 0

    for _ in range(10):
        load.record(
            "workbuddy", "m1", 1, success=True, status_code=200, latency_ms=100
        )
    healthy = load.stability("workbuddy", "m1")
    for _ in range(20):
        load.record(
            "workbuddy", "m1", 1, success=False, status_code=500, latency_ms=5000
        )
    degraded = load.stability("workbuddy", "m1")
    assert healthy > degraded


def test_jitter_is_stable_and_bounded():
    first = LoadTracker.jitter("session-a", "workbuddy", "m1")
    second = LoadTracker.jitter("session-a", "workbuddy", "m1")
    other = LoadTracker.jitter("session-b", "workbuddy", "m1")
    assert first == second
    assert 0.0 <= first <= 1.0
    assert first != other
