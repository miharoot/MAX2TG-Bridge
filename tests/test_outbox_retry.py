"""Tests for app/outbox_retry.py — backoff timing and per-item retry
dispatch across the three outbox directions."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.tg_handler as tg_handler
from app.outbox import MAX_TO_TG, TG_TO_MAX_MEDIA, TG_TO_MAX_TEXT, Outbox, OutboxItem
from app.outbox_retry import MAX_BACKOFF, _backoff_seconds, _retry_one, run_outbox_retry_loop


class TestBackoffSeconds:
    def test_zero_attempts_is_immediate(self):
        assert _backoff_seconds(0) == 0

    def test_first_retry_is_immediate(self):
        # attempts == 1 means it failed once already; still counts as a
        # fresh item as far as backoff goes (immediate first retry is
        # attempts == 0 before any failure has been recorded)
        assert _backoff_seconds(1) == 60

    def test_backoff_doubles_each_attempt(self):
        assert _backoff_seconds(2) == 120
        assert _backoff_seconds(3) == 240
        assert _backoff_seconds(4) == 480

    def test_backoff_caps_at_max(self):
        assert _backoff_seconds(20) == MAX_BACKOFF
        assert _backoff_seconds(1000) == MAX_BACKOFF


def _make_item(direction, payload, item_id=1, attempts=0) -> OutboxItem:
    return OutboxItem(
        id=item_id, direction=direction, payload=payload, attempts=attempts,
        created_at=time.time(), last_attempt_at=None, last_error=None,
    )


def _make_client():
    client = MagicMock()
    client.outbox = MagicMock()
    client.outbox.remove = AsyncMock()
    client.outbox.mark_failed = AsyncMock()
    client.redeliver_max_message = AsyncMock()
    return client


class TestRetryOneMaxToTg:
    async def test_success_removes_item(self):
        client = _make_client()
        item = _make_item(MAX_TO_TG, {
            "chat_id": 1, "sender_id": 2, "text": "hi", "timestamp": "now",
            "message_id": "m1", "is_self": False, "cid": None,
            "attaches": [], "link": {}, "raw": {},
        })
        redeliver_text = AsyncMock()
        redeliver_media = AsyncMock()

        await _retry_one(client, MagicMock(), 1024, item, redeliver_text, redeliver_media)

        client.redeliver_max_message.assert_awaited_once()
        client.outbox.remove.assert_awaited_once_with(1)
        client.outbox.mark_failed.assert_not_awaited()

    async def test_exception_marks_failed_not_removed(self):
        client = _make_client()
        client.redeliver_max_message = AsyncMock(side_effect=RuntimeError("no connection"))
        item = _make_item(MAX_TO_TG, {
            "chat_id": 1, "sender_id": 2, "text": "hi", "timestamp": "now",
            "message_id": "m1", "is_self": False, "cid": None,
            "attaches": [], "link": {}, "raw": {},
        })

        await _retry_one(client, MagicMock(), 1024, item, AsyncMock(), AsyncMock())

        client.outbox.remove.assert_not_awaited()
        client.outbox.mark_failed.assert_awaited_once()
        assert "no connection" in client.outbox.mark_failed.call_args[0][1]


class TestRetryOneTgToMaxText:
    async def test_success_removes_item(self):
        client = _make_client()
        item = _make_item(TG_TO_MAX_TEXT, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "text": "hello", "elements": [],
        })
        redeliver_text = AsyncMock(return_value=True)
        sender = MagicMock()
        sender.bot = MagicMock()

        await _retry_one(client, sender, 1024, item, redeliver_text, AsyncMock())

        redeliver_text.assert_awaited_once_with(client, sender.bot, item.payload)
        client.outbox.remove.assert_awaited_once_with(1)

    async def test_failure_marks_failed(self):
        client = _make_client()
        item = _make_item(TG_TO_MAX_TEXT, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "text": "hello", "elements": [],
        })
        redeliver_text = AsyncMock(return_value=False)

        await _retry_one(client, MagicMock(), 1024, item, redeliver_text, AsyncMock())

        client.outbox.remove.assert_not_awaited()
        client.outbox.mark_failed.assert_awaited_once()


class TestRetryOneTgToMaxMedia:
    async def test_success_removes_item(self):
        client = _make_client()
        item = _make_item(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "caption": "caption", "elements": [],
            "media_specs": [{"kind": "photo", "file_id": "abc"}],
        })
        redeliver_media = AsyncMock(return_value=True)
        sender = MagicMock()
        sender.bot = MagicMock()

        await _retry_one(client, sender, 2048, item, AsyncMock(), redeliver_media)

        redeliver_media.assert_awaited_once_with(client, sender.bot, item.payload, 2048)
        client.outbox.remove.assert_awaited_once_with(1)

    async def test_failure_marks_failed(self):
        client = _make_client()
        item = _make_item(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "caption": "", "elements": [],
            "media_specs": [{"kind": "photo", "file_id": "abc"}],
        })
        redeliver_media = AsyncMock(return_value=False)

        await _retry_one(client, MagicMock(), 1024, item, AsyncMock(), redeliver_media)

        client.outbox.remove.assert_not_awaited()
        client.outbox.mark_failed.assert_awaited_once()


class TestRunOutboxRetryLoopInFlightGuard:
    """Covers the race this fixes: a live send (or an earlier sweep tick)
    can still be waiting on a slow delivery — voice/video attachments
    routinely take pymax's internal "attachment not ready" retry up to a
    minute — when the next 20s sweep tick runs. Without a claim check the
    sweep would start a second, duplicate delivery attempt for the same
    still-in-progress item."""

    async def _run_one_tick(self, client, sender, monkeypatch):
        """Drive run_outbox_retry_loop through exactly one sweep iteration
        by making the second asyncio.sleep call raise CancelledError."""
        monkeypatch.setattr(
            "app.outbox_retry.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        )
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_retry_loop(client, sender, 1024)

    async def test_skips_item_already_claimed_in_flight(self, monkeypatch):
        ob = Outbox(":memory:")
        item_id = await ob.add(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "caption": "", "elements": [],
            "media_specs": [{"kind": "voice", "file_id": "abc"}],
        })
        ob.try_start(item_id)  # simulate the original live send still running

        redeliver_media = AsyncMock()
        monkeypatch.setattr(tg_handler, "redeliver_tg_to_max_media", redeliver_media)

        client = MagicMock()
        client.outbox = ob
        sender = MagicMock()
        sender.bot = MagicMock()

        await self._run_one_tick(client, sender, monkeypatch)

        redeliver_media.assert_not_awaited()
        items = await ob.pending()
        assert len(items) == 1
        assert items[0].attempts == 0
        await ob.close()

    async def test_delivers_and_releases_claim_when_not_in_flight(self, monkeypatch):
        ob = Outbox(":memory:")
        item_id = await ob.add(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 501, "caption": "", "elements": [],
            "media_specs": [{"kind": "voice", "file_id": "abc"}],
        })

        redeliver_media = AsyncMock(return_value=True)
        monkeypatch.setattr(tg_handler, "redeliver_tg_to_max_media", redeliver_media)

        client = MagicMock()
        client.outbox = ob
        sender = MagicMock()
        sender.bot = MagicMock()

        await self._run_one_tick(client, sender, monkeypatch)

        redeliver_media.assert_awaited_once()
        assert await ob.pending() == []
        # claim released after delivery, so a later sweep isn't blocked
        assert ob.try_start(item_id) is True
        await ob.close()


class TestRetryOneUnknownDirection:
    async def test_unknown_direction_drops_the_item(self):
        """An outbox row with a direction this build doesn't recognize
        (e.g. a stale row from a future version) shouldn't jam the retry
        loop forever — drop it rather than retry indefinitely."""
        client = _make_client()
        item = _make_item("some_future_direction", {"whatever": True})

        await _retry_one(client, MagicMock(), 1024, item, AsyncMock(), AsyncMock())

        client.outbox.remove.assert_awaited_once_with(1)
        client.outbox.mark_failed.assert_not_awaited()
