from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import aiohttp
from astrbot.api import logger
from yarl import URL

try:
    import cloudscraper  # type: ignore
except Exception:
    cloudscraper = None

from .iwara_helpers import (
    get_str_config,
    get_bool_config,
    get_int_config,
    proxy_url,
    build_request_headers,
    format_http_error,
)


_CF_ERROR_KW = ("cloudflare", "cf-chl", "just a moment", "cf-mitigated", "turnstile", "challenges.cloudflare.com")


def _is_cf_error_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in _CF_ERROR_KW)


class CloudflareBlocked(RuntimeError):
    """Cloudflare 拦截：aiohttp 与 cloudscraper 均失败，不再尝试其他 base URL。"""
    pass


class IwaraAPI:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._scraper = None
        self._warmup_done = False
        self._warmup_lock = asyncio.Lock()

    @property
    def warmup_done(self) -> bool:
        return self._warmup_done

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._scraper is not None:
            try:
                self._scraper.close()
            except Exception:
                pass
        self._scraper = None
        self._warmup_done = False

    async def get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_file_api: bool = False,
    ) -> Any:
        bases = self._candidate_api_bases(use_file_api)
        last_exc: Optional[Exception] = None
        for idx, base in enumerate(bases):
            url = f"{base.rstrip('/')}/{path.lstrip('/')}"
            try:
                return await self._request_once(url=url, params=params, headers=headers)
            except CloudflareBlocked:
                raise
            except Exception as exc:
                last_exc = exc
                if idx < len(bases) - 1 and self._is_retryable(exc):
                    logger.warning(f"request failed on {base}, try next base: {exc}")
                    continue
                raise
        raise RuntimeError(f"请求失败：{last_exc}")

    async def _request_once(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        merged = self._build_default_headers()
        cookie = get_str_config(self.config, "request_cookie", "")
        if cookie:
            merged["Cookie"] = cookie
        bearer = get_str_config(self.config, "request_bearer_token", "")
        if bearer and "Authorization" not in merged:
            merged["Authorization"] = (
                bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
            )
        if headers:
            merged.update(headers)
        session = await self._get_session()
        await self._ensure_warmup(session, merged)
        px = proxy_url(self.config)
        engine = self._request_engine()
        if engine == "cloudscraper":
            return await self._via_cloudscraper(url, params, merged, px)
        async with session.get(url, params=params, headers=merged, proxy=px) as resp:
            content = await resp.text()
            if resp.status >= 400:
                if self._should_cs_fallback(resp.status, content):
                    logger.warning(
                        "aiohttp got Cloudflare challenge, retry via cloudscraper."
                    )
                    try:
                        return await self._via_cloudscraper(url, params, merged, px)
                    except Exception as cs_exc:
                        err_text = str(cs_exc)
                        if _is_cf_error_text(err_text) or "403" in err_text:
                            raise CloudflareBlocked(
                                format_http_error(resp.status, content, bool(px), self.config)
                            ) from cs_exc
                        raise
                raise RuntimeError(
                    format_http_error(resp.status, content, bool(px), self.config)
                )
            if not content.strip():
                return {}
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                raise RuntimeError("响应不是合法 JSON。")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout_sec = get_int_config(self.config, "request_timeout_sec", 15, 5, 60)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
                trust_env=True,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
            self._apply_manual_cookies(self._session)
            self._warmup_done = False
        return self._session

    def _request_engine(self) -> str:
        engine = get_str_config(self.config, "request_engine", "auto").lower()
        return engine if engine in {"auto", "aiohttp", "cloudscraper"} else "auto"

    async def _ensure_warmup(
        self, session: aiohttp.ClientSession, headers: Dict[str, str]
    ):
        if self._warmup_done:
            return
        async with self._warmup_lock:
            if self._warmup_done:
                return
            if not get_bool_config(self.config, "warmup_homepage", True):
                self._warmup_done = True
                return
            warmup_url = (
                get_str_config(self.config, "request_referer", "https://www.iwara.tv/")
                or "https://www.iwara.tv/"
            )
            px = proxy_url(self.config)
            try:
                async with session.get(
                    warmup_url, headers=headers, proxy=px, allow_redirects=True
                ) as resp:
                    await resp.text()
                    logger.info(f"iwara warmup status: {resp.status}")
            except Exception as exc:
                logger.warning(f"iwara warmup failed: {exc}")
            finally:
                self._warmup_done = True

    def _apply_manual_cookies(self, session: aiohttp.ClientSession):
        cookie_text = get_str_config(self.config, "request_cookie", "")
        if not cookie_text:
            return
        cookies: Dict[str, str] = {}
        for chunk in cookie_text.split(";"):
            part = chunk.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key, value = key.strip(), value.strip()
            if key:
                cookies[key] = value
        if not cookies:
            return
        for target in [
            "https://www.iwara.tv/",
            "https://api.iwara.tv/",
            "https://files.iwara.tv/",
            "https://apiq.iwara.tv/",
            "https://filesq.iwara.tv/",
        ]:
            session.cookie_jar.update_cookies(cookies, response_url=URL(target))

    def _should_cs_fallback(self, status: int, content: str) -> bool:
        if self._request_engine() != "auto" or cloudscraper is None or status != 403:
            return False
        return self._is_cf_challenge(content)

    @staticmethod
    def _is_cf_challenge(content: str) -> bool:
        return _is_cf_error_text(content)

    def _get_scraper(self):
        if cloudscraper is None:
            raise RuntimeError("cloudscraper 未安装。请安装：pip install cloudscraper")
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        return self._scraper

    async def _via_cloudscraper(
        self,
        url: str,
        params: Optional[Dict[str, Any]],
        headers: Dict[str, str],
        px: Optional[str],
    ) -> Any:
        timeout_sec = get_int_config(self.config, "request_timeout_sec", 15, 5, 60)
        proxy_map = {"http": px, "https": px} if px else None
        req_headers = {**headers, "Accept-Encoding": "gzip, deflate"}

        def _do():
            return self._get_scraper().get(
                url,
                params=params,
                headers=req_headers,
                proxies=proxy_map,
                timeout=timeout_sec,
            )

        status, content = await asyncio.to_thread(
            lambda: (r := _do(), r.status_code, r.text)[1:]
        )
        if status >= 400:
            raise RuntimeError(
                format_http_error(status, content, bool(px), self.config)
            )
        if not content.strip():
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise RuntimeError("响应不是合法 JSON。")

    def _build_default_headers(self) -> Dict[str, str]:
        return build_request_headers(self.config)

    def _candidate_api_bases(self, use_file_api: bool) -> List[str]:
        if use_file_api:
            primary = get_str_config(
                self.config, "file_api_base_url", "https://files.iwara.tv"
            )
            candidates = [primary, "https://files.iwara.tv", "https://filesq.iwara.tv"]
        else:
            primary = get_str_config(
                self.config, "api_base_url", "https://api.iwara.tv"
            )
            candidates = [primary, "https://api.iwara.tv", "https://apiq.iwara.tv"]
        ordered: List[str] = []
        for item in candidates:
            val = (item or "").strip().rstrip("/")
            if val and val not in ordered:
                ordered.append(val)
        return ordered

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        text = str(exc).lower()
        if "cloudflare challenge" in text:
            return True
        return any(
            c in text
            for c in [
                "http 403",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
            ]
        )
