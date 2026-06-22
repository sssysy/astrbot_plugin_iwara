"""Iwara API connectivity probe.

Uses HttpExecutor internals directly (session, scraper, CF detection)
to measure per-endpoint health — this is a diagnostic tool, not business
logic, so it reaches into HTTP-layer details that IwaraAPI's facade
intentionally hides.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict

import aiohttp

try:
    import cloudscraper  # type: ignore
except Exception:
    cloudscraper = None

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    get_str_config,
    get_int_config,
    proxy_url,
    build_request_headers,
    looks_binary_body_text,
)


async def run_probe(api: IwaraAPI) -> str:
    config = api._config
    http = api._http
    px = proxy_url(config)
    engine = http._request_engine()
    api_base = get_str_config(config, "api_base_url", "https://api.iwara.tv").rstrip(
        "/"
    )
    file_base = get_str_config(
        config, "file_api_base_url", "https://files.iwara.tv"
    ).rstrip("/")
    www_url = (
        get_str_config(config, "request_referer", "https://www.iwara.tv/")
        or "https://www.iwara.tv/"
    )

    targets = [
        ("www", www_url),
        ("api", f"{api_base}/videos?limit=1&sort=date&rating=all"),
        ("files", f"{file_base}/"),
    ]
    lines = [
        "Iwara 连通性探测",
        f"request_engine: {engine}",
        f"proxy_url: {'已配置' if px else '未配置'}",
        f"cloudscraper_available: {cloudscraper is not None}",
        "",
    ]
    for label, url in targets:
        result = await _probe_single(http, config, url)
        cf = "yes" if result["is_cf"] else "no"
        lines.append(
            f"[{label}] {result['status']} | engine={result['engine']} | cf={cf} | {result['cost_ms']}ms"
        )
        lines.append(f"url: {url}")
        if result["preview"]:
            lines.append(f"preview: {result['preview']}")
        if result["error"]:
            lines.append(f"error: {result['error']}")
        lines.append("")
    if not px:
        lines.append("提示: 当前未启用代理，若持续 403 建议配置可用 HTTP 代理。")
    lines.append("提示: cf_clearance 通常绑定 IP+UA；跨机器/网络复制常失效。")
    return "\n".join(lines)


async def _probe_single(http, config: Dict[str, Any], url: str) -> Dict[str, Any]:
    engine = http._request_engine()
    if engine == "cloudscraper":
        return await _probe_cs(http, config, url)
    if engine == "aiohttp":
        return await _probe_aiohttp(http, config, url)
    first = await _probe_aiohttp(http, config, url)
    from .iwara_http import _is_cf_error_text
    if first["status"] == 403 and first["is_cf"] and cloudscraper is not None:
        second = await _probe_cs(http, config, url)
        if second["status"] != 403:
            return second
        second["engine"] = "aiohttp->cloudscraper"
        return second
    return first


async def _probe_aiohttp(http, config: Dict[str, Any], url: str) -> Dict[str, Any]:
    from .iwara_http import _is_cf_error_text
    start = time.perf_counter()
    session = await http._get_session()
    px = proxy_url(config)
    hdrs = _build_probe_headers(config)
    timeout_sec = get_int_config(config, "request_timeout_sec", 15, 5, 60)
    try:
        async with session.get(
            url,
            headers=hdrs,
            proxy=px,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
            allow_redirects=True,
        ) as resp:
            text = _decode_preview(await resp.read())
            return {
                "status": resp.status,
                "engine": "aiohttp",
                "is_cf": _is_cf_error_text(text)
                or (resp.status == 403 and looks_binary_body_text(text)),
                "preview": _safe_preview(text),
                "error": "",
                "cost_ms": int((time.perf_counter() - start) * 1000),
            }
    except Exception as exc:
        return {
            "status": "ERR",
            "engine": "aiohttp",
            "is_cf": False,
            "preview": "",
            "error": str(exc),
            "cost_ms": int((time.perf_counter() - start) * 1000),
        }


async def _probe_cs(http, config: Dict[str, Any], url: str) -> Dict[str, Any]:
    from .iwara_http import _is_cf_error_text
    start = time.perf_counter()
    px = proxy_url(config)
    timeout_sec = get_int_config(config, "request_timeout_sec", 15, 5, 60)
    proxy_map = {"http": px, "https": px} if px else None
    hdrs = _build_probe_headers(config)

    def _do():
        return http._get_scraper().get(
            url, headers=hdrs, proxies=proxy_map, timeout=timeout_sec
        )

    try:
        response = await asyncio.to_thread(_do)
        text = response.text
        return {
            "status": response.status_code,
            "engine": "cloudscraper",
            "is_cf": _is_cf_error_text(text)
            or (response.status_code == 403 and looks_binary_body_text(text)),
            "preview": _safe_preview(text),
            "error": "",
            "cost_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception as exc:
        return {
            "status": "ERR",
            "engine": "cloudscraper",
            "is_cf": False,
            "preview": "",
            "error": str(exc),
            "cost_ms": int((time.perf_counter() - start) * 1000),
        }


def _build_probe_headers(config: dict) -> Dict[str, str]:
    hdrs = build_request_headers(config)
    hdrs["Accept"] = "application/json, text/plain, */*"
    hdrs["Cookie"] = get_str_config(config, "request_cookie", "")
    return hdrs


def _decode_preview(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            return raw.decode("latin-1", errors="replace")
        except Exception:
            return ""


def _safe_preview(text: str) -> str:
    if not text:
        return ""
    if looks_binary_body_text(text):
        return "<binary body>"
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:160] + "..." if len(clean) > 160 else clean
