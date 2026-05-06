#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open browser, login iwara.tv, then export AstrBot plugin runtime config."
    )
    parser.add_argument(
        "--output",
        default="iwara_runtime_config.txt",
        help="Output txt file path (default: iwara_runtime_config.txt)",
    )
    parser.add_argument(
        "--browser",
        default="chrome",
        choices=["chrome", "msedge", "chromium"],
        help="Browser channel used by Playwright (default: chrome)",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Optional browser proxy server, e.g. http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--profile-dir",
        default=".iwara_playwright_profile",
        help="Persistent profile directory (default: .iwara_playwright_profile)",
    )
    return parser.parse_args()


def _safe_preview(value: str, head: int = 8, tail: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def _extract_bearer_token(storage_obj: Dict[str, str]) -> str:
    preferred_keys = [
        "token",
        "access_token",
        "accessToken",
        "auth_token",
        "id_token",
        "refresh_token",
        "jwt",
        "Authorization",
    ]
    jwt_re = re.compile(r"^[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+$")

    def normalize_token(raw: str) -> str:
        text = (raw or "").strip().strip("\"'")
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        return text

    def iter_strings(obj: Any, depth: int = 0):
        if depth > 6:
            return
        if isinstance(obj, str):
            s = obj.strip()
            if not s:
                return
            yield s
            if s[:1] in ("{", "["):
                try:
                    parsed = json.loads(s)
                except Exception:
                    return
                yield from iter_strings(parsed, depth + 1)
            return
        if isinstance(obj, dict):
            for v in obj.values():
                yield from iter_strings(v, depth + 1)
            return
        if isinstance(obj, list):
            for v in obj:
                yield from iter_strings(v, depth + 1)

    # 1) strict key matching first
    for k, v in storage_obj.items():
        if k in preferred_keys:
            candidate = normalize_token(v)
            if candidate:
                return candidate

    # 2) fuzzy key matching + recursive value parsing
    fuzzy_keys = ("token", "auth", "jwt", "bearer")
    for k, v in storage_obj.items():
        if any(mark in k.lower() for mark in fuzzy_keys):
            for s in iter_strings(v):
                candidate = normalize_token(s)
                if not candidate:
                    continue
                if jwt_re.fullmatch(candidate):
                    return candidate
                if len(candidate) >= 24 and any(ch in candidate for ch in ".-_"):
                    return candidate

    # 3) global scan in all storage values
    for v in storage_obj.values():
        for s in iter_strings(v):
            candidate = normalize_token(s)
            if jwt_re.fullmatch(candidate):
                return candidate
    return ""


def _build_cookie_header(cookies: List[Dict[str, Any]]) -> str:
    # Keep cookies only for iwara-related domains.
    pairs: List[str] = []
    for c in cookies:
        domain = str(c.get("domain", ""))
        if "iwara.tv" not in domain:
            continue
        name = str(c.get("name", "")).strip()
        value = str(c.get("value", "")).strip()
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _wait_any_key() -> None:
    try:
        import msvcrt  # type: ignore

        print("按任意键退出...")
        msvcrt.getch()
    except Exception:
        input("按回车退出...")


def _build_plain_text_lines(
    user_agent: str, referer: str, origin: str, cookie_header: str, bearer_token: str
) -> str:
    lines = [
        f"User-Agent: {user_agent}",
        f"Referer: {referer}",
        f"Origin: {origin}",
        f"Cookie: {cookie_header}",
        f"Bearer-Token: {bearer_token}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("缺少依赖: playwright")
        print("请先执行: python -m pip install playwright")
        print("然后执行: python -m playwright install chromium")
        return 1

    output_path = Path(args.output).resolve()
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("即将打开浏览器，请在页面中登录 Iwara 并通过 Cloudflare 验证。")
    print("完成后回到终端按回车继续导出配置。")
    print(f"浏览器 profile 目录: {profile_dir}")

    with sync_playwright() as p:
        browser_type = p.chromium
        launch_kwargs: Dict[str, Any] = {
            "headless": False,
            "channel": None if args.browser == "chromium" else args.browser,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        }
        if args.proxy.strip():
            launch_kwargs["proxy"] = {"server": args.proxy.strip()}

        context = browser_type.launch_persistent_context(
            str(profile_dir), **launch_kwargs
        )
        try:
            page = context.new_page()
            page.goto("https://www.iwara.tv/", wait_until="domcontentloaded")
            page.bring_to_front()

            input("登录完成后按回车导出配置...")

            # Ensure page is on iwara domain for localStorage reading.
            if "iwara.tv" not in page.url:
                page.goto("https://www.iwara.tv/", wait_until="domcontentloaded")

            user_agent = page.evaluate("() => navigator.userAgent")
            local_storage = page.evaluate(
                """() => {
                    const out = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (!key) continue;
                        const val = localStorage.getItem(key) || "";
                        out[key] = val;
                    }
                    return out;
                }"""
            )
            session_storage = page.evaluate(
                """() => {
                    const out = {};
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        if (!key) continue;
                        const val = sessionStorage.getItem(key) || "";
                        out[key] = val;
                    }
                    return out;
                }"""
            )
            merged_storage: Dict[str, str] = {}
            for k, v in local_storage.items():
                merged_storage[f"local:{k}"] = v
            for k, v in session_storage.items():
                merged_storage[f"session:{k}"] = v

            cookies = context.cookies()

            cookie_header = _build_cookie_header(cookies)
            bearer_token = _extract_bearer_token(merged_storage)

            referer = "https://www.iwara.tv/"
            origin = "https://www.iwara.tv"

            output_path.write_text(
                _build_plain_text_lines(
                    user_agent=user_agent,
                    referer=referer,
                    origin=origin,
                    cookie_header=cookie_header,
                    bearer_token=bearer_token,
                ),
                encoding="utf-8",
            )

            print("\n导出完成:")
            print(f"- 文本文件: {output_path}")
            print(f"- Cookie 长度: {len(cookie_header)}")
            print(f"- 含 cf_clearance: {'cf_clearance=' in cookie_header}")
            print(
                f"- Bearer Token: {'已捕获 (' + _safe_preview(bearer_token) + ')' if bearer_token else '未捕获'}"
            )
            print("\n可直接复制该 TXT 内容到 VPS 上 astrbot_plugin_iwara 配置。")
        finally:
            context.close()

    _wait_any_key()
    return 0


if __name__ == "__main__":
    sys.exit(main())
