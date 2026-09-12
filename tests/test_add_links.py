"""Resolving max.ru links for /add (app/pymax_client.py).

Three link shapes reach open_by_link, and only one of them — a join
token — is something pymax can handle on its own. A profile link names a
person, so there is no chat to join: MAX derives the id of a one-to-one
chat from the two participants, and that is computed locally.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pymax_client import PyMaxClient, _user_id_from_profile_link


class TestProfileLinkParsing:
    @pytest.mark.parametrize("link,expected", [
        ("https://max.ru/id6633015816_gos", 6633015816),
        ("https://max.ru/id6633015816", 6633015816),
        ("https://web.max.ru/id42", 42),
        ("http://max.ru/id42/", 42),
        ("  https://max.ru/id42  ", 42),
        ("HTTPS://MAX.RU/ID42", 42),
    ])
    def test_reads_the_user_id_out_of_a_profile_link(self, link, expected):
        assert _user_id_from_profile_link(link) == expected

    @pytest.mark.parametrize("link", [
        "https://max.ru/join/abcdef",       # an invite, not a profile
        "https://max.ru/username",          # a handle with no numeric id
        "https://max.ru/u/sometoken",
        "https://example.com/id42",         # not MAX at all
        "https://web.max.ru/-75107924425434",  # a chat id, handled elsewhere
        "",
        None,
    ])
    def test_leaves_everything_else_alone(self, link):
        assert _user_id_from_profile_link(link) is None


def _client_with(my_id=100, user=MagicMock()):
    client = PyMaxClient.__new__(PyMaxClient)   # no connection, no auth
    client._my_id = my_id
    client.resolver = None
    client._client = MagicMock()
    client._client.get_user = AsyncMock(return_value=user)
    client._client.get_chat_id = MagicMock(side_effect=lambda a, b: a ^ b)
    client._client.join_group = AsyncMock(side_effect=ValueError("Invalid group link"))
    client._client.join_channel = AsyncMock(side_effect=RuntimeError("MAX refused"))
    return client


class TestOpenDialogWithUser:
    async def test_a_profile_link_resolves_to_the_dialog_without_joining(self):
        """The bug this fixes: /add on a profile link used to be handed to
        join_group/join_channel, which can only answer with an error —
        there is nothing to join behind a person."""
        client = _client_with(my_id=100)

        result = await client.open_by_link("https://max.ru/id42_gos")

        assert result["chatId"] == 100 ^ 42
        assert result["chat"]["type"] == "DIALOG"
        client._client.join_group.assert_not_awaited()
        client._client.join_channel.assert_not_awaited()

    async def test_an_unknown_user_still_gets_their_dialog_bound(self):
        """MAX can decline to say anything about someone who isn't a
        contact. The chat id doesn't depend on that answer, so the bind
        goes ahead — only the name is lost."""
        client = _client_with(my_id=100)
        client._client.get_user = AsyncMock(return_value=None)

        result = await client.open_by_link("https://max.ru/id42")

        assert result["chatId"] == 100 ^ 42

    async def test_no_name_means_no_title_rather_than_a_stand_in_id(self):
        """A title made of the id would look like a real name downstream
        and suppress both /add's own peer lookup and ensure_topic's later
        rename — leaving the topic called by number for good."""
        client = _client_with(my_id=100)
        client._client.get_user = AsyncMock(return_value=None)

        result = await client.open_dialog_with_user(42)

        assert "title" not in result["chat"]

    async def test_a_lookup_that_never_answers_does_not_hang_the_command(self, monkeypatch):
        """Observed live: the request went out and nothing came back, so
        /add waited forever and the user got no reply at all."""
        monkeypatch.setattr("app.pymax_client.USER_LOOKUP_TIMEOUT", 0.01)
        client = _client_with(my_id=100)

        async def _never_answers(_user_id):
            await asyncio.sleep(3600)

        client._client.get_user = _never_answers

        result = await asyncio.wait_for(client.open_dialog_with_user(42), timeout=5)

        assert result["chatId"] == 100 ^ 42

    async def test_it_waits_until_max_has_told_us_who_we_are(self):
        client = _client_with(my_id=None)

        result = await client.open_dialog_with_user(42)

        assert "_max_error" in result
        client._client.get_user.assert_not_awaited()

    async def test_a_failing_lookup_costs_the_name_not_the_bind(self):
        client = _client_with(my_id=100)
        client._client.get_user = AsyncMock(side_effect=RuntimeError("нет связи"))

        result = await client.open_dialog_with_user(42)

        assert result["chatId"] == 100 ^ 42

    async def test_the_resolved_name_becomes_the_title(self):
        user = MagicMock()
        user.names = []
        client = _client_with(user=user)
        client.resolver = MagicMock()
        client.resolver._extract_name_from_contact = MagicMock(return_value="Иван Петров")
        client.resolver.users = {}

        result = await client.open_dialog_with_user(42)

        assert result["chat"]["title"] == "Иван Петров"
        assert client.resolver.users[42] == "Иван Петров"

    async def test_an_unnamed_user_leaves_the_title_open(self):
        client = _client_with(my_id=100)
        client.resolver = MagicMock()
        client.resolver._extract_name_from_contact = MagicMock(return_value="")
        client.resolver.users = {}

        result = await client.open_dialog_with_user(42)

        assert result["chatId"] == 100 ^ 42
        assert "title" not in result["chat"]
        assert client.resolver.users == {}  # nothing worth caching


class TestJoinLinksStillWork:
    async def test_a_join_link_goes_through_pymax_untouched(self):
        client = _client_with()
        chat = MagicMock()
        chat.id = -75107924425434
        client._client.join_group = AsyncMock(return_value=chat)

        result = await client.open_by_link("https://max.ru/join/abcdef")

        assert result["chatId"] == -75107924425434
        client._client.join_group.assert_awaited_once()


class TestJoinRefusedByMax:
    """A join link MAX answers with "not.found" — which it also says for a
    chat you're already in. /add wants a chat id to bind, not membership,
    so the link is put to LINK_INFO before giving up."""

    def _client_that_cannot_join(self, link_info_chat=None):
        client = _client_with()
        client._client.join_group = AsyncMock(side_effect=RuntimeError("Не найдено [not.found]"))
        response = MagicMock()
        response.payload = {"chat": link_info_chat} if link_info_chat else {}
        client._client._app = MagicMock()
        client._client._app.invoke = AsyncMock(return_value=response)
        return client

    async def test_a_chat_we_are_already_in_is_bound_despite_the_refused_join(self):
        """MAX answers not.found for a chat you're already a member of.
        Nothing needs joining there — the bind is what /add was after."""
        client = self._client_that_cannot_join(
            link_info_chat={"id": -68192506787240, "type": "CHAT",
                            "title": "Сотрудники", "participants": {"100": 0}},
        )

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert result["chatId"] == -68192506787240
        client._client._app.invoke.assert_awaited_once()

    async def test_the_error_names_the_chat_so_there_is_a_next_step(self):
        """Knowing which chat the link points at turns "can't join" into
        "join it in MAX, then bind this id"."""
        client = self._client_that_cannot_join(
            link_info_chat={"id": -68192506787240, "type": "CHAT",
                            "participants": {"999": 0}},
        )

        result = await client.open_by_link("https://max.ru/join/sometoken")

        message = result["_max_error"]["message"]
        assert "-68192506787240" in message
        assert "<" not in message   # /add reports errors without parse_mode

    async def test_a_chat_we_are_not_in_is_never_bound_without_joining(self):
        """/add joins; resolving is not joining. A topic bound to a chat
        we never entered could never receive a message, so the join
        failure is reported instead."""
        client = self._client_that_cannot_join(
            link_info_chat={"id": -68192506787240, "type": "CHAT",
                            "title": "Чужой чат", "participants": {"999": 0}},
        )

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert "not.found" in result["_max_error"]["message"]

    async def test_the_join_error_is_what_gets_reported_when_that_fails_too(self):
        """It describes what the user actually typed; a LINK_INFO miss
        would only say the link resolved to nothing."""
        client = self._client_that_cannot_join()

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert "not.found" in result["_max_error"]["message"]


class TestTopicNameComesFromMax:
    """The topic has to be called what the chat is called in MAX."""

    async def test_a_name_max_already_gave_us_needs_no_lookup(self):
        """Contacts and everyone sharing a chat with us are resolved at
        startup — asking again would only risk the lookup that hangs."""
        client = _client_with(my_id=100)
        client.resolver = MagicMock()
        client.resolver.users = {42: "Наринэ Ермилова"}

        result = await client.open_dialog_with_user(42)

        assert result["chat"]["title"] == "Наринэ Ермилова"
        client._client.get_user.assert_not_awaited()

    async def test_a_stranger_is_looked_up(self):
        user = MagicMock()
        client = _client_with(my_id=100, user=user)
        client.resolver = MagicMock()
        client.resolver.users = {}
        client.resolver._extract_name_from_contact = MagicMock(return_value="Олег")

        result = await client.open_dialog_with_user(42)

        client._client.get_user.assert_awaited_once()
        assert result["chat"]["title"] == "Олег"
        assert client.resolver.users[42] == "Олег"


class TestContactLookupIsBounded:
    """The contacts fetch sits under the topic intro card, the resolver's
    name lookups and /add's title pick. Live, one unanswered lookup left
    the card unposted entirely."""

    async def test_a_lookup_that_never_answers_gives_up(self, monkeypatch):
        monkeypatch.setattr("app.pymax_client.USER_LOOKUP_TIMEOUT", 0.01)
        client = _client_with()

        async def _never_answers(_ids):
            await asyncio.sleep(3600)

        client._client.get_users = _never_answers

        result = await asyncio.wait_for(client.fetch_contacts([42]), timeout=5)

        assert result == {}

    async def test_contacts_that_do_answer_come_back(self):
        client = _client_with()
        user = MagicMock()
        user.names = []
        client._client.get_users = AsyncMock(return_value=[user])

        result = await client.fetch_contacts([42])

        assert len(result["contacts"]) == 1
