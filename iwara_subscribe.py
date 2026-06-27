"""Iwara subscription manager — store & poll blogger updates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── SubscriptionStore ──────────────────────────────────

class SubscriptionStore:
    """Persistent JSON-backed subscription store.

    Structure on disk::

        {
          "subscriptions": {
            "<username>": {
              "user_id": "<UUID>",
              "known_video_ids": ["..."],
              "known_image_ids": ["..."],
              "subscribers": [
                {"session_str": "aiocqhttp:GroupMessage:123456"}
              ]
            }
          }
        }
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._data: Dict[str, Any] = self._load()

    # ── persistence ───────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                text = self._path.read_text(encoding="utf-8")
                if text.strip():
                    return json.loads(text)
            except (json.JSONDecodeError, OSError):
                pass
        return {"subscriptions": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def _flush(self) -> None:
        self._save()

    # ── public interface ──────────────────────────────────

    def list_all(self) -> Dict[str, Any]:
        """Return all subscriptions keyed by username."""
        return self._data.get("subscriptions", {})

    def add_subscription(
        self,
        username: str,
        user_id: str,
        session_str: str,
    ) -> None:
        """Add a subscriber for *username*. Creates entry if new."""
        subs = self._data.setdefault("subscriptions", {})
        entry = subs.setdefault(username, {
            "user_id": user_id,
            "known_video_ids": [],
            "known_image_ids": [],
            "subscribers": [],
        })
        # deduplicate
        for s in entry["subscribers"]:
            if s.get("session_str") == session_str:
                self._flush()
                return
        entry["subscribers"].append({"session_str": session_str})
        self._flush()

    def remove_subscription(self, username: str, session_str: str) -> bool:
        """Remove a subscriber. Returns True if removed."""
        subs = self._data.get("subscriptions", {})
        entry = subs.get(username)
        if entry is None:
            return False
        before = len(entry["subscribers"])
        entry["subscribers"] = [
            s for s in entry["subscribers"]
            if s.get("session_str") != session_str
        ]
        if len(entry["subscribers"]) == before:
            return False
        # clean up if no subscribers left
        if not entry["subscribers"]:
            del subs[username]
        self._flush()
        return True

    def update_known_ids(
        self,
        username: str,
        video_ids: Optional[List[str]] = None,
        image_ids: Optional[List[str]] = None,
    ) -> None:
        """Replace the known ID lists for *username*."""
        subs = self._data.get("subscriptions", {})
        entry = subs.get(username)
        if entry is None:
            return
        if video_ids is not None:
            entry["known_video_ids"] = list(video_ids)
        if image_ids is not None:
            entry["known_image_ids"] = list(image_ids)
        self._flush()

    def get_known_video_ids(self, username: str) -> List[str]:
        entry = self._data.get("subscriptions", {}).get(username, {})
        return list(entry.get("known_video_ids", []))

    def get_known_image_ids(self, username: str) -> List[str]:
        entry = self._data.get("subscriptions", {}).get(username, {})
        return list(entry.get("known_image_ids", []))

    def list_subscriptions_for_session(self, session_str: str) -> List[str]:
        """Return usernames that *session_str* is subscribed to."""
        result: List[str] = []
        for uname, entry in self._data.get("subscriptions", {}).items():
            for s in entry.get("subscribers", []):
                if s.get("session_str") == session_str:
                    result.append(uname)
                    break
        return result


# ── Polling logic ─────────────────────────────────────────


async def poll_user_content(
    api: Any,
    store: SubscriptionStore,
    username: str,
) -> tuple:
    """Poll one subscribed user for new videos/images.

    Returns ``(new_videos, new_images)`` — lists of item dicts
    that were not previously recorded in the store.
    On first poll (no known IDs), *all* items are returned as new.
    On API error, returns empty lists.
    """
    subs = store.list_all()
    entry = subs.get(username)
    if entry is None:
        return [], []

    user_id = entry.get("user_id", "")
    if not user_id:
        return [], []

    known_vid = set(store.get_known_video_ids(username))
    known_img = set(store.get_known_image_ids(username))
    first_poll = not entry.get("polled", False) and not known_vid and not known_img
    entry["polled"] = True
    
    new_videos: List[Dict[str, Any]] = []
    new_images: List[Dict[str, Any]] = []

    # ── fetch videos ──────────────────────────────────
    try:
        data = await api.get_json(
            "/videos",
            params={
                "sort": "date",
                "rating": "all",
                "user": user_id,
                "limit": 10,
            },
        )
        for item in _extract_results(data):
            vid = str(item.get("id", ""))
            if vid and vid not in known_vid and not first_poll:
                new_videos.append(item)
            known_vid.add(vid)
    except Exception:
        pass

    # ── fetch images ──────────────────────────────────
    try:
        data = await api.get_json(
            "/images",
            params={
                "sort": "date",
                "rating": "all",
                "user": user_id,
                "limit": 10,
            },
        )
        for item in _extract_results(data):
            iid = str(item.get("id", ""))
            if iid and iid not in known_img and not first_poll:
                new_images.append(item)
            known_img.add(iid)
    except Exception:
        pass

    # persist updated known IDs
    store.update_known_ids(
        username,
        video_ids=list(known_vid),
        image_ids=list(known_img),
    )

    return new_videos, new_images


def _extract_results(data: Any) -> List[Dict[str, Any]]:
    """Extract result items from API response, tolerant to various shapes."""
    try:
        from .iwara_helpers import extract_items

        return extract_items(data)
    except ImportError:
        # standalone / test mode
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("results", "items", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []
