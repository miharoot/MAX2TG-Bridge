"""An edited message in a topic edits its copy in MAX.

Edits used to vanish: the text handler got the update, found no
update.message in it (an edit arrives as update.edited_message) and
returned without a word.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from pymax.exceptions import ApiError
from telegram import Chat, Message, Update

from app.pymax_client import PyMaxClient
from app.tg_handler import _on_topic_edit, build_tg_app
from tests.test_tg_handler import (
    DEFAULT_TG_CHAT_ID,
    _make_context,
    _make_max_client,
    _make_topic_store,
)

MAX_CHAT = 42


def _edit_update(text="Исправленный текст", *, attachment=None, thread_id=10):
    update = MagicMock()
    update.message = None
    message = update.edited_message
    message.text = text
    message.message_id = 77
    message.chat_id = DEFAULT_TG_CHAT_ID
    message.message_thread_id = thread_id
    message.is_topic_message = True
    message.effective_attachment = attachment
    message.reply_text = AsyncMock()
    update.effective_chat.id = DEFAULT_TG_CHAT_ID
    update.effective_user.id = 100
    return update


def _setup(pair=(MAX_CHAT, 117000000000000001), result=(True, "")):
    max_client = _make_max_client()
    max_client.max_message_pair = AsyncMock(return_value=pair)
    max_client.edit_message = AsyncMock(return_value=result)
    ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: MAX_CHAT}))
    return ctx, max_client


class TestATelegramEditGoesToMax:
    async def test_the_paired_max_message_gets_the_new_text(self):
        ctx, max_client = _setup()
        update = _edit_update()

        await _on_topic_edit(update, ctx)

        max_client.max_message_pair.assert_awaited_once_with(DEFAULT_TG_CHAT_ID, 77)
        max_client.edit_message.assert_awaited_once_with(
            MAX_CHAT, 117000000000000001, "Исправленный текст")
        update.edited_message.reply_text.assert_not_awaited()

    async def test_a_message_with_no_max_copy_says_so(self):
        ctx, max_client = _setup(pair=None)
        update = _edit_update()

        await _on_topic_edit(update, ctx)

        max_client.edit_message.assert_not_awaited()
        assert "Не нашёл" in update.edited_message.reply_text.await_args.args[0]

    async def test_a_pair_from_another_chat_is_not_used(self):
        ctx, max_client = _setup(pair=(MAX_CHAT + 1, 5))

        await _on_topic_edit(_edit_update(), ctx)

        max_client.edit_message.assert_not_awaited()

    async def test_an_edited_caption_is_not_sent(self):
        """MAX's edit carries the attachments too; sent without them, it
        could strip the photo."""
        ctx, max_client = _setup()
        update = _edit_update(attachment=MagicMock())

        await _on_topic_edit(update, ctx)

        max_client.edit_message.assert_not_awaited()
        assert "подписи" in update.edited_message.reply_text.await_args.args[0]

    async def test_a_refusal_from_max_is_reported(self):
        ctx, _ = _setup(result=(False, "Время на редактирование истекло"))
        update = _edit_update()

        await _on_topic_edit(update, ctx)

        assert "истекло" in update.edited_message.reply_text.await_args.args[0]


class TestTheEditReachesItsHandler:
    """The text handler's filter matches an edit too, and the first match
    in a handler group wins — the edit handler has to come first."""

    def test_an_edited_text_message_is_routed_to_the_edit_handler(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), str(DEFAULT_TG_CHAT_ID),
                           MagicMock())
        chat = Chat(id=DEFAULT_TG_CHAT_ID, type="supergroup")
        edited = Message(message_id=77, date=datetime.now(timezone.utc), chat=chat,
                         text="Исправленный текст", message_thread_id=10,
                         is_topic_message=True,
                         edit_date=datetime.now(timezone.utc))
        update = Update(update_id=1, edited_message=edited)

        matching = [h for h in app.handlers[0] if h.check_update(update)]

        assert matching and matching[0].callback is _on_topic_edit


class TestPyMaxClientEdit:
    def _client(self):
        client = PyMaxClient.__new__(PyMaxClient)
        client._client = MagicMock()
        return client

    async def test_an_edit_reaches_pymax(self):
        client = self._client()
        client._client.edit_message = AsyncMock()

        assert await client.edit_message(MAX_CHAT, "117000000000000001", "текст") == (True, "")
        client._client.edit_message.assert_awaited_once_with(
            MAX_CHAT, 117000000000000001, text="текст")

    async def test_a_refusal_comes_back_worded(self):
        client = self._client()
        client._client.edit_message = AsyncMock(side_effect=ApiError(
            opcode=67, error="error.edit", message="nope",
            localized_message="Время на редактирование истекло"))

        assert await client.edit_message(MAX_CHAT, 1, "текст") == (
            False, "Время на редактирование истекло")
