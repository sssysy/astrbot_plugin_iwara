from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    extract_items,
    get_int_config,
    get_str_config,
    get_text,
    proxy_url,
    sanitize_image_url,
    build_request_headers,
    compute_x_version,
    format_http_error,
)
from .iwara_format import quality_sort_key
from .iwara_image import (
    apply_image_censor_bytes,
    get_display_image_url,
    image_fetch_candidates,
    is_content_id_thumbnail_url,
)


async def search_items(
    api: IwaraAPI, config: Dict[str, Any], keyword: str, media_type: str, limit: int
) -> List[Dict[str, Any]]:
    keyword = keyword.strip()
    if not keyword:
        return []
    if media_type not in {"video", "image", "all"}:
        media_type = "all"
    if media_type == "all":
        vl, il = max(1, (limit + 1) // 2), max(1, limit - (limit + 1) // 2)
        v_exc: Optional[Exception] = None
        i_exc: Optional[Exception] = None
        videos: List[Dict[str, Any]] = []
        images: List[Dict[str, Any]] = []
        try:
            videos = await search_by_type(api, config, keyword, "video", vl)
        except Exception as exc:
            v_exc = exc
        try:
            images = await search_by_type(api, config, keyword, "image", il)
        except Exception as exc:
            i_exc = exc
        if not videos and not images and (v_exc or i_exc):
            raise RuntimeError(
                "all 搜索失败："
                + "; ".join(
                    x
                    for x in [
                        f"video={v_exc}" if v_exc else "",
                        f"image={i_exc}" if i_exc else "",
                    ]
                    if x
                )
            )
        for item in videos:
            item["_media_type"] = "video"
        for item in images:
            item["_media_type"] = "image"
        return (videos + images)[:limit]
    items = await search_by_type(api, config, keyword, media_type, limit)
    for item in items:
        item["_media_type"] = media_type
    return items


async def search_by_type(
    api: IwaraAPI, config: Dict[str, Any], keyword: str, media_type: str, limit: int
) -> List[Dict[str, Any]]:
    try:
        data = await api.get_json(
            "/search", params={"query": keyword, "type": media_type}
        )
        items = extract_items(data, [f"{media_type}s", "results", "items"])
        if items:
            return items[:limit]
    except Exception as exc:
        logger.warning(f"Iwara /search fallback ({media_type}): {exc}")
    listing = await api.get_json(
        "/videos" if media_type == "video" else "/images",
        params={"sort": "date", "rating": "all", "limit": max(limit * 3, 24)},
    )
    items = extract_items(listing, [f"{media_type}s", "results", "items"])
    lowered = keyword.lower()
    return [
        item
        for item in items
        if lowered in get_text(item, "title").lower()
    ][:limit]


async def fetch_quality_list(
    api: IwaraAPI, config: Dict[str, Any], detail: Dict[str, Any]
) -> List[Dict[str, Any]]:
    file_url = str(detail.get("fileUrl", ""))
    if not file_url:
        raise RuntimeError("视频详情中没有 fileUrl。")
    parsed = urlparse(file_url)
    query = parse_qs(parsed.query)
    file_id = parsed.path.rstrip("/").split("/")[-1]
    expires, hash_value = query.get("expires", [None])[0], query.get("hash", [None])[0]
    if not file_id or not expires or not hash_value:
        raise RuntimeError("fileUrl 缺少参数。")
    data = await api.get_json(
        f"/file/{file_id}",
        params={"expires": expires, "hash": hash_value},
        headers={"X-Version": compute_x_version(file_id, str(expires))},
        use_file_api=True,
    )
    items = extract_items(data)
    items.sort(key=quality_sort_key)
    return items


async def resolve_cover_url(
    api: IwaraAPI, item: Dict[str, Any], media_type: str
) -> Optional[str]:
    image_url = get_display_image_url(item)
    item_id = str(item.get("id", ""))
    if (
        not image_url
        or not item_id
        or not is_content_id_thumbnail_url(image_url, item_id)
    ):
        return image_url
    try:
        detail = await api.get_json(
            f"{'/video' if media_type == 'video' else '/image'}/{item_id}"
        )
        if detail_url := get_display_image_url(detail):
            return detail_url
    except Exception as exc:
        logger.debug(f"cover detail lookup failed: {exc}")
    return image_url


async def make_chain(
    event, config: Dict[str, Any], api: IwaraAPI, text: str, image_url: Optional[str]
):
    if not image_url or not (image_url := sanitize_image_url(image_url)):
        return event.plain_result(text)
    transport = get_str_config(config, "image_transport", "bytes").lower()
    try:
        if transport == "url":
            image_comp = Comp.Image.fromURL(image_url)
        else:
            image_comp = await download_image(config, api, image_url)
    except Exception as exc:
        logger.warning(f"skip image: {image_url} ({exc})")
        return event.plain_result(f"{text}\n封面: {image_url}")
    return event.chain_result([image_comp, Comp.Plain(text)])


async def download_image(config: Dict[str, Any], api: IwaraAPI, image_url: str):
    session = await api._get_session()
    px = proxy_url(config)
    timeout_sec = get_int_config(config, "image_fetch_timeout_sec", 8, 3, 30)
    hdrs = build_request_headers(config)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    max_bytes = get_int_config(config, "image_max_kb", 2048, 256, 10240) * 1024
    last_exc: Optional[Exception] = None
    for attempt_url in image_fetch_candidates(image_url):
        try:
            async with session.get(
                attempt_url, headers=hdrs, proxy=px, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(
                        format_http_error(
                            resp.status, await resp.text(), bool(px), config
                        )
                    )
                data = await resp.read()
                if not data:
                    raise RuntimeError("图片响应为空")
                if len(data) > max_bytes:
                    raise RuntimeError(f"图片过大({len(data)}>{max_bytes})")
                data = await apply_image_censor_bytes(data, config)
                if len(data) > max_bytes:
                    raise RuntimeError("打码后图片过大")
                return Comp.Image.fromBytes(data)
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(str(last_exc or "图片下载失败"))


def cloudscraper_available() -> bool:
    try:
        import cloudscraper as _  # noqa: F401

        return True
    except Exception:
        return False
