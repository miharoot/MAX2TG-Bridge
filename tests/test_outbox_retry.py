"""Tests for app/outbox_retry.py — backoff timing and per-item retry
dispatch across the three outbox directions."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.tg_handler as tg_handler
from app.outbox import MAX_TO_TG, TG_TO_MAX_MEDIA, TG_TO_MAX_TEXT, Outbox, OutboxItem
from app.outbox_retry import (
    MAX_BACKOFF,
    MIN_BACKOFF,
    _backoff_seconds,
    _retry_one,
    run_outbox_retry_loop,
)


class TestBackoffSeconds:
    def test_zero_attempts_is_immediate(self):
        assert _backoff_seconds(0) == 0

    def test_the_first_wait_after_a_failure_is_two_minutes(self):
        # attempts == 1 means it failed once already; attempts == 0 is a
        # fresh item, retried immediately
        assert _backoff_seconds(1) == 120

    def test_backoff_doubles_each_attempt(self):
        assert _backoff_seconds(2) == 240
        assert _backoff_seconds(3) == 480

    def test_backoff_caps_at_max(self):
        assert _backoff_seconds(20) == MAX_BACKOFF
        assert _backoff_seconds(1000) == MAX_BACKOFF

    def test_the_throttle_runs_from_two_to_ten_minutes(self):
        # Long enough that a refusing target isn't hammered with repeated
        # downloads and uploads, short enough that a message doesn't sit
        # around long after delivery became possible again. The item is
        # not dropped for failing, only throttled — the one exception being
        # OUTBOX_MAX_AGE (see TestExpiry).
        assert MIN_BACKOFF == 120
        assert MAX_BACKOFF == 600


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


class TestAPermanentRefusalLeavesTheQueue:
    """A message MAX refused for good — the recipient's profile is
    restricted — sat in the outbox and was resent every ten minutes, each
    time posting the same refusal into the topic. On the next sweep it must
    be reported once more and dropped."""

    async def test_a_restricted_recipient_is_dropped_on_retry(self):
        client = _make_client()
        client.send_message = AsyncMock(return_value={"_max_error": {
            "message": "User is restricted [error.user.restricted.send]",
            "localizedMessage": "Начать диалог не получится. Возможности профиля ограничены",
            "permanent": True,
        }})
        bot = MagicMock()
        bot.send_message = AsyncMock()
        sender = MagicMock()
        sender.bot = bot
        item = _make_item(TG_TO_MAX_TEXT, {
            "max_chat_id": 100000001, "tg_chat_id": -10000000000001,
            "thread_id": 5, "tg_message_id": 7, "text": "привет",
        }, item_id=308, attempts=59)

        await _retry_one(client, sender, None, item,
                         tg_handler.redeliver_tg_to_max_text,
                         tg_handler.redeliver_tg_to_max_media)

        client.outbox.remove.assert_awaited_once_with(308)
        client.outbox.mark_failed.assert_not_awaited()
        posted = bot.send_message.await_args.kwargs["text"]
        assert "повторять не буду" in posted



class TestRefusalSchedule:
    """MAX refusing the same message again and again: the ordinary backoff
    (2, 4, 8 min, then every 10 min) for the first hour, then every 20 min,
    from 3 h every hour, from 6 h every 6 hours, from a day once a day."""

    H = 3600

    def test_the_first_hour_keeps_the_ordinary_backoff(self):
        from app.outbox_retry import _refusal_wait

        assert _refusal_wait(0) is None
        assert _refusal_wait(self.H - 1) is None

    def test_after_an_hour_every_twenty_minutes(self):
        from app.outbox_retry import _refusal_wait

        assert _refusal_wait(self.H) == 20 * 60
        assert _refusal_wait(3 * self.H - 1) == 20 * 60

    def test_after_three_hours_every_hour(self):
        from app.outbox_retry import _refusal_wait

        assert _refusal_wait(3 * self.H) == self.H
        assert _refusal_wait(6 * self.H - 1) == self.H

    def test_after_six_hours_every_six_hours(self):
        from app.outbox_retry import _refusal_wait

        assert _refusal_wait(6 * self.H) == 6 * self.H
        assert _refusal_wait(24 * self.H - 1) == 6 * self.H

    def test_after_a_day_once_a_day(self):
        from app.outbox_retry import _refusal_wait

        assert _refusal_wait(24 * self.H) == 24 * self.H
        assert _refusal_wait(6 * 24 * self.H) == 24 * self.H

    def test_the_ordinary_backoff_itself_is_unchanged(self):
        assert _backoff_seconds(1) == 120
        assert _backoff_seconds(2) == 240
        assert _backoff_seconds(3) == 480
        assert _backoff_seconds(4) == MAX_BACKOFF == 600

    def test_a_refused_item_waits_on_the_refusal_schedule(self):
        from app.outbox_retry import _wait_before_retry

        now = 100 * self.H
        item = _make_item(TG_TO_MAX_TEXT, {}, attempts=40)
        item.refused_since = now - 10 * self.H

        assert _wait_before_retry(item, now) == 6 * self.H

    def test_an_item_that_is_not_refused_keeps_the_ordinary_backoff(self):
        from app.outbox_retry import _wait_before_retry

        item = _make_item(TG_TO_MAX_TEXT, {}, attempts=40)

        assert _wait_before_retry(item, time.time()) == MAX_BACKOFF

    async def test_a_refusal_on_retry_is_recorded_not_dropped(self):
        client = _make_client()
        client.outbox.mark_refused = AsyncMock(return_value=0.0)
        client.send_message = AsyncMock(return_value={"_max_error": {
            "message": "Что-то не так [error.some.code]",
            "localizedMessage": "Что-то не так",
            "code": "error.some.code",
        }})
        bot = MagicMock()
        bot.send_message = AsyncMock()
        sender = MagicMock()
        sender.bot = bot
        item = _make_item(TG_TO_MAX_TEXT, {
            "max_chat_id": 100000001, "tg_chat_id": -10000000000001,
            "thread_id": 5, "tg_message_id": 7, "text": "привет",
        }, item_id=9)

        await _retry_one(client, sender, None, item,
                         tg_handler.redeliver_tg_to_max_text,
                         tg_handler.redeliver_tg_to_max_media)

        client.outbox.mark_refused.assert_awaited_once()
        assert client.outbox.mark_refused.await_args.args[:2] == (9, "error.some.code")
        client.outbox.remove.assert_not_awaited()
        client.outbox.mark_failed.assert_not_awaited()


class TestExpiry:
    """Whatever is still undelivered a week after it was queued is dropped
    — refusal or outage alike."""

    def _sender(self):
        sender = MagicMock()
        sender.bot = MagicMock()
        sender.bot.send_message = AsyncMock()
        return sender

    async def test_a_week_old_message_from_telegram_is_dropped_and_reported(self):
        from app.outbox_retry import OUTBOX_MAX_AGE, _expire_if_too_old

        client, sender = _make_client(), self._sender()
        item = _make_item(TG_TO_MAX_TEXT, {
            "max_chat_id": 100000001, "tg_chat_id": -10000000000001,
            "thread_id": 5, "tg_message_id": 7, "text": "привет",
        }, item_id=12)
        item.created_at = 1000.0

        dropped = await _expire_if_too_old(client, sender, item,
                                           now=1000.0 + OUTBOX_MAX_AGE)

        assert dropped is True
        client.outbox.remove.assert_awaited_once_with(12)
        kwargs = sender.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == -10000000000001
        assert kwargs["message_thread_id"] == 5
        assert "«привет»" in kwargs["text"] and "7 дней" in kwargs["text"]

    async def test_a_younger_message_is_kept(self):
        from app.outbox_retry import OUTBOX_MAX_AGE, _expire_if_too_old

        client, sender = _make_client(), self._sender()
        item = _make_item(TG_TO_MAX_TEXT, {"tg_chat_id": -10000000000001})
        item.created_at = 1000.0

        dropped = await _expire_if_too_old(client, sender, item,
                                           now=1000.0 + OUTBOX_MAX_AGE - 1)

        assert dropped is False
        client.outbox.remove.assert_not_awaited()

    async def test_a_message_from_max_is_dropped_without_posting(self):
        """Telegram being unreachable may be why it is still queued, so the
        log is the only place for the notice."""
        from app.outbox_retry import OUTBOX_MAX_AGE, _expire_if_too_old

        client, sender = _make_client(), self._sender()
        item = _make_item(MAX_TO_TG, {"chat_id": -10000000000001}, item_id=3)
        item.created_at = 0.0

        assert await _expire_if_too_old(client, sender, item, now=OUTBOX_MAX_AGE) is True
        client.outbox.remove.assert_awaited_once_with(3)
        sender.bot.send_message.assert_not_awaited()
