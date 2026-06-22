"""Token manager for Iwara API — extracted from IwaraAPI god facade.

Owns access-token lifecycle: cache → refresh → login. Persists refreshToken
to an injected KeyValueStore so it survives process restarts. Depends on a
minimal HttpCaller protocol (not the full HttpExecutor) for login/refresh
requests, which always carry skip_auth=True to avoid re-entering the token
lock (asyncio.Lock is non-reentrant).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

try:
    from .iwara_helpers import get_str_config
    from astrbot.api import logger
except ImportError:
    from iwara_helpers import get_str_config  # type: ignore[no-redef]
    from astrbot.api import logger  # type: ignore[no-redef]


REFRESH_TOKEN_KEY = "iwara_refresh_token"


class KeyValueStore(Protocol):
    """Minimal KV storage interface (matches AstrBot's put/get/delete_kv_data)."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...


class HttpCaller(Protocol):
    """Minimal HTTP interface TokenManager needs for login/refresh."""

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        skip_auth: bool = False,
    ) -> Any: ...


class TokenManager:
    """Manages Iwara access token lifecycle.

    Priority on each :meth:`get_access_token` call:
    1. Explicit ``request_bearer_token`` config → returned as-is.
    2. Cached access token still valid (>5 min to expiry) → returned.
    3. Stored refreshToken → silent refresh via POST /user/token.
    4. Email + password → full login via POST /user/login.

    refreshToken is persisted to *storage* so a process restart can silent-
    refresh without re-sending credentials.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        http: HttpCaller,
        storage: KeyValueStore,
    ):
        self._config = config
        self._http = http
        self._storage = storage
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_lock = asyncio.Lock()
        self._token_expiry: float = 0.0
        self._loaded = False

    # ── public ─────────────────────────────────────────────

    async def get_access_token(self) -> str:
        """Return a usable Bearer token string (without 'Bearer ' prefix),
        or '' if no auth is available."""
        explicit = get_str_config(self._config, "request_bearer_token", "")
        if explicit:
            return explicit if explicit.lower().startswith("bearer ") else explicit
        async with self._token_lock:
            await self._ensure_loaded()
            # 1. cached & valid
            if self._access_token and time.time() < self._token_expiry - 300:
                return self._access_token
            # 2. silent refresh
            if self._refresh_token:
                at = await self._refresh()
                if at:
                    self._access_token = at
                    self._token_expiry = time.time() + 3600
                    return at
            # 3. full login
            at = await self._login()
            if at:
                self._access_token = at
                self._token_expiry = time.time() + 3600
                return at
        return ""

    # ── internals ──────────────────────────────────────────

    async def _ensure_loaded(self) -> None:
        """Lazily load persisted refreshToken on first call."""
        if self._loaded:
            return
        try:
            rt = await self._storage.get(REFRESH_TOKEN_KEY)
            if rt:
                self._refresh_token = rt
        except Exception as exc:
            logger.warning(f"failed to load refresh token from storage: {exc}")
        self._loaded = True

    def _api_base(self) -> str:
        return get_str_config(
            self._config, "api_base_url", "https://api.iwara.tv"
        ).rstrip("/")

    async def _login(self) -> Optional[str]:
        """Exchange email+password for access token. Persists refreshToken."""
        email = get_str_config(self._config, "request_login_email", "")
        password = get_str_config(self._config, "request_login_password", "")
        if not email or not password:
            return None
        try:
            data = await self._http.request(
                url=f"{self._api_base()}/user/login",
                method="POST",
                body=json.dumps({"email": email, "password": password}),
                skip_auth=True,
            )
            if not isinstance(data, dict):
                return None
            at = data.get("accessToken") or data.get("token")
            rt = data.get("refreshToken") or data.get("refresh_token")
            if rt:
                self._refresh_token = rt
                await self._persist_refresh_token(rt)
            if at:
                logger.info("Iwara auto-login: access token obtained.")
                return at
        except Exception as exc:
            logger.warning(f"iwara auto-login failed: {exc}")
        return None

    async def _refresh(self) -> Optional[str]:
        """Exchange refreshToken for a new accessToken. Clears stored
        refresh token on failure so next call falls back to full login."""
        if not self._refresh_token:
            return None
        try:
            data = await self._http.request(
                url=f"{self._api_base()}/user/token",
                method="POST",
                headers={"Authorization": f"Bearer {self._refresh_token}"},
                skip_auth=True,
            )
            if isinstance(data, dict):
                at = data.get("accessToken") or data.get("token")
                if at:
                    logger.info("Iwara token refreshed via refreshToken.")
                    return at
            # refresh token invalid/expired
            await self._clear_refresh_token()
        except Exception as exc:
            logger.warning(f"iwara token refresh failed: {exc}")
            await self._clear_refresh_token()
        return None

    async def _persist_refresh_token(self, rt: str) -> None:
        try:
            await self._storage.set(REFRESH_TOKEN_KEY, rt)
        except Exception as exc:
            logger.warning(f"failed to persist refresh token: {exc}")

    async def _clear_refresh_token(self) -> None:
        self._refresh_token = None
        try:
            await self._storage.delete(REFRESH_TOKEN_KEY)
        except Exception as exc:
            logger.warning(f"failed to delete refresh token from storage: {exc}")


# ── Built-in storage implementations ────────────────────

class FileKVStore:
    """File-backed JSON KV store — fallback for AstrBot < v4.9.2.

    Stores all keys in a single JSON file under the plugin data directory
    (per AstrBot's storage spec for large files).
    """

    def __init__(self, path: "Path"):
        import json as _json
        self._path = path
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        import json as _json
        if self._path.exists():
            try:
                text = self._path.read_text(encoding="utf-8")
                if text.strip():
                    return _json.loads(text)
            except (_json.JSONDecodeError, OSError):
                pass
        return {}

    def _flush(self) -> None:
        import json as _json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            _json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._flush()
