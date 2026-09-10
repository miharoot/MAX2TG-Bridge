"""Tests for disconnect/reconnect notification throttling in
app/max_listener.py.

Rules under test:
- "⚠️ потеряно" is throttled to at most once per hour (flat, no
  escalating backoff).
- "✅ восстановлено" is only ever sent as a *reply* to a disconnect
  notice that was actually delivered to Telegram — never on its own,
  and never after a disconnect that got suppressed by the throttle.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.max_listener import configure_pymax_client
from app.outbox import Outbox


class FakeClient:
    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_read(self, func):
        self._on_read_cb = func
        return func

    def on_reaction(self, func):
        self._on_reaction_cb = func
        return func

    def on_qr(self, func):
        self._on_qr_cb = func
        return func

    def is_bridge_echo(self, msg) -> bool:
        return False

    my_id = None


def _make_client(sender=None):
    if sender is None:
        sender = AsyncMock()
    client = configure_pymax_client(FakeClient(), sender)
    client.outbox = Outbox(":memory:")  # don't touch real disk in tests
    return client, sender


EMPTY_SNAPSHOT = {"profile": {"id": 1, "names": []}, "chats": []}


# ---------------------------------------------------------------------------
# on_disconnect throttle logic — flat 1-hour window
# ---------------------------------------------------------------------------

class TestDisconnectThrottle:
    """Tests for disconnect notification rate-limiting."""

    async def test_first_disconnect_sends_immediately(self):
        client, sender = _make_client()
        await client._on_disconnect_cb()
        sender.broadcast.assert_called_once()
        assert "потеряно" in sender.broadcast.call_args[0][0]

    async def test_second_disconnect_suppressed_within_1_hour(self):
        client, sender = _make_client()

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 10, 30, 0)  # 30 min later

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            sender.broadcast.reset_mock()
            await client._on_disconnect_cb()

        sender.broadcast.assert_not_called()

    async def test_second_disconnect_sends_after_1_hour(self):
        client, sender = _make_client()

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)  # 1 hour + 1 sec later

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            sender.broadcast.reset_mock()
            await client._on_disconnect_cb()

        sender.broadcast.assert_called_once()

    async def test_third_disconnect_still_hourly_not_escalating(self):
        """Regression guard: the old behavior escalated to a 3h, then
        24h window after repeated disconnects. It's flat 1h now, so a
        disconnect 1h+1s after the *second* one should still send."""
        client, sender = _make_client()

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)   # 2nd: +1h, sends
        t2 = datetime(2026, 4, 5, 12, 0, 2)   # 3rd: +1h after 2nd, sends

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()
            mock_dt.now.return_value = t1
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t2
            sender.broadcast.reset_mock()
            await client._on_disconnect_cb()

        sender.broadcast.assert_called_once()


# ---------------------------------------------------------------------------
# on_ready reconnect notification — only follows an actually-sent disconnect
# ---------------------------------------------------------------------------

class TestReconnectNotification:
    """Tests for the 'connection restored' notification."""

    async def test_startup_notification_sent_on_first_connect(self):
        client, sender = _make_client()
        await client._on_ready_cb(EMPTY_SNAPSHOT)
        sender.broadcast.assert_called_once()
        assert "подключён" in sender.broadcast.call_args[0][0]

    async def test_startup_notification_includes_chat_count(self):
        client, sender = _make_client()
        snapshot = {
            "profile": {"id": 1, "names": []},
            "chats": [
                {"id": 100, "type": "GROUP", "title": "Chat A", "participants": {}},
                {"id": 101, "type": "GROUP", "title": "Chat B", "participants": {}},
            ],
        }
        await client._on_ready_cb(snapshot)
        assert "2" in sender.broadcast.call_args[0][0]

    async def test_reconnect_after_real_disconnect_sends_restored(self):
        client, sender = _make_client()
        await client._on_ready_cb(EMPTY_SNAPSHOT)          # first connect
        await client._on_disconnect_cb()                   # disconnect notice sent
        sender.broadcast.reset_mock()

        await client._on_ready_cb(EMPTY_SNAPSHOT)           # reconnect
        sender.broadcast.assert_called_once()
        assert "восстановлено" in sender.broadcast.call_args[0][0]

    async def test_reconnect_without_prior_disconnect_notice_stays_silent(self):
        """The actual bug being fixed: pymax's internal reconnects (or
        any on_ready re-fire) must not produce a lone '✅ восстановлено'
        with no preceding '⚠️ потеряно' the user actually saw."""
        client, sender = _make_client()
        await client._on_ready_cb(EMPTY_SNAPSHOT)          # first connect
        sender.broadcast.reset_mock()

        await client._on_ready_cb(EMPTY_SNAPSHOT)           # on_ready fires again, no disconnect happened
        sender.broadcast.assert_not_called()

    async def test_reconnect_after_suppressed_disconnect_stays_silent(self):
        """If the disconnect notice itself got throttled (suppressed),
        there's nothing for a reconnect notice to confirm — stay quiet."""
        client, sender = _make_client()
        await client._on_ready_cb(EMPTY_SNAPSHOT)          # first connect

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 10, 5, 0)   # 5 min later: suppressed

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()   # sends, sets pending

            # A reconnect right after clears the pending flag...
            sender.broadcast.reset_mock()
            await client._on_ready_cb(EMPTY_SNAPSHOT)
            sender.broadcast.assert_called_once()
            assert "восстановлено" in sender.broadcast.call_args[0][0]

            # ...so a second disconnect+reconnect within the throttle
            # window, where the disconnect itself gets suppressed,
            # must not produce another "восстановлено".
            mock_dt.now.return_value = t1
            sender.broadcast.reset_mock()
            await client._on_disconnect_cb()   # suppressed (< 1h since t0)
            sender.broadcast.assert_not_called()

            await client._on_ready_cb(EMPTY_SNAPSHOT)
            sender.broadcast.assert_not_called()

    async def test_reconnect_notice_only_fires_once_per_disconnect(self):
        client, sender = _make_client()
        await client._on_ready_cb(EMPTY_SNAPSHOT)
        await client._on_disconnect_cb()
        sender.broadcast.reset_mock()

        await client._on_ready_cb(EMPTY_SNAPSHOT)   # consumes the pending notice
        await client._on_ready_cb(EMPTY_SNAPSHOT)   # nothing left to confirm
        assert sender.broadcast.call_count == 1
