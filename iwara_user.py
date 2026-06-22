"""User & subscription command handlers — profile, followers, following,
user videos/images, subscribe/unsubscribe/list, poll loop, notifications.

Each `handle_*` function receives the plugin instance; main.py delegates
to them in one-liners.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from astrbot.api import logger

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    extract_command_payload,
    extract_author,
    extract_items,
    get_int_config,
    site_host,
)
from .iwara_format import format_search_item, format_user_profile
from .iwara_image import extract_avatar_url, get_display_image_url
from .iwara_commands import make_chain
from .iwara_subscribe import SubscriptionStore, poll_user_content


# ── profile ─────────────────────────────────────────────

async def handle_user(plugin, event):
    """查询用户信息。/iwara_user <用户名>"""
    query = extract_command_payload(event.message_str, "iwara_user").strip()
    if not query:
        yield event.plain_result("用法：/iwara_user <用户名>")
        return
    try:
        data = await plugin._api.get_json(f"/profile/{query}")
        yield await make_chain(
            event, plugin.config, plugin._api,
            format_user_profile(data, query, site_host(plugin.config)),
            extract_avatar_url(data),
        )
    except Exception as exc:
        logger.error(f"iwara_user failed: {exc}")
        yield event.plain_result(f"查询用户失败：{exc}")


# ── followers / following ───────────────────────────────

async def handle_followers(plugin, event):
    """查询用户粉丝列表。/iwara_followers <用户名>"""
    username = extract_command_payload(event.message_str, "iwara_followers").strip()
    if not username:
        yield event.plain_result("用法：/iwara_followers <用户名>")
        return
    try:
        user_id = await _resolve_user_id(plugin._api, username)
        if not user_id:
            yield event.plain_result(f"未找到用户 {username}。")
            return
        items = extract_items(
            await plugin._api.get_json(f"/user/{user_id}/followers", params={"limit": 10}))
    except Exception as exc:
        yield event.plain_result(f"查询粉丝失败：{exc}")
        return
    if not items:
        yield event.plain_result(f"{username} 暂无粉丝。")
        return
    yield event.plain_result(
        "\n".join([f"{username} 的粉丝："] +
                  [f"[{idx}] {extract_author(item)}" for idx, item in enumerate(items[:10], start=1)]))


async def handle_following(plugin, event):
    """查询用户关注列表。/iwara_following <用户名>"""
    username = extract_command_payload(event.message_str, "iwara_following").strip()
    if not username:
        yield event.plain_result("用法：/iwara_following <用户名>")
        return
    try:
        user_id = await _resolve_user_id(plugin._api, username)
        if not user_id:
            yield event.plain_result(f"未找到用户 {username}。")
            return
        items = extract_items(
            await plugin._api.get_json(f"/user/{user_id}/following", params={"limit": 10}))
    except Exception as exc:
        yield event.plain_result(f"查询关注失败：{exc}")
        return
    if not items:
        yield event.plain_result(f"{username} 暂无关注。")
        return
    yield event.plain_result(
        "\n".join([f"{username} 的关注："] +
                  [f"[{idx}] {extract_author(item)}" for idx, item in enumerate(items[:10], start=1)]))


# ── user videos / images ────────────────────────────────

async def handle_uservideos(plugin, event):
    """查询用户视频列表。/iwara_uservideos <用户名>"""
    username = extract_command_payload(event.message_str, "iwara_uservideos").strip()
    if not username:
        yield event.plain_result("用法：/iwara_uservideos <用户名>")
        return
    try:
        user_id = await _resolve_user_id(plugin._api, username)
        if not user_id:
            yield event.plain_result(f"未找到用户 {username}。")
            return
        limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
        items = extract_items(await plugin._api.get_json(
            "/videos", params={"sort": "date", "rating": "all", "user": user_id, "limit": limit}))
    except Exception as exc:
        yield event.plain_result(f"查询用户视频失败：{exc}")
        return
    if not items:
        yield event.plain_result(f"{username} 暂无视频。")
        return
    host = site_host(plugin.config)
    for idx, item in enumerate(items, start=1):
        yield await make_chain(event, plugin.config, plugin._api,
                               format_search_item(idx, item, "video", host),
                               get_display_image_url(item))


async def handle_userimages(plugin, event):
    """查询用户图片列表。/iwara_userimages <用户名>"""
    username = extract_command_payload(event.message_str, "iwara_userimages").strip()
    if not username:
        yield event.plain_result("用法：/iwara_userimages <用户名>")
        return
    try:
        user_id = await _resolve_user_id(plugin._api, username)
        if not user_id:
            yield event.plain_result(f"未找到用户 {username}。")
            return
        limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
        items = extract_items(await plugin._api.get_json(
            "/images", params={"sort": "date", "rating": "all", "user": user_id, "limit": limit}))
    except Exception as exc:
        yield event.plain_result(f"查询用户图片失败：{exc}")
        return
    if not items:
        yield event.plain_result(f"{username} 暂无图片。")
        return
    host = site_host(plugin.config)
    for idx, item in enumerate(items, start=1):
        yield await make_chain(event, plugin.config, plugin._api,
                               format_search_item(idx, item, "image", host),
                               get_display_image_url(item))


# ── subscribe / unsubscribe / list ──────────────────────

async def handle_sub(plugin, event):
    """订阅博主更新。/iwara_sub <用户名>"""
    if plugin._store is None:
        yield event.plain_result("订阅功能未初始化。")
        return
    username = extract_command_payload(event.message_str, "iwara_sub").strip()
    if not username:
        yield event.plain_result("用法：/iwara_sub <用户名>")
        return
    try:
        profile = await plugin._api.get_json(f"/profile/{username}")
    except Exception as exc:
        yield event.plain_result(f"查询用户失败：{exc}")
        return
    user = profile.get("user", profile) if isinstance(profile, dict) else {}
    user_id = str(user.get("id", ""))
    if not user_id:
        yield event.plain_result(f"未找到用户 {username}。")
        return
    plugin._store.add_subscription(username, user_id, str(event.session))
    yield event.plain_result(f"✅ 已订阅 {username}！有新视频/图片时会通知你。")


async def handle_unsub(plugin, event):
    """取消订阅博主。/iwara_unsub <用户名>"""
    if plugin._store is None:
        yield event.plain_result("订阅功能未初始化。")
        return
    username = extract_command_payload(event.message_str, "iwara_unsub").strip()
    if not username:
        yield event.plain_result("用法：/iwara_unsub <用户名>")
        return
    removed = plugin._store.remove_subscription(username, str(event.session))
    yield event.plain_result(f"已取消订阅 {username}。" if removed else f"你未订阅 {username}。")


async def handle_sublist(plugin, event):
    """查看你的订阅列表。"""
    if plugin._store is None:
        yield event.plain_result("订阅功能未初始化。")
        return
    usernames = plugin._store.list_subscriptions_for_session(str(event.session))
    if not usernames:
        yield event.plain_result("你还没有订阅任何博主。\n使用 /iwara_sub <用户名> 订阅。")
        return
    yield event.plain_result(
        "\n".join(["你的订阅列表："] + [f"[{idx}] {name}" for idx, name in enumerate(usernames, start=1)]))


# ── poll loop ───────────────────────────────────────────

async def poll_all_subscriptions(plugin):
    """Cron handler: poll all subscribed users for new content."""
    if plugin._store is None:
        return
    subs = plugin._store.list_all()
    if not subs:
        return
    host = site_host(plugin.config)
    for username in list(subs.keys()):
        try:
            new_videos, new_images = await poll_user_content(
                plugin._api, plugin._store, username)
        except Exception as exc:
            logger.error(f"subscribe poll failed for {username}: {exc}")
            continue
        entry = plugin._store.list_all().get(username, {})
        for item in new_videos:
            vid = str(item.get("id", ""))
            title = str(item.get("title", "新视频"))
            text = f"🔔 订阅通知: {username} 发布了新视频!\n标题: {title}\nhttps://{host}/video/{vid}"
            await _notify_subscribers(plugin, entry, text, get_display_image_url(item))
        for item in new_images:
            iid = str(item.get("id", ""))
            title = str(item.get("title", "新图片"))
            text = f"🔔 订阅通知: {username} 发布了新图片!\n标题: {title}\nhttps://{host}/image/{iid}"
            await _notify_subscribers(plugin, entry, text, get_display_image_url(item))


async def _notify_subscribers(plugin, entry: Dict[str, Any], text: str, image_url: Optional[str]):
    """Send notification to all subscribers of *entry*."""
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.message.components import Plain

    for sub in entry.get("subscribers", []):
        session_str = sub.get("session_str", "")
        if not session_str:
            continue
        try:
            chain = MessageChain(chain=[Plain(text=text)])
            await plugin.context.send_message(session_str, chain)
        except Exception as exc:
            logger.warning(f"notify {session_str} failed: {exc}")


# ── helpers ─────────────────────────────────────────────

async def _resolve_user_id(api: IwaraAPI, username: str) -> str:
    """Resolve a username to user UUID via /profile."""
    profile = await api.get_json(f"/profile/{username}")
    user = profile.get("user", profile) if isinstance(profile, dict) else {}
    return str(user.get("id", ""))
