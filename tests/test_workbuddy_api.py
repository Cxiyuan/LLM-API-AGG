from __future__ import annotations

from fastapi.testclient import TestClient

from catalogwatch.api import create_app
from catalogwatch.config import load_settings


def _settings(tmp_path):
    return load_settings(db_path=tmp_path / "api.db")


def test_console_auth_and_empty_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 307

        unauthorized = client.get("/api/overview")
        assert unauthorized.status_code == 401

        bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert bad.status_code == 401

        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        assert client.cookies.get("cw_admin")
        assert client.cookies.get("cw_csrf")

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["actor"] == "session"

        accounts = client.get("/api/workbuddy/accounts")
        assert accounts.status_code == 200
        assert accounts.json()["items"] == []

        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["ok"] is True

        # Buddy2api /health answers {"status":"ok"}; the console's `ok` flag is
        # normalized so it does not report a healthy bridge as unreachable.
        async def healthy():
            return {"status": "ok", "version": "test"}

        app.state.workbuddy.bridge.health = healthy
        normalized = client.get("/api/overview")
        assert normalized.json()["bridge"]["ok"] is True

        # Cookie-authenticated mutations require the double-submit CSRF token.
        no_csrf = client.post("/api/workbuddy/catalog/refresh")
        assert no_csrf.status_code == 403

        bearer = client.get(
            "/api/auth/session",
            headers={"Authorization": "Bearer test-admin-token"},
            cookies={},
        )
        assert bearer.status_code == 200
        assert bearer.json()["actor"] == "bearer"

        # The default admin/admin can be replaced from the environment.
        monkeypatch.setenv("CATALOGWATCH_ADMIN_USER", "ops")
        monkeypatch.setenv("CATALOGWATCH_ADMIN_PASSWORD", "s3cret")
        old = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert old.status_code == 401
        new = client.post(
            "/api/auth/login", json={"username": "ops", "password": "s3cret"}
        )
        assert new.status_code == 200


def test_checkin_rules_and_overview_api(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert login.status_code == 200
        service = app.state.workbuddy
        service.store.upsert_account(
            {"id": 1, "provider": "workbuddy", "name": "cn", "status": "active"}
        )

        async def status(force: bool = False) -> dict:
            return {
                "results": [
                    {
                        "account_id": 1,
                        "ok": True,
                        "today_checked_in": True,
                        "message": "今日已领取",
                    }
                ]
            }

        service.accounts.checkin_status = status  # type: ignore[method-assign]
        csrf = {"X-CSRF-Token": client.cookies.get("cw_csrf") or ""}

        saved = client.put(
            "/api/workbuddy/checkin/settings",
            headers=csrf,
            json={"enabled": True, "hour": 9, "minute": 30, "confirm_risk": True},
        )
        assert saved.status_code == 200
        rule = client.put(
            "/api/workbuddy/checkin/accounts/1",
            headers=csrf,
            json={"enabled": True, "hour": 23, "minute": 5},
        )
        assert rule.status_code == 200

        overview = client.get("/api/workbuddy/checkin").json()
        assert overview["global"] == {"enabled": True, "hour": 9, "minute": 30}
        item = overview["accounts"][0]
        assert item["rule"]["source"] == "account"
        assert item["rule"]["hour"] == 23
        assert item["checkin"]["checked_today"] is True

        cleared = client.delete("/api/workbuddy/checkin/accounts/1", headers=csrf)
        assert cleared.status_code == 200
        overview = client.get("/api/workbuddy/checkin").json()
        assert overview["accounts"][0]["rule"]["source"] == "global"
        assert overview["accounts"][0]["rule"]["hour"] == 9


def test_scheduling_policy_and_schedule_api(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGWATCH_POOL_SCHEDULER", "0")
    monkeypatch.setenv("CATALOGWATCH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALOGWATCH_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUDDY2API_URL", "http://127.0.0.1:1")

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": client.cookies.get("cw_csrf") or ""}
        service = app.state.workbuddy
        service.store.upsert_account(
            {"id": 1, "provider": "workbuddy", "name": "cn", "status": "active"}
        )
        service.store.upsert_models(
            "workbuddy",
            [
                {"id": "free-model", "credits": 0.0, "contextWindow": 100000},
                {"id": "paid-model", "credits": 0.2, "contextWindow": 100000},
                {"id": "full-model", "credits": 0.3, "contextWindow": 100000},
            ],
        )
        service.store.replace_promotions(
            "workbuddy",
            {
                "paid-model": [
                    {
                        "label": "夜间折扣",
                        "color": None,
                        "factor": 0.5,
                        "priority": 0,
                        "active_only": False,
                        "schedule": {
                            "timezone": "Asia/Shanghai",
                            "daily": [{"start": "00:00", "end": "23:59"}],
                        },
                        "text_zh": "",
                        "text_en": "",
                    }
                ]
            },
        )

        saved = client.put(
            "/api/policy",
            headers=csrf,
            json={
                "night_auto": False,
                "night_start": "22:00",
                "night_end": "08:00",
                "day_priority": {"free": 0, "promo": 1, "normal": 2},
                "night_priority": {
                    "night_free": 0,
                    "night_promo": 1,
                    "free": 2,
                    "promo": 3,
                    "normal": 4,
                },
            },
        )
        assert saved.status_code == 200, saved.text
        policy = saved.json()["policy"]
        assert policy["night_start"] == "22:00"
        assert policy["night_priority"]["night_promo"] == 1

        schedule = client.get("/api/schedule")
        assert schedule.status_code == 200
        payload = schedule.json()
        assert payload["night_window"]["start"] == "22:00"
        assert payload["night_window"]["end"] == "08:00"
        assert payload["night_window"]["source"] == "manual"
        assert payload["account_sharing"] == "balanced"
        assert payload["deny_full_price"] is True
        assert set(payload["providers"]) == {"workbuddy"}
        full = next(
            item for item in payload["models"] if item["model_id"] == "full-model"
        )
        assert full["full_price"] is True
        assert full["eligible"] is False
        model = next(
            item for item in payload["models"] if item["model_id"] == "paid-model"
        )
        assert model["night_credits"] == 0.1
        assert model["day_credits"] == 0.1

        bad = client.put(
            "/api/policy",
            headers=csrf,
            json={"night_priority": {"unknown": 0}},
        )
        assert bad.status_code == 400
        bad_time = client.put(
            "/api/policy", headers=csrf, json={"night_start": "25:99"}
        )
        assert bad_time.status_code == 400
        bad_sharing = client.put(
            "/api/policy", headers=csrf, json={"account_sharing": "random"}
        )
        assert bad_sharing.status_code == 400

        async def fake_login(realm: str) -> dict:
            return {
                "ok": True,
                "url": "https://www.workbuddy.ai/login?state=test",
                "state": "test",
                "realm": realm,
                "requires_incognito": realm == "global",
            }

        service.accounts.login_start = fake_login  # type: ignore[method-assign]
        global_link = client.post(
            "/api/workbuddy/login/start", headers=csrf, json={"realm": "global"}
        )
        assert global_link.status_code == 200
        assert global_link.json()["requires_incognito"] is True
