"""Tests for app/tg_sender.py — TelegramSender routing (resolve_chat_id, ensure_topic)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tg_sender import TelegramSender
from app.topics import TopicStore

DEFAULT = "-100999"


def _sender(tmp_path, chat_routes=None):
    store = TopicStore(str(tmp_path / "topics.json"))
    with patch("app.tg_sender.Bot"):
        sender = TelegramSender("dummy-token", DEFAULT, store, chat_routes=chat_routes)
    sender._bot = MagicMock()
    return sender, store


# ---------------------------------------------------------------------------
# resolve_chat_id
# ---------------------------------------------------------------------------

class TestResolveChatId:
    def test_falls_back_to_default_when_unrouted(self, tmp_path):
        sender, _ = _sender(tmp_path)
        assert sender.resolve_chat_id(42) == DEFAULT

    def test_uses_static_route_when_configured(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"42": -100111})
        assert sender.resolve_chat_id(42) == "-100111"

    def test_existing_topic_binding_wins_over_static_route(self, tmp_path):
        """Once a topic exists (e.g. via /bind into a different group), that
        binding is the source of truth — a static route must not override it."""
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        store.set_topic(42, -100222, 5, "Alice")
        assert sender.resolve_chat_id(42) == "-100222"

    def test_unrouted_chat_ignores_other_chats_routes(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"42": -100111})
        assert sender.resolve_chat_id(999) == DEFAULT


# ---------------------------------------------------------------------------
# ensure_topic
# ---------------------------------------------------------------------------

class TestEnsureTopic:
    async def test_creates_topic_in_default_group_when_unrouted(self, tmp_path):
        sender, store = _sender(tmp_path)
        topic = MagicMock()
        topic.message_thread_id = 7
        sender._bot.create_forum_topic = AsyncMock(return_value=topic)

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 7
        sender._bot.create_forum_topic.assert_called_once_with(
            chat_id=DEFAULT, name="Alice"
        )
        assert store.get_chat_id(42) == int(DEFAULT)

    async def test_creates_topic_in_routed_group(self, tmp_path):
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        topic = MagicMock()
        topic.message_thread_id = 7
        sender._bot.create_forum_topic = AsyncMock(return_value=topic)

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 7
        sender._bot.create_forum_topic.assert_called_once_with(
            chat_id="-100111", name="Alice"
        )
        assert store.get_chat_id(42) == -100111

    async def test_returns_existing_without_recreating(self, tmp_path):
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        store.set_topic(42, -100111, 9, "Alice")
        sender._bot.create_forum_topic = AsyncMock()

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 9
        sender._bot.create_forum_topic.assert_not_called()

    async def test_returns_none_on_failure(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.create_forum_topic = AsyncMock(side_effect=RuntimeError("no admin rights"))

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id is None


# ---------------------------------------------------------------------------
# send_* methods accept an explicit chat_id
# ---------------------------------------------------------------------------

class TestSendChatIdOverride:
    async def test_send_uses_explicit_chat_id(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.send_message = AsyncMock()

        await sender.send("hi", message_thread_id=3, chat_id="-100777")

        _, kwargs = sender._bot.send_message.call_args
        assert kwargs["chat_id"] == "-100777"

    async def test_send_falls_back_to_default_chat_id(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.send_message = AsyncMock()

        await sender.send("hi")

        _, kwargs = sender._bot.send_message.call_args
        assert kwargs["chat_id"] == DEFAULT
