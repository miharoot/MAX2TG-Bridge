"""Tests for MAX read-marker / reaction forwarding in app/max_listener.py."""

from unittest.mock import AsyncMock, MagicMock

from app.max_client import MaxMessage, MaxReactionEvent, MaxReadEvent
from app.max_listener import create_max_client


def _make_client(sender=None, my_id=None):
    if sender is None:
        sender = AsyncMock()
    client = create_max_client(max_token="tok", max_device_id="dev", sender=sender)
    if my_id is not None:
        client._my_id = my_id
    return client, sender


class TestReadEventForwarding:
    """✅ reaction mirrored onto the last Telegram message we forwarded for
    that Max chat, when the *other* side's read marker moves."""

    async def test_no_reaction_when_nothing_was_ever_forwarded(self):
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id=2, mark=123))

        sender.set_reaction.assert_not_called()

    async def test_reacts_on_last_forwarded_message_for_that_chat(self):
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()

        # Simulate a prior forward having populated the tracking dict —
        # reach into the closure the same way handle_message would have.
        # (handle_message itself is exercised end-to-end in TestFullFlow.)
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id=2, mark=123))

        sender.set_reaction.assert_called_once_with("-100999", 42, "✅")

    async def test_ignores_own_read_marker_moving(self):
        """When YOU read the chat (e.g. on your phone), user_id == my_id —
        that's not interesting to mirror into Telegram."""
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id=1, mark=123))

        sender.set_reaction.assert_not_called()

    async def test_ignores_set_as_unread(self):
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)

        await client._on_read_cb(
            MaxReadEvent(chat_id=-100, user_id=2, mark=123, set_as_unread=True)
        )

        sender.set_reaction.assert_not_called()

    async def test_only_reacts_on_correct_chats_message(self):
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)
        await _forward_simple_text(client, sender, chat_id=-200, tg_chat_id="-100999", tg_message_id=99)

        await client._on_read_cb(MaxReadEvent(chat_id=-200, user_id=2, mark=1))

        sender.set_reaction.assert_called_once_with("-100999", 99, "✅")


class TestReactionEventForwarding:
    """A MAX message-reaction change becomes a short status line in the
    corresponding topic (no per-message Telegram reaction — we don't track
    individual MAX message_id → Telegram message_id pairs)."""

    async def test_no_op_when_topic_does_not_exist_yet(self):
        client, sender = _make_client()
        sender.topic_store.get_topic = MagicMock(return_value=None)
        sender.send = AsyncMock()

        await client._on_reaction_cb(
            MaxReactionEvent(chat_id=-100, message_id="m1",
                            counters=[{"reaction": "👍", "count": 1}])
        )

        sender.send.assert_not_called()

    async def test_sends_status_line_with_reaction_counts(self):
        client, sender = _make_client()
        sender.topic_store.get_topic = MagicMock(return_value=7)
        sender.resolve_chat_id = MagicMock(return_value="-100999")
        sender.send = AsyncMock()

        await client._on_reaction_cb(
            MaxReactionEvent(chat_id=-100, message_id="m1",
                            counters=[{"reaction": "👍", "count": 2}])
        )

        sender.send.assert_called_once()
        args, kwargs = sender.send.call_args
        assert "👍×2" in args[0]
        assert kwargs["message_thread_id"] == 7
        assert kwargs["chat_id"] == "-100999"

    async def test_no_op_when_no_counters(self):
        client, sender = _make_client()
        sender.topic_store.get_topic = MagicMock(return_value=7)
        sender.send = AsyncMock()

        await client._on_reaction_cb(
            MaxReactionEvent(chat_id=-100, message_id="m1", counters=[])
        )

        sender.send.assert_not_called()


# ---------------------------------------------------------------------------
# helper: forward one plain text message through handle_message, the same
# way the real dispatcher would, so _last_tg_message gets populated.
# ---------------------------------------------------------------------------

async def _forward_simple_text(client, sender, chat_id, tg_chat_id, tg_message_id):
    sent = MagicMock()
    sent.message_id = tg_message_id
    sender.topic_store = MagicMock()
    sender.topic_store.get_topic = MagicMock(return_value=5)
    sender.ensure_topic = AsyncMock(return_value=5)
    sender.resolve_chat_id = MagicMock(return_value=tg_chat_id)
    sender.send = AsyncMock(return_value=sent)
    sender.bot = MagicMock()

    msg = MaxMessage(chat_id=chat_id, sender_id=2, text="hi", message_id="mid1")
    await client._on_message_cb(msg)
