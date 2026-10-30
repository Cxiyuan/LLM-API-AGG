"""Async HTTP client for the local Buddy2api bridge.

The control plane deliberately talks to the bridge over HTTP instead of
importing its modules: the bridge owns WorkBuddy credentials, token refresh,
and the upstream protocol, and it runs in a separate process.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx


class BridgeError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BridgeAuthError(BridgeError):
    pass


class BridgeUnavailable(BridgeError):
    pass


class BuddyBridge:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        admin_token: str = "",
        gateway_key: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token.strip()
        self.gateway_key = gateway_key.strip()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers

    def _admin_headers(self) -> dict[str, str]:
        if not self.admin_token:
            raise BridgeAuthError(0, "bridge admin token is not configured")
        return {"Authorization": f"Bearer {self.admin_token}"}

    def _gateway_headers(self) -> dict[str, str]:
        if not self.gateway_key:
            raise BridgeAuthError(0, "bridge gateway key is not configured")
        return {"Authorization": f"Bearer {self.gateway_key}"}

    @staticmethod
    def _message(response: httpx.Response) -> str:
        text = response.text[:500].strip()
        try:
            value = response.json()
            if isinstance(value, dict):
                detail = value.get("detail") or value.get("error") or value
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail
                text = str(detail)[:500]
        except ValueError:
            pass
        return text or f"HTTP {response.status_code}"

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = self._message(response)
        if response.status_code in {401, 403}:
            raise BridgeAuthError(response.status_code, message)
        raise BridgeError(response.status_code, message)

    async def _admin_get(self, path: str, params: dict | None = None) -> Any:
        try:
            response = await self._client.get(
                path, headers=self._admin_headers(), params=params
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge request failed: {exc}") from exc
        self._raise(response)
        return response.json()

    async def _admin_post(self, path: str, payload: dict | None = None) -> Any:
        try:
            response = await self._client.post(
                path, headers=self._admin_headers(), json=payload or {}
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge request failed: {exc}") from exc
        self._raise(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _admin_put(self, path: str, payload: dict | None = None) -> Any:
        try:
            response = await self._client.put(
                path, headers=self._admin_headers(), json=payload or {}
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge request failed: {exc}") from exc
        self._raise(response)
        return response.json()

    async def _admin_patch(self, path: str, payload: dict | None = None) -> Any:
        try:
            response = await self._client.patch(
                path, headers=self._admin_headers(), json=payload or {}
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge request failed: {exc}") from exc
        self._raise(response)
        return response.json()

    async def _admin_delete(self, path: str) -> Any:
        try:
            response = await self._client.delete(path, headers=self._admin_headers())
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge request failed: {exc}") from exc
        self._raise(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---------------------------------------------------------------- health/admin

    async def health(self) -> dict:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge health failed: {exc}") from exc
        self._raise(response)
        return response.json()

    async def list_accounts(self) -> list[dict]:
        value = await self._admin_get("/admin/accounts")
        return value if isinstance(value, list) else []

    async def update_account(self, account_id: int, data: dict) -> dict:
        return await self._admin_put(f"/admin/accounts/{account_id}", data)

    async def delete_account(self, account_id: int) -> None:
        await self._admin_delete(f"/admin/accounts/{account_id}")

    async def refresh_account(self, account_id: int) -> dict:
        return await self._admin_post(f"/admin/accounts/{account_id}/refresh")

    async def test_account(self, account_id: int, payload: dict | None = None) -> dict:
        return await self._admin_post(f"/admin/accounts/{account_id}/test", payload or {})

    async def discover_accounts(self, params: dict | None = None) -> dict:
        return await self._admin_get("/admin/accounts/discover", params or {})

    async def import_accounts(self, payload: dict) -> dict:
        return await self._admin_post("/admin/accounts/import", payload)

    async def login_start(self, realm: str = "cn") -> dict:
        return await self._admin_post("/admin/workbuddy/login/start", {"realm": realm})

    async def login_poll(self, state: str) -> dict:
        return await self._admin_get(
            "/admin/workbuddy/login/poll", {"state": state}
        )

    async def credit_summary(self, force: bool = False) -> dict:
        return await self._admin_get(
            "/admin/credit-summary", {"force": "1" if force else "0"}
        )

    async def account_resources(self, account_id: int, force: bool = True) -> dict:
        return await self._admin_get(
            f"/admin/accounts/{account_id}/resources",
            {"force": "1" if force else "0"},
        )

    async def checkin_status_all(self, force: bool = False) -> dict:
        return await self._admin_get(
            "/admin/accounts/checkin-status-all",
            {"force": "1" if force else "0"},
        )

    async def checkin_one(self, account_id: int) -> dict:
        return await self._admin_post(f"/admin/accounts/{account_id}/checkin")

    async def checkin_all(self) -> dict:
        return await self._admin_post("/admin/accounts/checkin-all", {})

    async def keepalive_all(self) -> dict:
        return await self._admin_post("/admin/accounts/keepalive-all", {})

    async def get_settings(self) -> dict:
        return await self._admin_get("/admin/settings")

    async def update_settings(self, payload: dict) -> dict:
        return await self._admin_put("/admin/settings", payload)

    async def automation_logs(self, limit: int = 100, failures_only: bool = False) -> Any:
        return await self._admin_get(
            "/admin/automation-logs",
            {"limit": limit, "failures_only": "1" if failures_only else "0"},
        )

    async def search_logs(self, **params: Any) -> dict:
        cleaned = {key: value for key, value in params.items() if value is not None}
        return await self._admin_get("/admin/logs/search", cleaned)

    async def workbuddy_config(
        self, provider: str = "workbuddy", identity: str = "current", force: bool = False
    ) -> dict:
        return await self._admin_get(
            "/admin/workbuddy/config",
            {
                "provider": provider,
                "identity": identity,
                "force": "1" if force else "0",
            },
        )

    async def models(self) -> dict:
        try:
            response = await self._client.get(
                "/v1/models", headers=self._gateway_headers()
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge model list failed: {exc}") from exc
        self._raise(response)
        return response.json()

    async def refresh_models(self) -> dict:
        """Ask the bridge to re-fetch the upstream model catalog."""
        return await self._admin_post("/admin/models/refresh", {})

    # ---------------------------------------------------------------- inference

    async def post_chat(self, payload: dict) -> httpx.Response:
        return await self.post_chat_with_headers(payload, None)

    async def post_chat_with_headers(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        request_headers = {**self._gateway_headers(), **(headers or {})}
        try:
            response = await self._client.post(
                "/v1/chat/completions",
                headers=request_headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge chat failed: {exc}") from exc
        self._raise(response)
        return response

    async def post_responses(self, payload: dict) -> httpx.Response:
        return await self.post_responses_with_headers(payload, None)

    async def post_responses_with_headers(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        request_headers = {**self._gateway_headers(), **(headers or {})}
        try:
            response = await self._client.post(
                "/v1/responses", headers=request_headers, json=payload
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge responses failed: {exc}") from exc
        self._raise(response)
        return response

    async def open_chat_stream(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """Open a streaming chat request. The caller must close the response."""
        request_headers = {**self._gateway_headers(), **(headers or {})}
        request = self._client.build_request(
            "POST", "/v1/chat/completions", headers=request_headers, json=payload
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge stream failed: {exc}") from exc

    async def open_responses_stream(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        request_headers = {**self._gateway_headers(), **(headers or {})}
        request = self._client.build_request(
            "POST", "/v1/responses", headers=request_headers, json=payload
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge responses stream failed: {exc}") from exc

    @asynccontextmanager
    async def stream_chat(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        request_headers = {**self._gateway_headers(), **(headers or {})}
        request = self._client.build_request(
            "POST", "/v1/chat/completions", headers=request_headers, json=payload
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge stream failed: {exc}") from exc
        try:
            yield response
        finally:
            await response.aclose()

    @asynccontextmanager
    async def stream_responses(
        self, payload: dict, headers: dict[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        request_headers = {**self._gateway_headers(), **(headers or {})}
        request = self._client.build_request(
            "POST", "/v1/responses", headers=request_headers, json=payload
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(0, f"bridge responses stream failed: {exc}") from exc
        try:
            yield response
        finally:
            await response.aclose()


def bridge_from_env() -> BuddyBridge:
    import os

    return BuddyBridge(
        base_url=os.environ.get("BUDDY2API_URL", "http://127.0.0.1:8787"),
        admin_token=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
        gateway_key=os.environ.get("WORKBUDDY_GATEWAY_KEY", ""),
    )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
