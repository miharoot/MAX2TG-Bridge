"""Edits and deletions made in MAX, shown in the topic.

Until now neither reached Telegram: the bridge listened to new messages
only, so an edited message kept its old text in the topic and a deleted
one stayed as if nothing happened.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from app.outbox import Outbox
from app.pymax_client import MaxMessage
from tests.test_pymax_client import FakeModel, adapter  # noqa: F401  (fixture)

TG_CHAT = -100999
MAX_CHAT = -10000000000001
MAX_MSG = "117000000000000001"


class TestPyMaxClientPassesThemOn:
    async def test_an_edit_reaches_on_edit(self, adapter):  # noqa: F811
        client, raw = adapter
        got = []
        client.on_edit(AsyncMock(side_effect=lambda m: got.append(m)))

        await raw.message_edit_handlers[0](
            FakeModel(id=5, chatId=20, chat_id=20, sender=8, text="новый текст",
                      attaches=[], link=None, cid=None, time=100), raw)

        assert got and got[0].text == "новый текст" and got[0].chat_id == 20

    async def test_a_delete_notice_reaches_on_delete(self, adapter):  # noqa: F811
        client, raw = adapter
        cb = AsyncMock()
        client.on_delete(cb)

        await raw.message_delete_handlers[0](
            SimpleNamespace(chat_id=20, message_ids=[5, 6]), raw)

        cb.assert_awaited_once_with(20, ["5", "6"])

    async def test_a_removed_message_reaches_on_delete_too(self, adapter):  # noqa: F811
        client, raw = adapter
        cb = AsyncMock()
        client.on_delete(cb)

        await raw.message_delete_handlers[0](SimpleNamespace(chat_id=20, id=7), raw)

        cb.assert_awaited_once_with(20, ["7"])

    async def test_an_ignored_chat_stays_ignored(self, adapter):  # noqa: F811
        client, raw = adapter
        client.ignore_chat_ids = [20]
        cb = AsyncMock()
        client.on_delete(cb)

        await raw.message_delete_handlers[0](SimpleNamespace(chat_id=20, message_ids=[5]), raw)

        cb.assert_not_awaited()


def _edited(**kw):
    defaults = dict(chat_id=MAX_CHAT, sender_id=100000001, text="Исправленный текст",
                    timestamp=200, message_id=MAX_MSG, is_self=False, cid=None,
                    attaches=[], link=None, raw={})
    defaults.update(kw)
    return MaxMessage(**defaults)


async def _setup(paired=True, topic=5):
    from app.max_listener import configure_pymax_client
    from tests.test_max_to_tg_outbox import _FakePyMaxClient

    sender = AsyncMock()
    sender.topic_store = MagicMock()
    sender.topic_store.get_topic = MagicMock(return_value=topic)
    sender.resolve_chat_id = MagicMock(return_value=str(TG_CHAT))
    sender.edit_text_or_caption = AsyncMock(return_value=True)
    sender.send = AsyncMock(return_value=MagicMock(message_id=99))
    client = _FakePyMaxClient()
    configure_pymax_client(client, sender)
    client.outbox = Outbox(":memory:")
    client.resolver.users[100000001] = "Иван Петров"
    client.resolver.chats[MAX_CHAT] = "Рабочий чат"
    client.resolver.chat_types[MAX_CHAT] = "CHAT"
    if paired:
        await client.outbox.map_message(TG_CHAT, 55, MAX_CHAT, MAX_MSG)
    return client, sender


class TestAnEditInMax:
    async def test_the_telegram_copy_is_rewritten_and_marked(self):
        client, sender = await _setup()

        await client._on_edit_cb(_edited())

        chat, tg_id, text = sender.edit_text_or_caption.await_args.args
        assert (chat, tg_id) == (str(TG_CHAT), 55)
        assert "Исправленный текст" in text and "✏️ изменено в MAX" in text
        assert "Иван Петров" in text
        sender.send.assert_not_awaited()
        await client.outbox.close()

    async def test_one_the_bridge_sent_is_left_alone(self):
        """Its Telegram side is the user's own message: a bot may not edit
        it, and the edit came from there anyway."""
        client, sender = await _setup()
        await client.outbox.mark_sent_by_bridge(MAX_CHAT, MAX_MSG)

        await client._on_edit_cb(_edited())

        sender.edit_text_or_caption.assert_not_awaited()
        await client.outbox.close()

    async def test_without_a_copy_nothing_happens(self):
        client, sender = await _setup(paired=False)

        await client._on_edit_cb(_edited())

        sender.edit_text_or_caption.assert_not_awaited()
        sender.send.assert_not_awaited()
        await client.outbox.close()

    async def test_if_telegram_refuses_the_new_text_goes_as_a_reply(self):
        client, sender = await _setup()
        sender.edit_text_or_caption = AsyncMock(return_value=False)

        await client._on_edit_cb(_edited())

        text = sender.send.await_args.args[0]
        assert "изменено в MAX" in text and "Исправленный текст" in text
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 55
        await client.outbox.close()


class TestADeletionInMax:
    async def test_the_copy_is_marked_not_removed(self):
        client, sender = await _setup()

        await client._on_delete_cb(MAX_CHAT, [MAX_MSG])

        assert "🗑 удалено в MAX" in sender.send.await_args.args[0]
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 55
        await client.outbox.close()

    async def test_the_bridge_s_own_deletion_is_not_announced(self):
        """/rm already removed it from the topic."""
        client, sender = await _setup()
        client.is_own_deletion = MagicMock(return_value=True)

        await client._on_delete_cb(MAX_CHAT, [MAX_MSG])

        sender.send.assert_not_awaited()
        await client.outbox.close()

    async def test_without_a_copy_nothing_is_posted(self):
        client, sender = await _setup(paired=False)

        await client._on_delete_cb(MAX_CHAT, [MAX_MSG])

        sender.send.assert_not_awaited()
        await client.outbox.close()


class TestEditingTheBotsOwnMessage:
    def _sender(self, tmp_path):
        from app.tg_sender import TelegramSender
        from app.topics import TopicStore

        with patch("app.tg_sender.Bot"):
            sender = TelegramSender("t", str(TG_CHAT), TopicStore(str(tmp_path / "t.json")))
        sender._bot = MagicMock()
        return sender

    async def test_a_text_message_is_edited_as_text(self, tmp_path):
        sender = self._sender(tmp_path)
        sender._bot.edit_message_text = AsyncMock()
        sender._bot.edit_message_caption = AsyncMock()

        assert await sender.edit_text_or_caption(TG_CHAT, 55, "<b>x</b>") is True
        sender._bot.edit_message_caption.assert_not_awaited()

    async def test_media_falls_back_to_its_caption(self, tmp_path):
        sender = self._sender(tmp_path)
        sender._bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Bad Request: there is no text in the message to edit"))
        sender._bot.edit_message_caption = AsyncMock()

        assert await sender.edit_text_or_caption(TG_CHAT, 55, "<b>x</b>") is True
        sender._bot.edit_message_caption.assert_awaited_once()

    async def test_unchanged_text_counts_as_done(self, tmp_path):
        sender = self._sender(tmp_path)
        sender._bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Bad Request: message is not modified"))

        assert await sender.edit_text_or_caption(TG_CHAT, 55, "x") is True

    @pytest.mark.parametrize("error", ["Bad Request: message to edit not found",
                                       "Bad Request: message can't be edited"])
    async def test_a_refusal_is_false(self, tmp_path, error):
        sender = self._sender(tmp_path)
        sender._bot.edit_message_text = AsyncMock(side_effect=BadRequest(error))

        assert await sender.edit_text_or_caption(TG_CHAT, 55, "x") is False


async def test_rm_marks_its_own_deletion():
    """So the deletion MAX reports back is not announced in the topic."""
    from app.pymax_client import PyMaxClient

    client = PyMaxClient.__new__(PyMaxClient)
    client._client = MagicMock()
    client._client.delete_message = AsyncMock(return_value=True)
    client._deleting = set()
    client.outbox = None

    await client.delete_message(MAX_CHAT, MAX_MSG)

    assert client.is_own_deletion(MAX_CHAT, MAX_MSG)
