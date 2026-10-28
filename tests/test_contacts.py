"""Adding people to the MAX contacts of the bridged account.

Three ways in: /contact inside a dialog's topic, /add of a person, and
someone new writing in a dialog. All three go through
PyMaxClient.add_contact (pymax's add_contact — MAX's own "add to
contacts" request).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pymax.exceptions import ApiError

from app.pymax_client import MaxMessage, PyMaxClient
from app.tg_handler import MAX_CLIENT_KEY, TOPIC_STORE_KEY, _cmd_add, _cmd_contact
from tests.test_tg_commands import TG_CHAT_ID, _make_context, _make_update, _replies

MY_ID = 100000001
PEER_ID = 1234567890
DIALOG_ID = MY_ID ^ PEER_ID


def _pymax_client():
    client = PyMaxClient.__new__(PyMaxClient)   # no connection, no auth
    client._client = MagicMock()
    client.resolver = MagicMock()
    client.resolver.users = {}
    client.resolver._extract_name_from_contact = MagicMock(return_value="Иван Петров")
    return client


class TestAddContactInPyMaxClient:
    async def test_the_person_is_added_and_their_name_remembered(self):
        client = _pymax_client()
        user = MagicMock()
        client._client.add_contact = AsyncMock(return_value=user)

        with patch("app.pymax_client._user_to_dict", return_value={"id": PEER_ID}):
            resp = await client.add_contact(PEER_ID)

        client._client.add_contact.assert_awaited_once_with(PEER_ID)
        assert resp == {"contact": {"id": PEER_ID}}
        assert client.resolver.users[PEER_ID] == "Иван Петров"

    async def test_a_refusal_comes_back_as_an_error_not_an_exception(self):
        client = _pymax_client()
        client._client.add_contact = AsyncMock(side_effect=ApiError(
            opcode=34, error="error.contact.add", message="nope",
            localized_message="Не получилось",
        ))

        resp = await client.add_contact(PEER_ID)

        assert resp["_max_error"]["localizedMessage"] == "Не получилось"

    async def test_a_server_that_never_answers_is_given_up_on(self, monkeypatch):
        client = _pymax_client()
        monkeypatch.setattr("app.pymax_client.USER_LOOKUP_TIMEOUT", 0.01)

        async def _never(_):
            await asyncio.sleep(10)
        client._client.add_contact = _never

        resp = await client.add_contact(PEER_ID)

        assert "не ответил" in resp["_max_error"]["message"]


def _resolver(chat):
    resolver = MagicMock()
    resolver.my_id = MY_ID
    resolver.chats_raw = {chat["id"]: chat}
    resolver.chat_types = {chat["id"]: chat["type"]}
    resolver.is_dm = MagicMock(side_effect=lambda cid: chat.get("type") == "DIALOG")
    resolver.is_saved_messages = MagicMock(return_value=False)
    resolver.chat_name = MagicMock(return_value=chat.get("title", str(chat["id"])))
    resolver.user_name = MagicMock(
        side_effect=lambda uid: "Иван Петров" if uid == PEER_ID else str(uid))
    return resolver


DIALOG = {"id": DIALOG_ID, "type": "DIALOG",
          "participants": {str(MY_ID): 0, str(PEER_ID): 0}}
GROUP = {"id": -10000000000001, "type": "CHAT", "title": "Рабочий чат",
         "participants": {str(MY_ID): 0, str(PEER_ID): 0}}


class TestCmdContact:
    def _setup(self, chat, add_result=None):
        update = _make_update("/contact")
        update.message.message_thread_id = 144
        update.message.is_topic_message = True
        ctx = _make_context(args=[])
        ctx.bot_data[TOPIC_STORE_KEY].chat_for_topic = MagicMock(return_value=chat["id"])
        ctx.bot_data[TOPIC_STORE_KEY].get_title = MagicMock(return_value=None)
        max_client = ctx.bot_data[MAX_CLIENT_KEY]
        max_client.resolver = _resolver(chat)
        max_client.add_contact = AsyncMock(return_value=add_result or {"contact": {}})
        return update, ctx, max_client

    async def test_the_person_in_the_dialog_is_added(self):
        update, ctx, max_client = self._setup(DIALOG)

        await _cmd_contact(update, ctx)

        max_client.add_contact.assert_awaited_once_with(PEER_ID)
        assert "Иван Петров" in _replies(update)[0]
        assert "в контактах MAX" in _replies(update)[0]

    async def test_the_dialog_id_is_never_taken_for_the_person(self):
        """A dialog's id is the XOR of two user ids, not the other one's
        id — without the participant list there is nobody to add."""
        update, ctx, max_client = self._setup(dict(DIALOG, participants={}))

        await _cmd_contact(update, ctx)

        max_client.add_contact.assert_not_awaited()
        assert "не добавил" in _replies(update)[0]

    async def test_a_group_is_not_a_person(self):
        update, ctx, max_client = self._setup(GROUP)

        await _cmd_contact(update, ctx)

        max_client.add_contact.assert_not_awaited()
        assert "не личный диалог" in _replies(update)[0]

    async def test_a_refusal_from_max_is_reported(self):
        update, ctx, _ = self._setup(DIALOG, add_result={
            "_max_error": {"localizedMessage": "Не получилось"}})

        await _cmd_contact(update, ctx)

        assert "Не получилось" in _replies(update)[0]

    async def test_outside_a_topic_it_says_so(self):
        update, ctx, max_client = self._setup(DIALOG)
        update.message.message_thread_id = None

        await _cmd_contact(update, ctx)

        max_client.add_contact.assert_not_awaited()
        assert "внутри топика" in _replies(update)[0]

    def test_help_mentions_it(self):
        from app.tg_handler import HELP_TEXT

        assert "/contact" in HELP_TEXT


class TestAddPutsThePersonIntoContacts:
    def _setup(self, chat, existing_topic=None):
        ctx = _make_context(args=[], existing_topic=existing_topic)
        max_client = ctx.bot_data[MAX_CLIENT_KEY]
        resolver = _resolver(chat)
        resolver.chats = {}
        max_client.resolver = resolver
        max_client.open_dialog_with_user = AsyncMock(
            return_value={"chatId": chat["id"], "chat": chat})
        return ctx, max_client

    async def test_a_person_added_by_id_is_added_to_contacts(self):
        update = _make_update(f"/add {PEER_ID}")
        ctx, max_client = self._setup(DIALOG)
        ctx.args = [str(PEER_ID)]

        await _cmd_add(update, ctx)

        max_client.add_contact.assert_awaited_once_with(PEER_ID)
        assert "в контактах MAX" in _replies(update)[-1]

    async def test_an_already_bound_person_is_still_added(self):
        """/add of someone whose dialog has a topic is the way to put
        them into contacts — the binding part has nothing to do."""
        update = _make_update(f"/add {PEER_ID}")
        ctx, max_client = self._setup(DIALOG, existing_topic=7)
        ctx.args = [str(PEER_ID)]

        await _cmd_add(update, ctx)

        max_client.add_contact.assert_awaited_once_with(PEER_ID)
        assert "уже привязан" in _replies(update)[-1]
        assert "в контактах MAX" in _replies(update)[-1]

    async def test_a_group_adds_nobody(self):
        update = _make_update("/add -10000000000001")
        ctx, max_client = self._setup(GROUP)
        ctx.args = ["-10000000000001"]

        await _cmd_add(update, ctx)

        max_client.add_contact.assert_not_awaited()
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=TG_CHAT_ID, name="Рабочий чат")


def _dm_msg(*, is_self=False, chat_id=DIALOG_ID, sender_id=PEER_ID):
    return MaxMessage(chat_id=chat_id, sender_id=sender_id, text="привет",
                      timestamp=100, message_id="1", is_self=is_self, cid=None,
                      attaches=[], link=None, raw={})


class TestSomeoneNewWritingIn:
    def _setup(self, *, topic=None, enabled=True):
        from app.max_listener import configure_pymax_client
        from app.outbox import Outbox
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(return_value=topic)
        sender.ensure_topic = AsyncMock(return_value=topic or 10)
        sender.resolve_chat_id = MagicMock(return_value=str(TG_CHAT_ID))
        sender.send = AsyncMock(return_value=MagicMock(message_id=1))

        client = _FakePyMaxClient()
        client.settings = MagicMock(auto_add_contacts=enabled,
                                    backfill_enabled=False, state_dir="state")
        client.add_contact = AsyncMock(return_value={"contact": {}})
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        return client

    async def _deliver(self, client, msg):
        with patch("app.tg_handler.post_topic_intro", AsyncMock()):
            await client._on_message_cb(msg)
            await asyncio.sleep(0)   # let the fire-and-forget task run
        await client.outbox.close()

    async def test_the_sender_of_a_new_dialog_is_added(self):
        client = self._setup()

        await self._deliver(client, _dm_msg())

        client.add_contact.assert_awaited_once_with(PEER_ID)

    async def test_a_dialog_that_already_has_a_topic_adds_nobody(self):
        client = self._setup(topic=5)

        await self._deliver(client, _dm_msg())

        client.add_contact.assert_not_awaited()

    async def test_our_own_first_message_adds_nobody(self):
        """Only the sender is certain to be the person; when that is us,
        the dialog's id says nothing about who is on the other side."""
        client = self._setup()

        await self._deliver(client, _dm_msg(is_self=True, sender_id=MY_ID))

        client.add_contact.assert_not_awaited()

    async def test_a_group_adds_nobody(self):
        client = self._setup()

        await self._deliver(client, _dm_msg(chat_id=-10000000000001))

        client.add_contact.assert_not_awaited()

    async def test_it_can_be_switched_off(self):
        client = self._setup(enabled=False)

        await self._deliver(client, _dm_msg())

        client.add_contact.assert_not_awaited()


class TestStoredLists:
    """The full chat and contact lists MAX sends at login, kept in the
    outbox database."""

    async def _box(self):
        from app.outbox import Outbox
        return Outbox(":memory:")

    async def test_contacts_are_replaced_by_the_new_full_list(self):
        box = await self._box()
        await box.replace_max_contacts([(1, "Иван Петров", {"id": 1})])

        await box.replace_max_contacts([(2, None, {"id": 2})])

        assert await box.max_contact_ids() == {2}
        await box.close()

    async def test_a_removed_contact_is_not_a_contact(self):
        box = await self._box()
        await box.replace_max_contacts([
            (1, None, {"id": 1, "status": "REMOVED"}),
            (2, None, {"id": 2, "status": "ACTIVE"}),
            (3, None, {"id": 3}),
        ])

        assert await box.max_contact_ids() == {2, 3}
        await box.close()

    async def test_one_added_contact_joins_the_list(self):
        box = await self._box()
        await box.replace_max_contacts([(1, None, {"id": 1})])

        await box.add_max_contact(PEER_ID, "Иван Петров", {"id": PEER_ID})

        assert await box.max_contact_ids() == {1, PEER_ID}
        await box.close()

    async def _chat_ids(self, box):
        conn = await box._get_conn()
        async with conn.execute("SELECT chat_id FROM max_chats") as cur:
            return {row[0] for row in await cur.fetchall()}

    async def test_a_complete_chat_list_drops_chats_no_longer_in_it(self):
        box = await self._box()
        await box.save_max_chats([{"id": -1}, {"id": -2}], complete=True)

        await box.save_max_chats([{"id": -2}], complete=True)

        assert await self._chat_ids(box) == {"-2"}
        await box.close()

    async def test_an_incomplete_one_drops_nothing(self):
        """A walk cut short must not read as having left the chats it
        didn't reach."""
        box = await self._box()
        await box.save_max_chats([{"id": -1}, {"id": -2}], complete=True)

        await box.save_max_chats([{"id": -2, "title": "Рабочий чат"}], complete=False)

        assert await self._chat_ids(box) == {"-1", "-2"}
        await box.close()


class TestListsStoredAtLogin:
    async def _ready(self, snapshot, stored_contacts=()):
        from app.max_listener import configure_pymax_client
        from app.outbox import Outbox
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(return_value=None)
        client = _FakePyMaxClient()
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        if stored_contacts:
            await client.outbox.replace_max_contacts(
                [(uid, None, {"id": uid}) for uid in stored_contacts])

        await client._on_ready_cb(snapshot)
        return client

    async def test_the_contacts_from_max_become_the_known_contacts(self):
        client = await self._ready({
            "chats": [{"id": -10000000000001, "type": "CHAT", "title": "Рабочий чат"}],
            "contacts": [{"id": PEER_ID, "names": [{"name": "Иван Петров"}]}],
            "chats_complete": True,
        })

        assert client.contact_ids == {PEER_ID}
        assert await client.outbox.max_contact_ids() == {PEER_ID}
        await client.outbox.close()

    async def test_no_contacts_from_max_keeps_the_stored_ones(self):
        client = await self._ready({"chats": [], "contacts": []}, stored_contacts=[PEER_ID])

        assert client.contact_ids == {PEER_ID}
        await client.outbox.close()

    async def test_with_nothing_known_the_list_stays_unknown(self):
        client = await self._ready({"chats": [], "contacts": []})

        assert getattr(client, "contact_ids", None) is None
        await client.outbox.close()


class TestAddingWithTheContactListKnown:
    """With the full list in hand, "new" means "not in the contacts", not
    "no topic yet" — someone already talking to you gets added too."""

    def _setup(self, contacts):
        client = TestSomeoneNewWritingIn()._setup(topic=5)
        client.contact_ids = set(contacts)
        return client

    async def test_a_sender_missing_from_the_contacts_is_added(self):
        client = self._setup({1})

        await TestSomeoneNewWritingIn()._deliver(client, _dm_msg())

        client.add_contact.assert_awaited_once_with(PEER_ID)

    async def test_a_contact_is_left_alone(self):
        client = self._setup({PEER_ID})

        await TestSomeoneNewWritingIn()._deliver(client, _dm_msg())

        client.add_contact.assert_not_awaited()

    async def test_each_sender_is_tried_once_a_run(self):
        """A refusal (a service account) isn't asked again for every
        message they send."""
        client = self._setup({1})
        client.add_contact = AsyncMock(return_value={"_max_error": {"message": "нет"}})

        with patch("app.tg_handler.post_topic_intro", AsyncMock()):
            await client._on_message_cb(_dm_msg())
            await client._on_message_cb(MaxMessage(
                chat_id=DIALOG_ID, sender_id=PEER_ID, text="ещё", timestamp=200,
                message_id="2", is_self=False, cid=None, attaches=[], link=None, raw={}))
            await asyncio.sleep(0)
        await client.outbox.close()

        client.add_contact.assert_awaited_once_with(PEER_ID)


class TestAnAddedContactIsRemembered:
    async def test_it_joins_the_known_contacts_and_the_database(self):
        from app.outbox import Outbox

        client = _pymax_client()
        client.contact_ids = {1}
        client.outbox = Outbox(":memory:")
        client._client.add_contact = AsyncMock(return_value=MagicMock())

        with patch("app.pymax_client._user_to_dict", return_value={"id": PEER_ID}):
            await client.add_contact(PEER_ID)

        assert client.contact_ids == {1, PEER_ID}
        assert await client.outbox.max_contact_ids() == {PEER_ID}
        await client.outbox.close()


class TestTheLoginLogLine:
    async def test_it_gives_both_what_max_sent_and_what_the_db_holds(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="app.max_listener"):
            client = await TestListsStoredAtLogin()._ready({
                "chats": [{"id": -10000000000001, "type": "CHAT"}],
                "contacts": [{"id": 1, "status": "ACTIVE"},
                             {"id": 2, "status": "REMOVED"}],
                "chats_complete": True,
            })
        await client.outbox.close()

        line = next(r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("MAX lists at login"))
        assert line == ("MAX lists at login: chats 1 from MAX, 1 in DB; contacts 2 "
                        "from MAX (statuses: {'ACTIVE': 1, 'REMOVED': 1}), 2 in DB, "
                        "1 of them current")
