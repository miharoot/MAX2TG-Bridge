"""Tests for the MAX→TG outbox wrapping in app/max_listener.py:
handle_message persists to the outbox before attempting delivery, and
only removes the row once _deliver_max_message actually completes
without raising.
"""

from unittest.mock import AsyncMock, MagicMock

from app.max_listener import configure_pymax_client
from app.outbox import MAX_TO_TG, Outbox
from app.pymax_client import MaxMessage


class _FakePyMaxClient:
    """Same minimal stand-in as tests/test_read_reaction.py."""

    def __init__(self, my_id=None):
        self.my_id = my_id
        self._on_ready_cb = None
        self._on_message_cb = None
        self._on_disconnect_cb = None
        self._on_read_cb = None
        self._on_reaction_cb = None

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
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

    def is_bridge_echo(self, msg: MaxMessage) -> bool:
        return False


def _make_client(sender=None):
    if sender is None:
        sender = AsyncMock()
    client = _FakePyMaxClient()
    configure_pymax_client(client, sender)
    client.outbox = Outbox(":memory:")
    return client, sender


def _make_message(**overrides) -> MaxMessage:
    defaults = dict(
        chat_id=42, sender_id=7, text="hello", timestamp="now",
        message_id="m1", is_self=False, cid=None,
        attaches=[], link={}, raw={},
    )
    defaults.update(overrides)
    return MaxMessage(**defaults)


class TestHandleMessageOutbox:
    async def test_successful_forward_removes_outbox_item(self):
        client, sender = _make_client()
        msg = _make_message()

        await client._on_message_cb(msg)

        assert await client.outbox.count() == 0

    async def test_bridge_echo_never_touches_the_outbox(self):
        """An echo of our own bridged message isn't a real inbound
        message — nothing to persist or retry."""
        sender = AsyncMock()
        client = _FakePyMaxClient()
        client.is_bridge_echo = lambda msg: True
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")

        msg = _make_message()
        await client._on_message_cb(msg)

        assert await client.outbox.count() == 0
        sender.send.assert_not_called()

    async def test_delivery_exception_keeps_item_queued(self):
        """If forwarding blows up (e.g. Telegram totally unreachable), the
        message must stay in the outbox for a later retry, not vanish."""
        sender = AsyncMock()
        sender.send = AsyncMock(side_effect=RuntimeError("network down"))
        client, sender = _make_client(sender=sender)
        msg = _make_message()

        await client._on_message_cb(msg)

        items = await client.outbox.pending()
        assert len(items) == 1
        assert items[0].direction == MAX_TO_TG
        assert items[0].attempts == 1
        assert "network down" in items[0].last_error

    async def test_queued_item_payload_round_trips_to_the_same_message(self):
        sender = AsyncMock()
        sender.send = AsyncMock(side_effect=RuntimeError("boom"))
        client, sender = _make_client(sender=sender)
        msg = _make_message(chat_id=99, text="важное сообщение")

        await client._on_message_cb(msg)

        items = await client.outbox.pending()
        rebuilt = MaxMessage(**items[0].payload)
        assert rebuilt.chat_id == 99
        assert rebuilt.text == "важное сообщение"

    async def test_redeliver_max_message_is_exposed_on_the_client(self):
        client, sender = _make_client()
        assert callable(client.redeliver_max_message)

    async def test_redeliver_max_message_can_resend_a_queued_item(self):
        """What the background retry loop actually calls."""
        sender = AsyncMock()
        sender.send = AsyncMock(side_effect=[RuntimeError("boom"), MagicMock(message_id=1)])
        client, sender = _make_client(sender=sender)
        msg = _make_message()

        await client._on_message_cb(msg)  # fails, queued
        items = await client.outbox.pending()
        assert len(items) == 1

        # Retry loop's job: reconstruct + redeliver + remove on success.
        rebuilt = MaxMessage(**items[0].payload)
        await client.redeliver_max_message(rebuilt)
        await client.outbox.remove(items[0].id)

        assert await client.outbox.count() == 0
