"""Resolving max.ru links for /add (app/pymax_client.py).

The shape of a link says less than it looks like it does. MAX gives
public groups and channels a handle link — https://max.ru/id6633015816_gos
is one, belonging to a *channel*, not to a person — so digits in a link
are no evidence of a user id. What settles it is the chat data we already
hold, then MAX itself.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pymax_client import PyMaxClient, _normalized_link, _user_id_in_link


class TestLinkNormalisation:
    @pytest.mark.parametrize("a,b", [
        ("https://max.ru/id42_gos", "https://max.ru/id42_gos"),
        ("https://max.ru/id42_gos", "http://max.ru/id42_gos/"),
        ("https://max.ru/id42_gos", "  HTTPS://MAX.RU/ID42_GOS  "),
        ("https://max.ru/id42_gos", "https://web.max.ru/id42_gos"),
    ])
    def test_the_same_link_written_differently_still_matches(self, a, b):
        assert _normalized_link(a) == _normalized_link(b)

    def test_different_links_stay_different(self):
        assert _normalized_link("https://max.ru/id42_gos") != \
               _normalized_link("https://max.ru/id43_gos")


def _client_with(chats_raw=None):
    client = PyMaxClient.__new__(PyMaxClient)   # no connection, no auth
    client._my_id = 100
    client.resolver = MagicMock()
    client.resolver.chats_raw = chats_raw if chats_raw is not None else {}
    client._client = MagicMock()
    client._client.join_group = AsyncMock(side_effect=ValueError("Invalid group link"))
    client._client.join_channel = AsyncMock(side_effect=RuntimeError("MAX refused"))
    return client


CHANNEL = {
    "id": -69369957050939,
    "type": "CHANNEL",
    "title": 'МАДОУ детский сад №43 "Малыш"',
    "link": "https://max.ru/id6633015816_gos",
    "participants": {"100": 0},
}


class TestAChatWeAlreadyHave:
    """The case that sent /add down the wrong path entirely: a channel's
    own handle link, mistaken for a person's profile, bound a dialog that
    does not exist while the channel stayed unbound."""

    async def test_a_handle_link_binds_the_chat_it_belongs_to(self):
        client = _client_with({-69369957050939: CHANNEL})

        result = await client.open_by_link("https://max.ru/id6633015816_gos")

        assert result["chatId"] == -69369957050939
        assert result["chat"]["title"] == 'МАДОУ детский сад №43 "Малыш"'

    async def test_it_needs_neither_joining_nor_asking(self):
        """We have the chat because we're in it — there is nothing to
        join and nothing to look up."""
        client = _client_with({-69369957050939: CHANNEL})

        await client.open_by_link("https://max.ru/id6633015816_gos")

        client._client.join_group.assert_not_awaited()
        client._client.join_channel.assert_not_awaited()

    async def test_a_link_written_differently_still_finds_it(self):
        client = _client_with({-69369957050939: CHANNEL})

        result = await client.open_by_link("http://web.max.ru/id6633015816_gos/")

        assert result["chatId"] == -69369957050939

    async def test_an_unrelated_link_is_not_matched(self):
        client = _client_with({-69369957050939: CHANNEL})
        chat = MagicMock()
        chat.id = -1
        client._client.join_group = AsyncMock(return_value=chat)

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert result["chatId"] == -1
        client._client.join_group.assert_awaited_once()

    async def test_chats_without_a_link_are_skipped_not_crashed_on(self):
        client = _client_with({
            -1: {"id": -1, "title": "без ссылки"},
            -2: "not even a dict",
            -69369957050939: CHANNEL,
        })

        result = await client.open_by_link("https://max.ru/id6633015816_gos")

        assert result["chatId"] == -69369957050939


class TestJoinLinks:
    async def test_a_join_link_goes_through_pymax(self):
        client = _client_with()
        chat = MagicMock()
        chat.id = -75107924425434
        client._client.join_group = AsyncMock(return_value=chat)

        result = await client.open_by_link("https://max.ru/join/abcdef")

        assert result["chatId"] == -75107924425434
        client._client.join_group.assert_awaited_once()


class TestJoinRefusedByMax:
    """A join MAX answers with not.found — which it also says for a chat
    you're already in — or with error.user.restricted.join, seen live on
    an account that may not join anything at all."""

    def _client_that_cannot_join(self, link_info_chat=None):
        client = _client_with()
        client._client.join_group = AsyncMock(side_effect=RuntimeError("Не найдено [not.found]"))
        response = MagicMock()
        response.payload = {"chat": link_info_chat} if link_info_chat else {}
        client._client._app = MagicMock()
        client._client._app.invoke = AsyncMock(return_value=response)
        return client

    async def test_a_chat_we_are_already_in_is_bound_despite_the_refused_join(self):
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
        we never entered could never receive a message."""
        client = self._client_that_cannot_join(
            link_info_chat={"id": -68192506787240, "type": "CHAT",
                            "title": "Чужой чат", "participants": {"999": 0}},
        )

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert "chatId" not in result
        assert "not.found" in result["_max_error"]["message"]

    async def test_nothing_resolved_reports_the_join_error(self):
        client = self._client_that_cannot_join()

        result = await client.open_by_link("https://max.ru/join/sometoken")

        assert "not.found" in result["_max_error"]["message"]


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


class TestOpeningADialogWithAPerson:
    """A dialog is derived, not joined: MAX's id for the chat between two
    people is the XOR of their user ids — checked against live dialogs,
    it holds exactly. What must come from the server is whether the
    person exists at all."""

    def _client_for_dialog(self, user=None, cached_name=None):
        client = _client_with()
        client._client.get_user = AsyncMock(return_value=user)
        client._client.get_chat_id = MagicMock(side_effect=lambda a, b: a ^ b)
        client.resolver.users = {42: cached_name} if cached_name else {}
        client.resolver._extract_name_from_contact = MagicMock(return_value="Олег")
        return client

    async def test_the_dialog_id_is_derived_from_both_participants(self):
        client = self._client_for_dialog(user=MagicMock())

        result = await client.open_dialog_with_user(42)

        assert result["chatId"] == 100 ^ 42
        assert result["chat"]["type"] == "DIALOG"
        assert result["chat"]["title"] == "Олег"

    async def test_a_user_max_does_not_know_is_refused(self):
        """The check that tells a person from a public channel handle:
        MAX answers a lookup for channel digits with an empty list, and
        binding on the guess produced a topic wired to nothing."""
        client = self._client_for_dialog(user=None)

        result = await client.open_dialog_with_user(6633015816)

        assert "chatId" not in result
        assert "6633015816" in result["_max_error"]["message"]

    async def test_a_name_we_already_have_is_proof_enough(self):
        client = self._client_for_dialog(cached_name="Наринэ Ермилова")

        result = await client.open_dialog_with_user(42)

        assert result["chat"]["title"] == "Наринэ Ермилова"
        client._client.get_user.assert_not_awaited()

    async def test_it_waits_until_max_has_told_us_who_we_are(self):
        client = self._client_for_dialog(user=MagicMock())
        client._my_id = None

        result = await client.open_dialog_with_user(42)

        assert "chatId" not in result
        client._client.get_user.assert_not_awaited()

    async def test_a_lookup_that_never_answers_refuses_rather_than_hangs(self, monkeypatch):
        monkeypatch.setattr("app.pymax_client.USER_LOOKUP_TIMEOUT", 0.01)
        client = self._client_for_dialog()

        async def _never_answers(_user_id):
            await asyncio.sleep(3600)

        client._client.get_user = _never_answers

        result = await asyncio.wait_for(client.open_dialog_with_user(42), timeout=5)

        assert "chatId" not in result


class TestAProfileLinkIsOnlyAHint:
    @pytest.mark.parametrize("link,expected", [
        ("https://max.ru/id42", 42),
        ("https://max.ru/id6633015816_gos", 6633015816),
        ("https://web.max.ru/id42/", 42),
        ("https://max.ru/join/abc", None),
        ("https://max.ru/username", None),
        ("", None),
    ])
    def test_it_reads_the_digits_without_concluding_anything(self, link, expected):
        assert _user_id_in_link(link) == expected

    async def test_a_person_is_tried_only_after_the_chat_paths_fail(self):
        """The channel handle that started this: both link and dialog
        shapes match it, and only the chat reading is right."""
        client = _client_with({-69369957050939: CHANNEL})
        client._client.get_user = AsyncMock(return_value=MagicMock())
        client._client.get_chat_id = MagicMock(side_effect=lambda a, b: a ^ b)

        result = await client.open_by_link("https://max.ru/id6633015816_gos")

        assert result["chatId"] == -69369957050939   # the channel, not a dialog
        client._client.get_user.assert_not_awaited()

    async def test_an_unknown_link_falls_through_to_the_person(self):
        client = _client_with()
        client._client.join_group = AsyncMock(side_effect=RuntimeError("не найдено"))
        response = MagicMock()
        response.payload = {}
        client._client._app = MagicMock()
        client._client._app.invoke = AsyncMock(return_value=response)
        client._client.get_user = AsyncMock(return_value=MagicMock())
        client._client.get_chat_id = MagicMock(side_effect=lambda a, b: a ^ b)
        client.resolver.users = {}
        client.resolver._extract_name_from_contact = MagicMock(return_value="Олег")

        result = await client.open_by_link("https://max.ru/id42")

        assert result["chatId"] == 100 ^ 42

    async def test_when_max_knows_neither_the_join_error_is_reported(self):
        client = _client_with()
        client._client.join_group = AsyncMock(side_effect=RuntimeError("не найдено"))
        response = MagicMock()
        response.payload = {}
        client._client._app = MagicMock()
        client._client._app.invoke = AsyncMock(return_value=response)
        client._client.get_user = AsyncMock(return_value=None)
        client.resolver.users = {}

        result = await client.open_by_link("https://max.ru/id6633015816_gos")

        assert "не найдено" in result["_max_error"]["message"]
