"""Persistent delivery outbox (SQLite via aiosqlite).

Both directions of the bridge write a durable row here *before*
attempting delivery, and only remove it once delivery is actually
confirmed — a Telegram message successfully posted, or MAX's
send_message returning without an error. If delivery fails for any
reason (no connection, the other side down, the process crashing
mid-send), the row stays and a background retry loop
(see app/outbox_retry.py) keeps re-attempting it with backoff until it
succeeds. Nothing is dropped just because a connection hiccuped.

This intentionally gives *at-least-once* delivery, not exactly-once: if
the process crashes in the narrow window after a message was
successfully delivered but before its outbox row was deleted, a retry
could resend it. A rare duplicate is preferable to a silently lost
message, which is the failure mode this exists to close.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# Directions stored in the `direction` column.
MAX_TO_TG = "max_to_tg"
TG_TO_MAX_TEXT = "tg_to_max_text"
TG_TO_MAX_MEDIA = "tg_to_max_media"


class PermanentDeliveryFailure(Exception):
    """The target refused this message itself, not the attempt.

    Retrying can only produce the same answer — MAX rejecting an audio
    recording outright is the case this exists for — so the row is
    dropped rather than re-delivered forever. Everything else (network
    trouble, timeouts, MAX being down, an unrecognised error) stays a
    plain failure and keeps its place in the queue: misjudging that
    direction costs a wasted retry, misjudging this one loses a message.

    Raised only after the reason has been reported into the topic, so a
    dropped message is visible rather than silently gone.
    """


@dataclass
class OutboxItem:
    id: int
    direction: str
    payload: dict
    attempts: int
    created_at: float
    last_attempt_at: float | None
    last_error: str | None


class Outbox:
    """One shared instance for both directions; rows are distinguished by
    ``direction``. The sqlite connection is opened lazily on first use
    (kept async throughout via aiosqlite, consistent with the rest of
    this asyncio codebase) so construction itself stays a cheap,
    synchronous call — safe to do at app wiring time."""

    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        # Item ids currently being delivered (live send or a background
        # retry sweep), so the two never overlap for the same row — see
        # try_start/finish below.
        self._in_flight: set[int] = set()

    def try_start(self, item_id: int) -> bool:
        """Claim an item for delivery. Returns False if it's already being
        delivered elsewhere (a live send still running, or an earlier
        retry sweep tick that hasn't finished), in which case the caller
        must skip it rather than start a second, duplicate attempt.

        This matters most for voice/video attachments: MAX's server can
        take up to a minute to report an upload "ready" (see the ApiError
        matching patch in app/pymax_client.py), and the background retry
        sweep (app/outbox_retry.py) polls every 20 seconds — far sooner
        than that wait can finish. Without this guard, the sweep would
        see the row still pending and kick off a brand new upload+send
        for the same message while the first attempt is still legitimately
        waiting, risking duplicate delivery or wasted concurrent uploads.

        Plain set membership check-then-add is safe here without locking:
        this is asyncio, and neither operation awaits, so nothing can
        interleave between them.
        """
        if item_id in self._in_flight:
            return False
        self._in_flight.add(item_id)
        return True

    def finish(self, item_id: int) -> None:
        """Release a claim made by try_start, whether delivery succeeded
        or failed."""
        self._in_flight.discard(item_id)

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._lock:
            if self._conn is None:
                parent = os.path.dirname(self._path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = await aiosqlite.connect(self._path)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        direction TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        last_attempt_at REAL,
                        last_error TEXT
                    )
                    """
                )
                await conn.commit()
                self._conn = conn
        return self._conn

    async def add(self, direction: str, payload: dict[str, Any]) -> int:
        conn = await self._get_conn()
        cur = await conn.execute(
            "INSERT INTO outbox (direction, payload, created_at) VALUES (?, ?, ?)",
            (direction, json.dumps(payload, default=str, ensure_ascii=False), time.time()),
        )
        await conn.commit()
        log.info("Outbox: queued %s item id=%s", direction, cur.lastrowid)
        return cur.lastrowid

    async def remove(self, item_id: int) -> None:
        conn = await self._get_conn()
        await conn.execute("DELETE FROM outbox WHERE id = ?", (item_id,))
        await conn.commit()

    async def mark_failed(self, item_id: int, error: str) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_attempt_at = ?, "
            "last_error = ? WHERE id = ?",
            (time.time(), (error or "")[:500], item_id),
        )
        await conn.commit()

    async def pending(self) -> list[OutboxItem]:
        conn = await self._get_conn()
        conn.row_factory = aiosqlite.Row
        rows = await conn.execute_fetchall(
            "SELECT id, direction, payload, attempts, created_at, last_attempt_at, "
            "last_error FROM outbox ORDER BY id"
        )
        items = []
        corrupt: list[int] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                # Nothing can ever be delivered from an unreadable payload,
                # so keeping the row only grows the file and re-logs this
                # on every sweep. Drop it instead of skipping it forever.
                log.error("Outbox: corrupt payload for item id=%s, dropping", r["id"])
                corrupt.append(r["id"])
                continue
            items.append(OutboxItem(
                id=r["id"], direction=r["direction"], payload=payload,
                attempts=r["attempts"], created_at=r["created_at"],
                last_attempt_at=r["last_attempt_at"], last_error=r["last_error"],
            ))
        for item_id in corrupt:
            await self.remove(item_id)
        return items

    async def count(self) -> int:
        conn = await self._get_conn()
        async with conn.execute("SELECT COUNT(*) FROM outbox") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
