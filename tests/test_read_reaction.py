"""Tests for MAX read-marker / reaction forwarding in app/max_listener.py."""

from unittest.mock import AsyncMock, MagicMock

from app.pymax_client import MaxMessage, MaxReactionEvent, MaxReadEvent
from app.max_listener import configure_pymax_client
from app.outbox import Outbox


class _FakePyMaxClient:
    """Minimal stand-in for PyMaxClient exposing just the decorator-based
    wiring surface that configure_pymax_client() attaches callbacks to,
    without needing a real PyMax session/connection."""

    def __init__(self, my_id=None):
        self.my_id = my_id
        self._echoes: set = set()
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


def _make_client(sender=None, my_id=None):
    if sender is None:
        sender = AsyncMock()
    client = _FakePyMaxClient(my_id=my_id)
    configure_pymax_client(client, sender)
    client.outbox = Outbox(":memory:")  # don't touch real disk in tests
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

    async def test_ignores_own_read_marker_when_id_types_differ(self):
        """MAX sends ids as ints in some payloads and strings in others.
        A strict comparison fails open here: the ✅ would go up for your
        own read marker, reading in Telegram as if the other side had
        read the message when nobody has."""
        client, sender = _make_client(my_id=427441720)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)

        await client._on_read_cb(
            MaxReadEvent(chat_id=-100, user_id="427441720", mark=123)
        )

        sender.set_reaction.assert_not_called()

    async def test_still_reacts_for_the_other_sides_read(self):
        """The guard must not swallow the case it exists to surface."""
        client, sender = _make_client(my_id=427441720)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=42)

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id="26619816", mark=123))

        sender.set_reaction.assert_awaited_once_with("-100999", 42, "✅")

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

    async def test_reacts_on_album_when_the_album_was_the_last_message(self):
        """Regression test: forwarding a message whose attachments went
        out as one Telegram media-group album must still update the
        read-receipt tracking, same as a plain text message — otherwise
        the ✅ lands on a stale, earlier message instead of the album."""
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()
        # An older plain-text message first, to prove the tracking really
        # moved forward to the album and isn't just stuck on this one.
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=1)

        await _forward_media_group(
            client, sender, chat_id=-100, tg_chat_id="-100999", album_message_id=77,
        )

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id=2, mark=123))

        sender.set_reaction.assert_called_once_with("-100999", 77, "✅")

    async def test_reacts_on_single_attachment_not_grouped_into_an_album(self):
        """A single (non-groupable) attachment goes through the
        per-attachment fallback path, not _try_send_media_group — must
        still update the read-receipt tracking."""
        client, sender = _make_client(my_id=1)
        sender.set_reaction = AsyncMock()
        await _forward_simple_text(client, sender, chat_id=-100, tg_chat_id="-100999", tg_message_id=1)

        sent_file_message = MagicMock()
        sent_file_message.message_id = 88
        sender.send_document = AsyncMock(return_value=sent_file_message)
        client.download_file = AsyncMock(return_value=b"file-bytes")
        client.resolve_file_url = AsyncMock(return_value="https://cdn/file.bin")

        msg = MaxMessage(
            chat_id=-100, sender_id=2, message_id="file-mid",
            attaches=[{"_type": "FILE", "name": "report.pdf", "fileId": 1}],
        )
        await client._on_message_cb(msg)

        await client._on_read_cb(MaxReadEvent(chat_id=-100, user_id=2, mark=123))

        sender.set_reaction.assert_called_once_with("-100999", 88, "✅")


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


async def _forward_media_group(client, sender, chat_id, tg_chat_id, album_message_id):
    """Forward a MAX message with 2+ PHOTO attaches, so it goes out as one
    Telegram media-group album rather than individual sends."""
    sent_album_message = MagicMock()
    sent_album_message.message_id = album_message_id
    sender.topic_store = MagicMock()
    sender.topic_store.get_topic = MagicMock(return_value=5)
    sender.ensure_topic = AsyncMock(return_value=5)
    sender.resolve_chat_id = MagicMock(return_value=tg_chat_id)
    sender.send = AsyncMock()
    sender.send_media_group = AsyncMock(return_value=sent_album_message)
    sender.bot = MagicMock()
    client.download_file = AsyncMock(return_value=b"photo-bytes")

    msg = MaxMessage(
        chat_id=chat_id, sender_id=2, message_id="album-mid",
        attaches=[
            {"_type": "PHOTO", "baseUrl": "https://cdn/1.jpg"},
            {"_type": "PHOTO", "baseUrl": "https://cdn/2.jpg"},
        ],
    )
    await client._on_message_cb(msg)
