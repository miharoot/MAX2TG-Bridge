"""/rm: delete a message in MAX from its topic.

Telegram tells a bot nothing when a message is deleted, so deleting in the
topic alone never reached MAX — a command is the only way. What the bridge
sent goes for everyone; anything else only for this account.
"""

from unittest.mock import AsyncMock, MagicMock

from pymax.exceptions import ApiError

from app.outbox import Outbox
from app.pymax_client import PyMaxClient
from app.tg_handler import _cmd_rm
from tests.test_tg_handler import (
    DEFAULT_TG_CHAT_ID,
    _make_context,
    _make_max_client,
    _make_topic_store,
)

MAX_CHAT = 42
MAX_MSG = 117000000000000001


def _rm_update(reply_id=55):
    update = MagicMock()
    message = update.message
    message.text = "/rm"
    message.message_id = 90
    message.chat_id = DEFAULT_TG_CHAT_ID
    message.message_thread_id = 10
    message.is_topic_message = True
    message.reply_text = AsyncMock()
    if reply_id is None:
        message.reply_to_message = None
    else:
        message.reply_to_message = MagicMock(message_id=reply_id, forum_topic_created=None)
    update.effective_chat.id = DEFAULT_TG_CHAT_ID
    update.effective_user.id = 100
    return update


def _setup(pair=(MAX_CHAT, MAX_MSG), result=(True, True, ""), copies=(55, 56)):
    max_client = _make_max_client()
    max_client.max_message_pair = AsyncMock(return_value=pair)
    max_client.delete_message = AsyncMock(return_value=result)
    max_client.tg_messages_for = AsyncMock(return_value=list(copies))
    ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: MAX_CHAT}))
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    return ctx, max_client


def _deleted(ctx):
    return sorted(c.kwargs["message_id"] for c in ctx.bot.delete_message.await_args_list)


class TestRm:
    async def test_the_max_message_and_every_telegram_copy_go(self):
        ctx, max_client = _setup()

        await _cmd_rm(_rm_update(), ctx)

        max_client.delete_message.assert_awaited_once_with(MAX_CHAT, MAX_MSG)
        assert _deleted(ctx) == [55, 56, 90]   # both copies and the command
        ctx.bot.send_message.assert_not_awaited()

    async def test_deleted_only_for_us_is_said(self):
        ctx, _ = _setup(result=(True, False, ""))

        await _cmd_rm(_rm_update(), ctx)

        assert "только у тебя" in ctx.bot.send_message.await_args.kwargs["text"]

    async def test_a_refusal_leaves_telegram_alone(self):
        ctx, _ = _setup(result=(False, True, "Нельзя удалить"))
        update = _rm_update()

        await _cmd_rm(update, ctx)

        ctx.bot.delete_message.assert_not_awaited()
        assert "Нельзя удалить" in update.message.reply_text.await_args.args[0]

    async def test_without_a_reply_it_explains(self):
        ctx, max_client = _setup()
        update = _rm_update(reply_id=None)

        await _cmd_rm(update, ctx)

        max_client.delete_message.assert_not_awaited()
        assert "/rm" in update.message.reply_text.await_args.args[0]

    async def test_a_message_with_no_max_copy_says_so(self):
        ctx, max_client = _setup(pair=None)
        update = _rm_update()

        await _cmd_rm(update, ctx)

        max_client.delete_message.assert_not_awaited()
        assert "Не нашёл" in update.message.reply_text.await_args.args[0]

    def test_help_mentions_it(self):
        from app.tg_handler import HELP_TEXT

        assert "/rm" in HELP_TEXT


class TestPyMaxClientDelete:
    async def _client(self, sent_by_bridge):
        client = PyMaxClient.__new__(PyMaxClient)
        client._client = MagicMock()
        client._client.delete_message = AsyncMock(return_value=True)
        client.outbox = Outbox(":memory:")
        if sent_by_bridge:
            await client.outbox.mark_sent_by_bridge(MAX_CHAT, MAX_MSG)
        await client.outbox.map_message(DEFAULT_TG_CHAT_ID, 55, MAX_CHAT, MAX_MSG)
        return client

    async def test_the_bridge_s_own_message_goes_for_everyone(self):
        client = await self._client(sent_by_bridge=True)

        assert await client.delete_message(MAX_CHAT, MAX_MSG) == (True, True, "")
        client._client.delete_message.assert_awaited_once_with(
            MAX_CHAT, [MAX_MSG], for_me=False)
        await client.outbox.close()

    async def test_anything_else_only_for_us(self):
        """The bridge does not unsend other people's words."""
        client = await self._client(sent_by_bridge=False)

        assert await client.delete_message(MAX_CHAT, MAX_MSG) == (True, False, "")
        client._client.delete_message.assert_awaited_once_with(
            MAX_CHAT, [MAX_MSG], for_me=True)
        await client.outbox.close()

    async def test_a_deleted_message_is_forgotten(self):
        client = await self._client(sent_by_bridge=True)

        await client.delete_message(MAX_CHAT, MAX_MSG)

        assert await client.max_message_pair(DEFAULT_TG_CHAT_ID, 55) is None
        await client.outbox.close()

    async def test_a_refusal_comes_back_worded(self):
        client = await self._client(sent_by_bridge=True)
        client._client.delete_message = AsyncMock(side_effect=ApiError(
            opcode=66, error="error.delete", message="nope", localized_message="Нельзя удалить"))

        assert await client.delete_message(MAX_CHAT, MAX_MSG) == (False, True, "Нельзя удалить")
        assert await client.max_message_pair(DEFAULT_TG_CHAT_ID, 55) == (MAX_CHAT, MAX_MSG)
        await client.outbox.close()
