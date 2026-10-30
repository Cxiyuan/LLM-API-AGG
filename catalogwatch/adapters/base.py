from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config import ProviderConfig
from ..models import ModelRecord


class AdapterError(RuntimeError):
    """Provider returned something we cannot use."""


class MissingKeyError(AdapterError):
    """A required API key is not configured."""


class AuthError(AdapterError):
    """The provider rejected the configured key."""


class BaseAdapter(ABC):
    def __init__(
        self,
        cfg: ProviderConfig,
        client: httpx.AsyncClient,
        api_key: str | None,
    ):
        self.cfg = cfg
        self.client = client
        self.api_key = api_key

    def ensure_key(self) -> None:
        if self.cfg.auth == "none":
            return
        if self.cfg.key_required and not self.api_key:
            raise MissingKeyError(f"{self.cfg.key_env or 'API key'} is not set")

    def request_parts(self) -> tuple[dict[str, str], dict[str, Any]]:
        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, Any] = {}
        if self.api_key:
            if self.cfg.auth == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif self.cfg.auth == "query":
                params[self.cfg.auth_param] = self.api_key
            elif self.cfg.auth == "header":
                headers[self.cfg.auth_param] = self.api_key
        return headers, params

    @staticmethod
    def check_response(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise AuthError(f"HTTP {response.status_code} (check the API key and its permissions)")
        if response.status_code == 429:
            raise AdapterError("HTTP 429 (provider rate limited the catalog request)")
        if response.status_code >= 400:
            snippet = response.text[:200].replace("\n", " ")
            raise AdapterError(f"HTTP {response.status_code}: {snippet}")

    @abstractmethod
    async def fetch_models(self) -> list[ModelRecord]:
        raise NotImplementedError
