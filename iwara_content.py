"""Content command handlers — search, video/image detail, direct links,
related, comments, likes, trending.

Each `handle_*` function is a regular async generator that receives the
plugin instance instead of `self`, so main.py stays thin.
"""
from __future__ import annotations

from typing import Any, Dict, List

from astrbot.api import logger

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    extract_command_payload,
    extract_image_id,
    extract_items,
    extract_video_id,
    get_int_config,
    get_str_config,
    get_text,
    parse_search_payload,
    proxy_url,
    site_host,
    normalize_url,
    HTML_TAG_RE,
)
from .iwara_format import format_search_item, format_video_detail, format_image_detail
from .iwara_image import get_display_image_url
from .iwara_commands import fetch_quality_list, make_chain, resolve_cover_url, search_items


# ── search ──────────────────────────────────────────────

async def handle_search(plugin, event):
    """搜索 Iwara 内容。/iwara_search [video|image|all] <关键词>"""
    payload = extract_command_payload(event.message_str, "iwara_search")
    media_type, keyword = parse_search_payload(payload)
    if not keyword:
        yield event.plain_result("用法：/iwara_search [video|image|all] <关键词>")
        return
    limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
    try:
        items = await search_items(plugin._api, plugin.config, keyword, media_type, limit)
    except Exception as exc:
        logger.error(f"iwara_search failed: {exc}")
        yield event.plain_result(f"Iwara 搜索失败：{exc}")
        return
    if not items:
        yield event.plain_result(f'没有找到与"{keyword}"相关的内容。')
        return
    host = site_host(plugin.config)
    for idx, item in enumerate(items, start=1):
        t = str(item.get("_media_type", media_type))
        text = format_search_item(idx, item, t, host)
        image_url = await resolve_cover_url(plugin._api, item, t)
        yield await make_chain(event, plugin.config, plugin._api, text, image_url)


# ── video / image detail ────────────────────────────────

async def handle_video(plugin, event):
    """查询视频详情。/iwara_video <视频ID或链接>"""
    video_id = extract_video_id(extract_command_payload(event.message_str, "iwara_video"))
    if not video_id:
        yield event.plain_result("用法：/iwara_video <视频ID或链接>")
        return
    try:
        data = await plugin._api.get_json(f"/video/{video_id}")
        yield await make_chain(
            event, plugin.config, plugin._api,
            format_video_detail(data, video_id, site_host(plugin.config)),
            get_display_image_url(data),
        )
    except Exception as exc:
        logger.error(f"iwara_video failed: {exc}")
        yield event.plain_result(f"查询视频失败：{exc}")


async def handle_image(plugin, event):
    """查询图片详情。/iwara_image <图片ID或链接>"""
    image_id = extract_image_id(extract_command_payload(event.message_str, "iwara_image"))
    if not image_id:
        yield event.plain_result("用法：/iwara_image <图片ID或链接>")
        return
    try:
        data = await plugin._api.get_json(f"/image/{image_id}")
        yield await make_chain(
            event, plugin.config, plugin._api,
            format_image_detail(data, image_id, site_host(plugin.config)),
            get_display_image_url(data),
        )
    except Exception as exc:
        logger.error(f"iwara_image failed: {exc}")
        yield event.plain_result(f"查询图片失败：{exc}")


# ── direct ──────────────────────────────────────────────

async def handle_direct(plugin, event):
    """获取视频直链。/iwara_direct <视频ID或链接>"""
    video_id = extract_video_id(extract_command_payload(event.message_str, "iwara_direct"))
    if not video_id:
        yield event.plain_result("用法：/iwara_direct <视频ID或链接>")
        return
    try:
        detail = await plugin._api.get_json(f"/video/{video_id}")
        quality_list = await fetch_quality_list(plugin._api, plugin.config, detail)
    except Exception as exc:
        logger.error(f"iwara_direct failed: {exc}")
        yield event.plain_result(f"获取直链失败：{exc}")
        return
    host = site_host(plugin.config)
    title = str(detail.get("title", "未知标题"))
    final_id = str(detail.get("id", video_id))
    lines: List[str] = [
        f"《{title}》直链",
        f"ID: {final_id}",
        f"页面: https://{host}/video/{final_id}",
    ]
    for item in quality_list:
        name = str(item.get("name", "unknown"))
        src = item.get("src", {}) if isinstance(item.get("src"), dict) else {}
        if view := normalize_url(src.get("view", "")):
            lines.append(f"[{name}] 播放: {view}")
        if dl := normalize_url(src.get("download", "")):
            lines.append(f"[{name}] 下载: {dl}")
    yield await make_chain(event, plugin.config, plugin._api,
                           "\n".join(lines), get_display_image_url(detail))


# ── related / comments / likes ──────────────────────────

async def handle_related(plugin, event):
    """查询相关视频。/iwara_related <视频ID或链接>"""
    video_id = extract_video_id(extract_command_payload(event.message_str, "iwara_related"))
    if not video_id:
        yield event.plain_result("用法：/iwara_related <视频ID或链接>")
        return
    try:
        items = extract_items(await plugin._api.get_json(f"/video/{video_id}/related"))
        if not items:
            yield event.plain_result("未找到相关视频。")
            return
        limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
        host = site_host(plugin.config)
        for idx, item in enumerate(items[:limit], start=1):
            yield await make_chain(event, plugin.config, plugin._api,
                                   format_search_item(idx, item, "video", host),
                                   get_display_image_url(item))
    except Exception as exc:
        logger.error(f"iwara_related failed: {exc}")
        yield event.plain_result(f"查询相关视频失败：{exc}")


async def handle_comments(plugin, event):
    """查询视频评论。/iwara_comments <视频ID或链接>"""
    from .iwara_helpers import extract_author

    video_id = extract_video_id(extract_command_payload(event.message_str, "iwara_comments"))
    if not video_id:
        yield event.plain_result("用法：/iwara_comments <视频ID或链接>")
        return
    try:
        items = extract_items(
            await plugin._api.get_json(f"/video/{video_id}/comments", params={"page": 0}))
        if not items:
            yield event.plain_result("该视频暂无评论。")
            return
        limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
        lines = [f"视频 {video_id} 的评论："]
        for idx, item in enumerate(items[:limit], start=1):
            body = get_text(item, "body") or get_text(item, "content") or get_text(item, "text")
            body = HTML_TAG_RE.sub(" ", body).strip()
            lines.append(f"[{idx}] {extract_author(item)} ({get_text(item, 'createdAt', '-')}): {body[:120]}")
        yield event.plain_result("\n".join(lines))
    except Exception as exc:
        logger.error(f"iwara_comments failed: {exc}")
        yield event.plain_result(f"查询评论失败：{exc}")


async def handle_likes(plugin, event):
    """查询视频点赞用户。/iwara_likes <视频ID或链接>"""
    from .iwara_helpers import extract_author

    video_id = extract_video_id(extract_command_payload(event.message_str, "iwara_likes"))
    if not video_id:
        yield event.plain_result("用法：/iwara_likes <视频ID或链接>")
        return
    try:
        display_limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
        items = extract_items(
            await plugin._api.get_json(f"/video/{video_id}/likes", params={"page": 0, "limit": 20}))
        if not items:
            yield event.plain_result("该视频暂无点赞。")
            return
        lines = [f"视频 {video_id} 的点赞用户："]
        for idx, item in enumerate(items[:display_limit], start=1):
            lines.append(f"[{idx}] {extract_author(item)}")
        yield event.plain_result("\n".join(lines))
    except Exception as exc:
        logger.error(f"iwara_likes failed: {exc}")
        yield event.plain_result(f"查询点赞失败：{exc}")


# ── trending ────────────────────────────────────────────

async def handle_trending(plugin, event):
    """查询热门内容。/iwara_trending [video|image|all]"""
    payload = extract_command_payload(event.message_str, "iwara_trending").strip().lower()
    media_type = payload if payload in {"video", "image", "all"} else "video"
    limit = get_int_config(plugin.config, "search_limit", 5, 1, 10)
    types: List[str] = ["video", "image"] if media_type == "all" else [media_type]
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[str] = []
    for t in types:
        try:
            items = extract_items(
                await plugin._api.get_json(f"/trending/{t}", params={"rating": "all", "limit": limit}))
            for item in items:
                item["_media_type"] = t
            buckets[t] = items
        except Exception as exc:
            errors.append(f"{t}={exc}")
    if not any(buckets.values()) and errors:
        yield event.plain_result(f"获取热门内容失败：{'; '.join(errors)}")
        return
    if not any(buckets.values()):
        yield event.plain_result("暂无热门内容。")
        return
    interleaved: List[Dict[str, Any]] = []
    max_len = max((len(v) for v in buckets.values()), default=0)
    for i in range(max_len):
        for t in types:
            if t in buckets and i < len(buckets[t]):
                interleaved.append(buckets[t][i])
    host = site_host(plugin.config)
    for idx, item in enumerate(interleaved[:limit], start=1):
        t = str(item.get("_media_type", media_type))
        yield await make_chain(event, plugin.config, plugin._api,
                               format_search_item(idx, item, t, host),
                               get_display_image_url(item))
