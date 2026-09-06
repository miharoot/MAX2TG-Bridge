"""Persistent map between Max chat IDs and (Telegram chat, forum topic) pairs."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


def _coerce(key: str) -> Any:
    """Restore the original numeric type of a Max chat ID stored as a JSON key."""
    try:
        return int(key)
    except (ValueError, TypeError):
        return key


class TopicStore:
    """JSON-backed map: Max chat ID ↔ (Telegram chat ID, forum topic/thread ID).

    Each Max chat can be routed to a *different* Telegram supergroup, not just
    a different topic within a single fixed group. ``(tg_chat_id, topic_id)``
    together are the reverse lookup key, since Telegram thread IDs are only
    unique within one chat.
    """

    def __init__(self, path: str):
        self._path = path
        # str(max_chat_id) → {"tg_chat_id": int, "topic_id": int, "title": str}
        self._chats: dict[str, dict] = {}
        # (tg_chat_id, topic_id) → max_chat_id (original type)
        self._by_topic: dict[tuple[int, int], Any] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._chats = data.get("chats", {})
            for key, rec in self._chats.items():
                tid = rec.get("topic_id")
                cid = rec.get("tg_chat_id")
                if tid is not None and cid is not None:
                    self._by_topic[(int(cid), int(tid))] = _coerce(key)
            log.info("Loaded %d topic mappings from %s", len(self._chats), self._path)
        except Exception:
            log.exception("Failed to load topic store %s — starting empty", self._path)
            self._chats = {}
            self._by_topic = {}

    def _save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"chats": self._chats}, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            log.exception("Failed to save topic store %s", self._path)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def get_topic(self, max_chat_id: Any) -> int | None:
        rec = self._chats.get(str(max_chat_id))
        if rec and rec.get("topic_id") is not None:
            return int(rec["topic_id"])
        return None

    def get_chat_id(self, max_chat_id: Any) -> int | None:
        """Telegram chat (supergroup) this Max chat is currently bound to, if any."""
        rec = self._chats.get(str(max_chat_id))
        if rec and rec.get("tg_chat_id") is not None:
            return int(rec["tg_chat_id"])
        return None

    def get_title(self, max_chat_id: Any) -> str | None:
        rec = self._chats.get(str(max_chat_id))
        return rec.get("title") if rec else None

    def set_topic(self, max_chat_id: Any, tg_chat_id: int, topic_id: int, title: str) -> None:
        self._chats[str(max_chat_id)] = {
            "tg_chat_id": int(tg_chat_id),
            "topic_id": int(topic_id),
            "title": title,
        }
        self._by_topic[(int(tg_chat_id), int(topic_id))] = max_chat_id
        self._save()

    def update_title(self, max_chat_id: Any, title: str) -> None:
        rec = self._chats.get(str(max_chat_id))
        if rec:
            rec["title"] = title
            self._save()

    def chat_for_topic(self, tg_chat_id: int, topic_id: int) -> Any | None:
        return self._by_topic.get((int(tg_chat_id), int(topic_id)))

    def all_tg_chat_ids(self) -> set[int]:
        """Every Telegram supergroup that currently has at least one bound topic."""
        return {int(rec["tg_chat_id"]) for rec in self._chats.values()
                if rec.get("tg_chat_id") is not None}

    def remove(self, max_chat_id: Any) -> tuple[int, int] | None:
        """Drop the mapping for a Max chat. Returns (tg_chat_id, topic_id), if any."""
        rec = self._chats.pop(str(max_chat_id), None)
        if not rec:
            return None
        tid = rec.get("topic_id")
        cid = rec.get("tg_chat_id")
        if tid is not None and cid is not None:
            self._by_topic.pop((int(cid), int(tid)), None)
        self._save()
        if tid is not None and cid is not None:
            return int(cid), int(tid)
        return None
