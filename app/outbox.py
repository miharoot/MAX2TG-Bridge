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

# How long a bridge-sent MAX message id is worth keeping: long enough to
# outlive any catch-up window, short enough that the table stays small.
SENT_BY_BRIDGE_TTL_SEC = 7 * 24 * 60 * 60


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
                # How far each MAX chat has been forwarded. Lives here
                # rather than in topics.json because it's written on every
                # delivered message: one row updated in place beats
                # rewriting a JSON file, and concurrent deliveries can't
                # clobber each other.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seen (
                        chat_id   TEXT PRIMARY KEY,
                        last_time INTEGER NOT NULL,
                        last_id   TEXT
                    )
                    """
                )
                # MAX message ids the bridge itself produced (a
                # Telegram message relayed into MAX). MAX hands them back
                # in chat history, where nothing else distinguishes them
                # from a message typed by hand in a MAX client — without
                # this, a catch-up after a reconnect forwards the
                # bridge's own messages back into Telegram.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sent_by_bridge (
                        chat_id    TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (chat_id, message_id)
                    )
                    """
                )
                # The Telegram message a MAX read marker should land its
                # ✅ on: the last thing that appeared in that chat's topic,
                # whichever side put it there. In the database because the
                # peer usually reads minutes or hours later, often past a
                # restart — kept only in memory, the ✅ had nothing to
                # attach to and was silently dropped.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS last_tg_message (
                        chat_id       TEXT PRIMARY KEY,
                        tg_chat_id    TEXT NOT NULL,
                        tg_message_id INTEGER NOT NULL,
                        updated_at    REAL NOT NULL
                    )
                    """
                )
                await conn.commit()
                self._conn = conn
        return self._conn

    async def set_last_tg_message(self, chat_id: Any, tg_chat_id: Any,
                                  tg_message_id: Any) -> None:
        """Remember where a ✅ for this MAX chat should go."""
        if tg_message_id is None or tg_chat_id is None:
            return
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO last_tg_message (chat_id, tg_chat_id, tg_message_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "    tg_chat_id = excluded.tg_chat_id, "
            "    tg_message_id = excluded.tg_message_id, "
            "    updated_at = excluded.updated_at",
            (str(chat_id), str(tg_chat_id), int(tg_message_id), time.time()),
        )
        await conn.commit()

    async def last_tg_messages(self) -> dict[str, tuple[str, int]]:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT chat_id, tg_chat_id, tg_message_id FROM last_tg_message"
        ) as cur:
            rows = await cur.fetchall()
        return {str(chat_id): (str(tg_chat_id), int(tg_message_id))
                for chat_id, tg_chat_id, tg_message_id in rows}

    async def seen_message_ids(self) -> dict[str, str]:
        """The last forwarded MAX message id per chat.

        What a read marker sent back to MAX is addressed to, so it has to
        survive a restart: held only in memory, a reply typed before the
        chat said anything new had nothing to mark the chat read up to.
        """
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT chat_id, last_id FROM seen WHERE last_id IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        return {str(chat_id): str(last_id) for chat_id, last_id in rows}

    async def mark_sent_by_bridge(self, chat_id: Any, message_id: Any) -> None:
        """Remember that this MAX message came from the bridge.

        Kept in the database rather than in memory because the catch-up
        that needs it runs right after a restart, when any in-process
        record of what was sent is already gone.
        """
        if message_id is None:
            return
        conn = await self._get_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO sent_by_bridge (chat_id, message_id, created_at) "
            "VALUES (?, ?, ?)",
            (str(chat_id), str(message_id), time.time()),
        )
        # Only ever consulted for messages recent enough to still show up
        # in a catch-up, so old rows are dead weight.
        await conn.execute(
            "DELETE FROM sent_by_bridge WHERE created_at < ?",
            (time.time() - SENT_BY_BRIDGE_TTL_SEC,),
        )
        await conn.commit()

    async def was_sent_by_bridge(self, chat_id: Any, message_id: Any) -> bool:
        if message_id is None:
            return False
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT 1 FROM sent_by_bridge WHERE chat_id = ? AND message_id = ?",
            (str(chat_id), str(message_id)),
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(self, chat_id: Any, message_time: Any,
                        message_id: Any = None) -> None:
        """Record that this chat has been forwarded up to this message.

        Never moves backwards: a retry or an out-of-order delivery of an
        older message must not make the bridge re-send everything after it.
        """
        try:
            stamp = int(message_time)
        except (TypeError, ValueError):
            return
        conn = await self._get_conn()
        await conn.execute(
            """
            INSERT INTO seen (chat_id, last_time, last_id) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_time = excluded.last_time,
                last_id   = excluded.last_id
            WHERE excluded.last_time > seen.last_time
            """,
            (str(chat_id), stamp, None if message_id is None else str(message_id)),
        )
        await conn.commit()

    async def seen_mark(self, chat_id: Any) -> int | None:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT last_time FROM seen WHERE chat_id = ?", (str(chat_id),)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else int(row[0])

    async def seen_marks(self) -> dict[str, int]:
        conn = await self._get_conn()
        async with conn.execute("SELECT chat_id, last_time FROM seen") as cur:
            rows = await cur.fetchall()
        return {str(chat_id): int(last_time) for chat_id, last_time in rows}

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
