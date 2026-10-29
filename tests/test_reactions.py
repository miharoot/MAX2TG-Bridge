"""Reactions in both directions, on the message they belong to.

The message_map pairs (see tests/test_replies.py) say which Telegram
message is which MAX message; a reaction on one is put on the other.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.max_listener import _reactions_of_others
from app.outbox import Outbox
from app.pymax_client import MaxReactionEvent, PyMaxClient
from app.tg_handler import ALLOWED_USER_KEY, MAX_CLIENT_KEY, _on_message_reaction
from app.tg_sender import telegram_reaction

TG_CHAT = -100999
MAX_CHAT = -10000000000001


class TestWhichReactionsAreOthers:
    def test_ours_is_left_out(self):
        counters = [{"reaction": "👍", "count": 2}, {"reaction": "🔥", "count": 1}]

        assert _reactions_of_others(counters, "👍") == [("👍", 1), ("🔥", 1)]

    def test_only_ours_means_none(self):
        assert _reactions_of_others([{"reaction": "👍", "count": 1}], "👍") == []

    def test_most_given_first(self):
        counters = [{"reaction": "🔥", "count": 1}, {"reaction": "👍", "count": 3}]

        assert _reactions_of_others(counters, None)[0] == ("👍", 3)


class TestTelegramSpelling:
    @pytest.mark.parametrize("emoji,expected", [
        ("👍", "👍"),
        ("❤️", "❤"),      # MAX writes it with U+FE0F, Telegram without
        ("🫠", None),     # not in Telegram's set for bots
        (None, None),
    ])
    def test_it_is_matched_to_telegram_s_set(self, emoji, expected):
        assert telegram_reaction(emoji) == expected


class TestMaxReactionOnTheTelegramCopy:
    async def _setup(self, paired=True):
        from app.max_listener import configure_pymax_client
        from tests.test_read_reaction import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(return_value=7)
        sender.resolve_chat_id = MagicMock(return_value=str(TG_CHAT))
        sender.set_reaction = AsyncMock(return_value=True)
        sender.clear_reaction = AsyncMock(return_value=True)
        client = _FakePyMaxClient()
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        if paired:
            await client.outbox.map_message(TG_CHAT, 55, MAX_CHAT, "117000000000000001")
        return client, sender

    def _event(self, counters, yours=None):
        return MaxReactionEvent(chat_id=MAX_CHAT, message_id="117000000000000001",
                                counters=counters, your_reaction=yours)

    async def test_it_lands_on_the_paired_message(self):
        client, sender = await self._setup()

        await client._on_reaction_cb(self._event([{"reaction": "❤️", "count": 1}]))

        sender.set_reaction.assert_awaited_once_with(str(TG_CHAT), 55, "❤")
        sender.send.assert_not_called()
        await client.outbox.close()

    async def test_all_taken_off_clears_it(self):
        client, sender = await self._setup()

        await client._on_reaction_cb(self._event([]))

        sender.clear_reaction.assert_awaited_once_with(str(TG_CHAT), 55)
        await client.outbox.close()

    async def test_our_own_reaction_is_not_mirrored_back(self):
        """It was set in Telegram in the first place."""
        client, sender = await self._setup()

        await client._on_reaction_cb(self._event([{"reaction": "👍", "count": 1}], yours="👍"))

        sender.set_reaction.assert_not_called()
        sender.send.assert_not_called()
        await client.outbox.close()

    async def test_one_telegram_can_t_show_becomes_a_neutral_reaction(self):
        """Not a 👍 — that would claim approval nobody gave."""
        client, sender = await self._setup()

        await client._on_reaction_cb(self._event([{"reaction": "🫠", "count": 1}]))

        sender.set_reaction.assert_awaited_once_with(str(TG_CHAT), 55, "💊")
        sender.send.assert_not_called()
        await client.outbox.close()

    async def test_a_reaction_telegram_refuses_becomes_a_line_under_the_message(self):
        """The group may allow only some reactions."""
        client, sender = await self._setup()
        sender.set_reaction = AsyncMock(return_value=False)

        await client._on_reaction_cb(self._event([{"reaction": "🫠", "count": 1}]))

        line = sender.send.await_args.args[0]
        assert "🫠×1" in line and not line.startswith("👍")
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 55
        await client.outbox.close()

    async def test_an_unpaired_message_still_gets_the_status_line(self):
        client, sender = await self._setup(paired=False)

        await client._on_reaction_cb(self._event([{"reaction": "👍", "count": 2}]))

        sender.set_reaction.assert_not_called()
        assert "👍×2" in sender.send.await_args.args[0]
        await client.outbox.close()


def _reaction_update(new, *, user_id=777, is_bot=False, message_id=55):
    update = MagicMock()
    change = update.message_reaction
    change.chat.id = TG_CHAT
    change.message_id = message_id
    change.user = MagicMock(id=user_id, is_bot=is_bot)
    change.new_reaction = [MagicMock(emoji=e) if e else MagicMock(emoji=None) for e in new]
    return update


def _context(pair=(MAX_CHAT, 117000000000000001), allowed=None):
    max_client = MagicMock()
    max_client.max_message_pair = AsyncMock(return_value=pair)
    max_client.set_reaction = AsyncMock(return_value=True)
    ctx = MagicMock()
    ctx.bot_data = {MAX_CLIENT_KEY: max_client, ALLOWED_USER_KEY: allowed}
    return ctx, max_client


class TestTelegramReactionGoesToMax:
    async def test_it_lands_on_the_paired_max_message(self):
        ctx, max_client = _context()

        await _on_message_reaction(_reaction_update(["🔥"]), ctx)

        max_client.max_message_pair.assert_awaited_once_with(TG_CHAT, 55)
        max_client.set_reaction.assert_awaited_once_with(
            MAX_CHAT, 117000000000000001, "🔥")

    async def test_taking_it_off_takes_it_off_in_max(self):
        ctx, max_client = _context()

        await _on_message_reaction(_reaction_update([]), ctx)

        max_client.set_reaction.assert_awaited_once_with(MAX_CHAT, 117000000000000001, None)

    async def test_a_message_with_no_max_copy_is_left_alone(self):
        ctx, max_client = _context(pair=None)

        await _on_message_reaction(_reaction_update(["🔥"]), ctx)

        max_client.set_reaction.assert_not_awaited()

    async def test_a_custom_emoji_has_nothing_to_become(self):
        ctx, max_client = _context()

        await _on_message_reaction(_reaction_update([None]), ctx)

        max_client.set_reaction.assert_not_awaited()

    async def test_someone_outside_the_allowed_list_is_ignored(self):
        ctx, max_client = _context(allowed=frozenset({1}))

        await _on_message_reaction(_reaction_update(["🔥"], user_id=777), ctx)

        max_client.set_reaction.assert_not_awaited()

    async def test_a_bot_is_ignored(self):
        ctx, max_client = _context()

        await _on_message_reaction(_reaction_update(["🔥"], is_bot=True), ctx)

        max_client.set_reaction.assert_not_awaited()


class TestPyMaxClientReactions:
    def _client(self):
        client = PyMaxClient.__new__(PyMaxClient)
        client._client = MagicMock()
        client._client.add_reaction = AsyncMock()
        client._client.remove_reaction = AsyncMock()
        return client

    async def test_a_reaction_is_added(self):
        client = self._client()

        assert await client.set_reaction(MAX_CHAT, "117000000000000001", "🔥") is True
        client._client.add_reaction.assert_awaited_once_with(
            MAX_CHAT, 117000000000000001, "🔥")

    async def test_none_removes_ours(self):
        client = self._client()

        await client.set_reaction(MAX_CHAT, 1, None)

        client._client.remove_reaction.assert_awaited_once_with(MAX_CHAT, 1)

    async def test_a_refusal_is_false_not_an_exception(self):
        client = self._client()
        client._client.add_reaction = AsyncMock(side_effect=RuntimeError("not allowed"))

        assert await client.set_reaction(MAX_CHAT, 1, "🫠") is False

    async def test_the_pair_comes_back_as_ints(self):
        client = self._client()
        client.outbox = Outbox(":memory:")
        await client.outbox.map_message(TG_CHAT, 55, MAX_CHAT, "117000000000000001")

        assert await client.max_message_pair(TG_CHAT, 55) == (MAX_CHAT, 117000000000000001)
        await client.outbox.close()
