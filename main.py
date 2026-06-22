"""Iwara plugin for AstrBot — entry point.

Facade-pattern architecture: IwaraAPI → HttpExecutor + TokenManager.
Command handlers are split into:
- iwara_content.py  — search, video, image, direct, related, comments, likes, trending, diag, probe
- iwara_user.py     — profile, followers, following, uservideos, userimages, sub/unsub/sublist, poll
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .iwara_api import IwaraAPI
from .iwara_helpers import get_int_config, get_str_config, proxy_url
from .iwara_token import FileKVStore
from .iwara_subscribe import SubscriptionStore


class IwaraPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._store: Optional[SubscriptionStore] = None
        self._api: Optional[IwaraAPI] = None

    async def initialize(self):
        logger.info("astrbot_plugin_iwara initialized.")
        kv = self._make_kv_store()
        self._api = IwaraAPI(self.config, kv)
        from astrbot.api.star import StarTools
        data_dir = StarTools.get_data_dir()
        self._store = SubscriptionStore(data_dir / "subscriptions.json")
        poll_min = get_int_config(self.config, "subscribe_poll_interval_min", 30, 5, 1440)
        try:
            await self.context.cron_manager.add_basic_job(
                name="iwara_subscribe_poll",
                cron_expression=f"*/{poll_min} * * * *",
                handler=self._poll_all_subscriptions,
                persistent=True,
            )
            logger.info(f"iwara subscribe poll cron registered (every {poll_min} min)")
        except Exception as exc:
            logger.warning(f"Failed to register subscribe cron: {exc}")

    async def terminate(self):
        if self._api:
            await self._api.close()

    def _make_kv_store(self):
        if hasattr(self, "put_kv_data"):
            return _AstrBotKVStore(self)
        from pathlib import Path
        from astrbot.api.star import StarTools
        return FileKVStore(StarTools.get_data_dir() / "kv_store.json")

    # ── cron ────────────────────────────────────────────

    async def _poll_all_subscriptions(self):
        from .iwara_user import poll_all_subscriptions
        await poll_all_subscriptions(self)

    # ── content commands ─────────────────────────────────

    @filter.command("iwara_search")
    async def iwara_search(self, event: AstrMessageEvent):
        from .iwara_content import handle_search
        async for r in handle_search(self, event): yield r

    @filter.command("iwara_video")
    async def iwara_video(self, event: AstrMessageEvent):
        from .iwara_content import handle_video
        async for r in handle_video(self, event): yield r

    @filter.command("iwara_image")
    async def iwara_image(self, event: AstrMessageEvent):
        from .iwara_content import handle_image
        async for r in handle_image(self, event): yield r

    @filter.command("iwara_direct")
    async def iwara_direct(self, event: AstrMessageEvent):
        from .iwara_content import handle_direct
        async for r in handle_direct(self, event): yield r

    @filter.command("iwara_related")
    async def iwara_related(self, event: AstrMessageEvent):
        from .iwara_content import handle_related
        async for r in handle_related(self, event): yield r

    @filter.command("iwara_comments")
    async def iwara_comments(self, event: AstrMessageEvent):
        from .iwara_content import handle_comments
        async for r in handle_comments(self, event): yield r

    @filter.command("iwara_likes")
    async def iwara_likes(self, event: AstrMessageEvent):
        from .iwara_content import handle_likes
        async for r in handle_likes(self, event): yield r

    @filter.command("iwara_trending")
    async def iwara_trending(self, event: AstrMessageEvent):
        from .iwara_content import handle_trending
        async for r in handle_trending(self, event): yield r

    @filter.command("iwara_diag")
    async def iwara_diag(self, event: AstrMessageEvent):
        from .iwara_commands import cloudscraper_available
        cookie_text = get_str_config(self.config, "request_cookie", "")
        bearer = get_str_config(self.config, "request_bearer_token", "")
        px = proxy_url(self.config)
        email = get_str_config(self.config, "request_login_email", "")
        passwd = get_str_config(self.config, "request_login_password", "")
        yield event.plain_result("\n".join([
            "Iwara 插件诊断",
            f"api_base_url: {get_str_config(self.config, 'api_base_url', 'https://api.iwara.tv')}",
            f"file_api_base_url: {get_str_config(self.config, 'file_api_base_url', 'https://files.iwara.tv')}",
            f"request_engine: {get_str_config(self.config, 'request_engine', 'auto')}",
            f"cloudscraper_available: {cloudscraper_available()}",
            f"image_transport: {get_str_config(self.config, 'image_transport', 'bytes')}",
            f"proxy_url: {'已配置' if px else '未配置'}",
            f"warmup_homepage: {get_str_config(self.config, 'warmup_homepage', 'true')}",
            f"user_agent_len: {len(get_str_config(self.config, 'request_user_agent', ''))}",
            f"cookie_len: {len(cookie_text)}",
            f"cookie_has_cf_clearance: {'cf_clearance=' in cookie_text}",
            f"bearer_token: {'已配置' if bearer else '未配置'}",
            f"auto_login: {'已配置' if (email and passwd) else '未配置'}",
            f"warmup_done: {self._api.warmup_done}",
        ]))

    @filter.command("iwara_probe")
    async def iwara_probe(self, event: AstrMessageEvent):
        from .iwara_probe import run_probe
        try:
            yield event.plain_result(await run_probe(self._api))
        except Exception as exc:
            logger.error(f"iwara_probe failed: {exc}")
            yield event.plain_result(f"Iwara 探测失败：{exc}")

    # ── user / subscription commands ─────────────────────

    @filter.command("iwara_user")
    async def iwara_user(self, event: AstrMessageEvent):
        from .iwara_user import handle_user
        async for r in handle_user(self, event): yield r

    @filter.command("iwara_followers")
    async def iwara_followers(self, event: AstrMessageEvent):
        from .iwara_user import handle_followers
        async for r in handle_followers(self, event): yield r

    @filter.command("iwara_following")
    async def iwara_following(self, event: AstrMessageEvent):
        from .iwara_user import handle_following
        async for r in handle_following(self, event): yield r

    @filter.command("iwara_uservideos")
    async def iwara_uservideos(self, event: AstrMessageEvent):
        from .iwara_user import handle_uservideos
        async for r in handle_uservideos(self, event): yield r

    @filter.command("iwara_userimages")
    async def iwara_userimages(self, event: AstrMessageEvent):
        from .iwara_user import handle_userimages
        async for r in handle_userimages(self, event): yield r

    @filter.command("iwara_sub")
    async def iwara_sub(self, event: AstrMessageEvent):
        from .iwara_user import handle_sub
        async for r in handle_sub(self, event): yield r

    @filter.command("iwara_unsub")
    async def iwara_unsub(self, event: AstrMessageEvent):
        from .iwara_user import handle_unsub
        async for r in handle_unsub(self, event): yield r

    @filter.command("iwara_sublist")
    async def iwara_sublist(self, event: AstrMessageEvent):
        from .iwara_user import handle_sublist
        async for r in handle_sublist(self, event): yield r


# ── KV Storage Adapter ──────────────────────────────────

class _AstrBotKVStore:
    """Adapter: delegates to AstrBot's built-in put/get/delete_kv_data (v4.9.2+)."""
    def __init__(self, star): self._star = star
    async def get(self, key: str, default: Any = None) -> Any:
        return await self._star.get_kv_data(key, default)
    async def set(self, key: str, value: Any) -> None:
        await self._star.put_kv_data(key, value)
    async def delete(self, key: str) -> None:
        await self._star.delete_kv_data(key)
