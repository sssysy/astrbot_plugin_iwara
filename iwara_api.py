"""IwaraAPI — thin facade over HttpExecutor + TokenManager.

This module used to be a god facade holding HTTP transport, token lifecycle,
warmup, and Cloudflare fallback all in one class. Those concerns are now
split:
- :mod:`iwara_http`  → HTTP transport, warmup, CF fallback, base-URL retry
- :mod:`iwara_token` → access-token cache / login / refresh, persisted to KV

IwaraAPI only wires them together and exposes the business entry point
:meth:`get_json`. The token link is created via
``HttpExecutor.set_token_provider(TokenManager.get_access_token)`` — a
callable injection that breaks the circular dependency (TokenManager needs
HttpExecutor to login; HttpExecutor needs TokenManager to authorize) without
a non-reentrant-lock deadlock.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .iwara_http import CloudflareBlocked, HttpExecutor
from .iwara_token import KeyValueStore, TokenManager

__all__ = ["IwaraAPI", "CloudflareBlocked", "KeyValueStore"]


class IwaraAPI:
    """Business entry point for Iwara API calls.

    Args:
        config: plugin config dict.
        storage: persistent KV store for refreshToken (see AstrBot storage spec).
    """

    def __init__(self, config: Dict[str, Any], storage: KeyValueStore):
        self._config = config
        self._http = HttpExecutor(config)
        self._tokens = TokenManager(config, self._http, storage)
        # Break the cycle: HttpExecutor calls the callable (not TokenManager
        # directly) to obtain each request's access token.
        self._http.set_token_provider(self._tokens.get_access_token)

    @property
    def warmup_done(self) -> bool:
        return self._http.warmup_done

    async def get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_file_api: bool = False,
    ) -> Any:
        """GET *path* on the Iwara API and return parsed JSON."""
        return await self._http.get_json(path, params, headers, use_file_api)

    async def get_session(self):
        """Expose the aiohttp session for image downloads (raw HTTP GET
        needed by :func:`iwara_commands.download_image`)."""
        return await self._http._get_session()

    async def close(self):
        await self._http.close()
