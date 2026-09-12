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
    _cmd_add,
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


class TestCmdAddWithAnId:
    """/add takes what you have. A chat id has nothing to join — MAX joins
    by link only — so it binds exactly as /bind would, while a positive
    number nobody knows as a chat is read as a person."""

    def _add_context(self, chats_raw=None, dialog_result=None):
        ctx = _make_context(args=[])
        max_client = ctx.bot_data[MAX_CLIENT_KEY]
        resolver = MagicMock()
        resolver.chats_raw = chats_raw or {}
        resolver.chat_types = {}
        # Faithful to the real resolver: chat_name reads .chats, which
        # /add fills from the chat data before picking a title.
        resolver.chats = {cid: chat.get("title") for cid, chat in (chats_raw or {}).items()
                          if chat.get("title")}
        resolver.is_dm = MagicMock(return_value=False)
        resolver.chat_name = MagicMock(
            side_effect=lambda cid: resolver.chats.get(cid, str(cid)))
        max_client.resolver = resolver
        max_client.open_by_link = AsyncMock(return_value={})
        max_client.open_dialog_with_user = AsyncMock(
            return_value=dialog_result or {"chatId": 6746666032, "chat": {"id": 6746666032}})
        max_client.open_dialog_by_phone = AsyncMock(
            return_value={"chatId": 7, "chat": {"id": 7}})
        return ctx

    async def _run(self, ctx, update):
        await _cmd_add(update, ctx)

    async def test_a_channel_id_is_bound_without_asking_max_anything(self):
        update = _make_update("/add -69369957050939")
        ctx = self._add_context(chats_raw={
            -69369957050939: {"id": -69369957050939, "type": "CHANNEL", "title": "Малыш"},
        })
        ctx.args = ["-69369957050939"]

        await self._run(ctx, update)

        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=TG_CHAT_ID, name="Малыш")
        ctx.bot_data[MAX_CLIENT_KEY].open_by_link.assert_not_awaited()

    async def test_an_unknown_negative_id_still_binds(self):
        """Same as /bind: a chat missing from the snapshot is bindable."""
        update = _make_update("/add -42")
        ctx = self._add_context()
        ctx.args = ["-42"]

        await self._run(ctx, update)

        ctx.bot.create_forum_topic.assert_awaited_once()
        ctx.bot_data[TOPIC_STORE_KEY].set_topic.assert_called_once()

    async def test_a_known_positive_chat_id_binds_that_chat(self):
        """A dialog's own id is positive too — the chat we hold wins over
        the person we would otherwise infer from the same digits."""
        update = _make_update("/add 418124176")
        ctx = self._add_context(chats_raw={
            418124176: {"id": 418124176, "type": "DIALOG", "title": "Наринэ"},
        })
        ctx.args = ["418124176"]

        await self._run(ctx, update)

        ctx.bot_data[MAX_CLIENT_KEY].open_dialog_with_user.assert_not_awaited()
        ctx.bot_data[TOPIC_STORE_KEY].set_topic.assert_called_once_with(
            418124176, TG_CHAT_ID, 10, "Наринэ")

    async def test_an_unknown_positive_number_is_taken_for_a_person(self):
        update = _make_update("/add 6633015816")
        ctx = self._add_context()
        ctx.args = ["6633015816"]

        await self._run(ctx, update)

        ctx.bot_data[MAX_CLIENT_KEY].open_dialog_with_user.assert_awaited_once_with(
            6633015816)

    async def test_a_phone_still_goes_to_the_phone_lookup(self):
        update = _make_update("/add +7 999 123-45-67")
        ctx = self._add_context()
        ctx.args = ["+7", "999", "123-45-67"]

        await self._run(ctx, update)

        ctx.bot_data[MAX_CLIENT_KEY].open_dialog_by_phone.assert_awaited_once()
        ctx.bot_data[MAX_CLIENT_KEY].open_dialog_with_user.assert_not_awaited()

    async def test_nonsense_still_explains_the_usage(self):
        update = _make_update("/add ерунда")
        ctx = self._add_context()
        ctx.args = ["ерунда"]

        await self._run(ctx, update)

        assert "Использование" in _replies(update)[0]


class TestSavedMessages:
    """MAX's "Избранное" is a dialog with exactly one participant: you.
    Having no peer to name it after, it used to show up blank everywhere
    and its topic got no card at all."""

    def _resolver(self, my_id=100, participants=None):
        from app.resolver import ContactResolver

        resolver = ContactResolver()
        resolver._my_id = my_id
        resolver.chats_raw = {0: {"id": 0, "type": "DIALOG",
                                  "participants": participants
                                  if participants is not None else {"100": 1}}}
        return resolver

    def test_a_chat_with_only_me_in_it_is_saved_messages(self):
        assert self._resolver().is_saved_messages(0) is True

    def test_a_dialog_with_someone_else_is_not(self):
        resolver = self._resolver(participants={"100": 1, "42": 1})
        assert resolver.is_saved_messages(0) is False

    def test_a_chat_we_know_nothing_about_is_not(self):
        assert self._resolver().is_saved_messages(-42) is False

    def test_it_takes_knowing_who_we_are(self):
        assert self._resolver(my_id=None).is_saved_messages(0) is False

    async def test_list_names_it_rather_than_leaving_it_blank(self):
        update = _make_update("/list")
        ctx = _make_context(args=[])
        ctx.bot_data[SUPERGROUP_KEY] = TG_CHAT_ID
        resolver = self._resolver(my_id=100)
        resolver.chat_types = {0: "DIALOG"}
        ctx.bot_data[MAX_CLIENT_KEY].resolver = resolver
        ctx.bot_data[TOPIC_STORE_KEY].get_topic = MagicMock(return_value=None)

        await _cmd_list(update, ctx)

        body = "\n".join(_replies(update))
        assert "Избранное" in body
        assert "(без названия)" not in body

    async def test_its_topic_gets_a_card_saying_what_it_is(self):
        """The card was skipped outright: the DM branch needs a peer, and
        a chat with yourself has none."""
        from app.tg_handler import post_topic_intro

        max_client = MagicMock()
        max_client.resolver = self._resolver(my_id=100)
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))

        await post_topic_intro(bot, TG_CHAT_ID, max_client, 0, thread_id=7)

        body = bot.send_message.await_args.kwargs["text"]
        assert "Избранное" in body
        bot.pin_chat_message.assert_awaited_once()


class TestDelMax:
    """/del_max acts on MAX itself, where nothing can be undone — unlike
    /del, which only unlinks a Telegram topic."""

    def _ctx(self, chat_type="CHAT", in_main_group=True, leave_result=None):
        ctx = _make_context(args=[])
        ctx.bot_data[SUPERGROUP_KEY] = TG_CHAT_ID if in_main_group else -1
        resolver = MagicMock()
        resolver.chats_raw = {-42: {"id": -42, "type": chat_type}}
        resolver.chat_types = {-42: chat_type}
        resolver.chat_name = MagicMock(return_value="Рабочий чат")
        resolver.is_saved_messages = MagicMock(return_value=False)
        max_client = ctx.bot_data[MAX_CLIENT_KEY]
        max_client.resolver = resolver
        max_client.leave_or_delete_chat = AsyncMock(
            return_value=leave_result or {"left": "вышел из чата"})
        return ctx

    def _callback_update(self, data):
        update = MagicMock()
        update.message = None
        update.callback_query = MagicMock()
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 777
        return update

    async def test_it_only_asks_first_and_changes_nothing_yet(self):
        from app.tg_handler import _cmd_del_max

        update = _make_update("/del_max -42")
        ctx = self._ctx()
        ctx.args = ["-42"]

        await _cmd_del_max(update, ctx)

        ctx.bot_data[MAX_CLIENT_KEY].leave_or_delete_chat.assert_not_awaited()
        assert "Выйти" in _replies(update)[0]

    async def test_outside_the_main_group_it_refuses(self):
        from app.tg_handler import _cmd_del_max

        update = _make_update("/del_max -42")
        ctx = self._ctx(in_main_group=False)
        ctx.args = ["-42"]

        await _cmd_del_max(update, ctx)

        assert "только в основной" in _replies(update)[0]

    async def test_saved_messages_are_never_deleted(self):
        from app.tg_handler import _cmd_del_max

        update = _make_update("/del_max 0")
        ctx = self._ctx()
        ctx.args = ["0"]
        ctx.bot_data[MAX_CLIENT_KEY].resolver.is_saved_messages = MagicMock(
            return_value=True)

        await _cmd_del_max(update, ctx)

        assert "Избранное" in _replies(update)[0]
        ctx.bot_data[MAX_CLIENT_KEY].leave_or_delete_chat.assert_not_awaited()

    async def test_without_an_id_or_a_topic_it_explains_itself(self):
        from app.tg_handler import _cmd_del_max

        update = _make_update("/del_max")
        ctx = self._ctx()
        ctx.args = []
        ctx.bot_data[TOPIC_STORE_KEY].chat_for_topic = MagicMock(return_value=None)

        await _cmd_del_max(update, ctx)

        assert "Использование" in _replies(update)[0]

    async def test_confirming_leaves_the_chat_in_max(self):
        from app.tg_handler import _on_del_max_callback

        ctx = self._ctx()
        update = self._callback_update("delmax:ok:-42")

        await _on_del_max_callback(update, ctx)

        ctx.bot_data[MAX_CLIENT_KEY].leave_or_delete_chat.assert_awaited_once_with(-42)
        assert "Готово" in update.callback_query.edit_message_text.await_args.args[0]

    async def test_cancelling_does_nothing_at_all(self):
        from app.tg_handler import _on_del_max_callback

        ctx = self._ctx()
        update = self._callback_update("delmax:cancel")

        await _on_del_max_callback(update, ctx)

        ctx.bot_data[MAX_CLIENT_KEY].leave_or_delete_chat.assert_not_awaited()

    async def test_a_refusal_from_max_is_reported(self):
        from app.tg_handler import _on_del_max_callback

        ctx = self._ctx(leave_result={"_max_error": {"message": "нельзя"}})
        update = self._callback_update("delmax:ok:-42")

        await _on_del_max_callback(update, ctx)

        assert "нельзя" in update.callback_query.edit_message_text.await_args.args[0]

    async def test_the_topic_is_left_alone(self):
        """Two separate destructive acts stay separate: /del removes the
        topic, /del_max acts in MAX."""
        from app.tg_handler import _on_del_max_callback

        ctx = self._ctx()
        update = self._callback_update("delmax:ok:-42")

        await _on_del_max_callback(update, ctx)

        ctx.bot_data[TOPIC_STORE_KEY].remove.assert_not_called()
