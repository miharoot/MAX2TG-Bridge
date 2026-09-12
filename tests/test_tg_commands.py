"""Tests for the Telegram command handlers in app/tg_handler.py.

These had no coverage at all, which is how `/bind` shipped broken for
five days: a bad patch dropped its ``args = context.args or []`` line,
so every invocation raised NameError and the user got no reply — no
error message, nothing. The smoke test at the bottom exists to catch
that whole class of breakage for the other commands too.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    SUPERGROUP_KEY,
    TOPIC_STORE_KEY,
    _cmd_bind,
    _cmd_list,
)

TG_CHAT_ID = -100999


def _make_update(text: str = "/bind"):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = None
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = TG_CHAT_ID
    update.effective_user = MagicMock()
    update.effective_user.id = 777
    update.effective_user.username = "someone"
    return update


def _make_context(args=None, existing_topic=None, allowed_user_ids=None):
    ctx = MagicMock()
    ctx.args = args

    topic_store = MagicMock()
    topic_store.get_topic = MagicMock(return_value=existing_topic)
    topic_store.set_topic = MagicMock()

    max_client = MagicMock()
    max_client.resolver = None

    ctx.bot_data = {
        ALLOWED_USER_KEY: allowed_user_ids,
        TOPIC_STORE_KEY: topic_store,
        MAX_CLIENT_KEY: max_client,
    }
    ctx.bot = MagicMock()
    topic = MagicMock()
    topic.message_thread_id = 10
    ctx.bot.create_forum_topic = AsyncMock(return_value=topic)
    return ctx


def _replies(update) -> list[str]:
    return [call.args[0] for call in update.message.reply_text.await_args_list]


class TestCmdBind:
    async def test_binds_a_chat_id_to_a_new_topic(self):
        update = _make_update("/bind -75107924425434")
        ctx = _make_context(args=["-75107924425434"])

        await _cmd_bind(update, ctx)

        ctx.bot.create_forum_topic.assert_awaited_once()
        ctx.bot_data[TOPIC_STORE_KEY].set_topic.assert_called_once_with(
            -75107924425434, TG_CHAT_ID, 10, "-75107924425434",
        )
        assert "Готово" in _replies(update)[0]

    async def test_without_arguments_it_explains_the_usage(self):
        """The regression this file exists for: `/bind` with no argument
        used to raise NameError before it could answer anything."""
        update = _make_update("/bind")
        ctx = _make_context(args=[])

        await _cmd_bind(update, ctx)

        assert "Использование" in _replies(update)[0]
        ctx.bot.create_forum_topic.assert_not_awaited()

    async def test_an_unparsable_chat_id_is_reported(self):
        update = _make_update("/bind не-число")
        ctx = _make_context(args=["не-число"])

        await _cmd_bind(update, ctx)

        assert "Не понял chat_id" in _replies(update)[0]
        ctx.bot.create_forum_topic.assert_not_awaited()

    async def test_an_explicit_title_wins_over_the_chat_id(self):
        update = _make_update("/bind -42 Рабочий чат")
        ctx = _make_context(args=["-42", "Рабочий", "чат"])

        await _cmd_bind(update, ctx)

        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=TG_CHAT_ID, name="Рабочий чат",
        )

    async def test_an_already_bound_chat_is_not_bound_twice(self):
        update = _make_update("/bind -42")
        ctx = _make_context(args=["-42"], existing_topic=10)

        await _cmd_bind(update, ctx)

        assert "уже привязан" in _replies(update)[0]
        ctx.bot.create_forum_topic.assert_not_awaited()

    async def test_a_user_outside_the_allowed_list_gets_nowhere(self):
        update = _make_update("/bind -42")
        ctx = _make_context(args=["-42"], allowed_user_ids=frozenset({1}))

        await _cmd_bind(update, ctx)

        assert _replies(update) == []
        ctx.bot.create_forum_topic.assert_not_awaited()


class TestEveryCommandAnswersSomething:
    """A command handler that raises before replying leaves the user
    staring at silence — exactly the `/bind` failure mode. Walk every
    registered command with an empty argument list and require that it
    neither raises nor stays mute."""

    @pytest.mark.parametrize("name", [
        "bind", "add", "list", "help", "del", "intro", "profile",
    ])
    async def test_command_replies_without_raising(self, name, monkeypatch):
        import app.tg_handler as th

        handler = getattr(th, f"_cmd_{name}")
        update = _make_update(f"/{name}")
        ctx = _make_context(args=[])
        ctx.bot.send_photo = AsyncMock()
        ctx.bot.send_message = AsyncMock()

        max_client = ctx.bot_data[MAX_CLIENT_KEY]
        max_client.open_by_link = AsyncMock(return_value={"_max_error": {"message": "нет"}})
        max_client.resolver = None

        topic_store = ctx.bot_data[TOPIC_STORE_KEY]
        topic_store.all_topics = MagicMock(return_value={})
        topic_store.all_items = MagicMock(return_value=[])

        await handler(update, ctx)

        assert _replies(update), f"/{name} replied nothing at all"


class TestCmdList:
    """What /list prints per chat: everything copyable is monospace and on
    its own line, and the invite link MAX already sent us is shown as-is."""

    def _list_context(self, chats_raw, chat_types=None, topics=None):
        ctx = _make_context(args=[])
        ctx.bot_data[SUPERGROUP_KEY] = TG_CHAT_ID

        resolver = MagicMock()
        resolver.chats_raw = chats_raw
        resolver.chat_types = chat_types or {}
        resolver.is_dm = MagicMock(return_value=False)
        resolver.chat_name = MagicMock(side_effect=lambda cid: str(cid))
        ctx.bot_data[MAX_CLIENT_KEY].resolver = resolver

        topics = topics or {}
        ctx.bot_data[TOPIC_STORE_KEY].get_topic = MagicMock(
            side_effect=lambda cid: topics.get(cid))
        return ctx

    async def test_ids_and_links_are_monospace_on_their_own_lines(self):
        update = _make_update("/list")
        ctx = self._list_context({-42: {"title": "Рабочий чат", "type": "CHAT"}})

        await _cmd_list(update, ctx)

        body = "\n".join(_replies(update))
        assert "<code>-42</code>" in body
        assert "<code>https://web.max.ru/-42</code>" in body
        assert "<a href" not in body      # never a titled link again

    async def test_the_invite_link_from_max_is_shown_when_there_is_one(self):
        """MAX ships a group's invite link in the snapshot, so /list can
        show it without asking for anything."""
        update = _make_update("/list")
        ctx = self._list_context({
            -42: {"title": "Рабочий чат", "type": "CHAT",
                  "link": "https://max.ru/join/abcdef"},
        })

        await _cmd_list(update, ctx)

        assert "<code>https://max.ru/join/abcdef</code>" in "\n".join(_replies(update))

    async def test_a_chat_without_an_invite_link_simply_has_none(self):
        """Conjuring one would mean rework_invite_link, which revokes the
        chat's current link — listing chats must never do that."""
        update = _make_update("/list")
        ctx = self._list_context({-42: {"title": "Рабочий чат", "type": "CHAT"}})

        await _cmd_list(update, ctx)

        assert "max.ru/join" not in "\n".join(_replies(update))
