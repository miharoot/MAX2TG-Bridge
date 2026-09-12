"""Resolving max.ru links for /add (app/pymax_client.py).

Three link shapes reach open_by_link, and only one of them — a join
token — is something pymax can handle on its own. A profile link names a
person, so there is no chat to join: MAX derives the id of a one-to-one
chat from the two participants, and that is computed locally.
"""

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

    async def test_the_user_is_checked_to_exist_first(self):
        """Deriving the chat id needs no server, but binding a topic to a
        chat that can never receive anything helps nobody."""
        client = _client_with()
        client._client.get_user = AsyncMock(return_value=None)

        result = await client.open_by_link("https://max.ru/id42")

        assert "_max_error" in result

    async def test_it_waits_until_max_has_told_us_who_we_are(self):
        client = _client_with(my_id=None)

        result = await client.open_dialog_with_user(42)

        assert "_max_error" in result
        client._client.get_user.assert_not_awaited()

    async def test_a_failing_lookup_is_reported_not_raised(self):
        client = _client_with()
        client._client.get_user = AsyncMock(side_effect=RuntimeError("нет связи"))

        result = await client.open_dialog_with_user(42)

        assert "нет связи" in result["_max_error"]["message"]

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

    async def test_an_unnamed_user_falls_back_to_the_bare_id(self):
        client = _client_with()
        client.resolver = MagicMock()
        client.resolver._extract_name_from_contact = MagicMock(return_value="")
        client.resolver.users = {}

        result = await client.open_dialog_with_user(42)

        assert result["chat"]["title"] == "42"
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
