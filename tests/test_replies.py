"""Replies to a specific message, in both directions.

The bridge pairs every Telegram message it posts or relays with the MAX
message it stands for (outbox table message_map). A Telegram reply in a
topic then goes to MAX as a reply to the paired message, and a MAX reply
to a paired message arrives in Telegram as a reply to it.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.outbox import MESSAGE_MAP_TTL_SEC, Outbox
from app.pymax_client import MaxMessage, PyMaxClient
from app.tg_handler import _on_topic_message, _replied_tg_message_id, redeliver_tg_to_max_text
from tests.test_tg_handler import (
    DEFAULT_TG_CHAT_ID,
    _make_context,
    _make_max_client,
    _make_topic_store,
    _make_update,
)

MAX_CHAT = -10000000000001
OTHER_MAX_CHAT = -10000000000002


@pytest.fixture
async def box():
    o = Outbox(":memory:")
    yield o
    await o.close()


class TestTheMessageMap:
    async def test_a_pair_is_found_both_ways(self, box):
        await box.map_message(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT, "117000000000000001")

        assert await box.max_message_for(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT) == "117000000000000001"
        assert await box.tg_message_for(MAX_CHAT, "117000000000000001", DEFAULT_TG_CHAT_ID) == 77

    async def test_a_pair_from_another_chat_is_not_used(self, box):
        """A reply must never be aimed at a message in some other chat."""
        await box.map_message(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT, "1")

        assert await box.max_message_for(DEFAULT_TG_CHAT_ID, 77, OTHER_MAX_CHAT) is None

    async def test_a_message_sent_in_several_parts_answers_with_the_last(self, box):
        await box.map_message(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT, "1")
        await box.map_message(DEFAULT_TG_CHAT_ID, 78, MAX_CHAT, "1")

        assert await box.tg_message_for(MAX_CHAT, "1", DEFAULT_TG_CHAT_ID) == 78

    async def test_old_pairs_are_dropped(self, box):
        await box.map_message(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT, "1")
        later = time.time() + MESSAGE_MAP_TTL_SEC + 60
        with patch("app.outbox.time.time", return_value=later):
            await box.map_message(DEFAULT_TG_CHAT_ID, 78, MAX_CHAT, "2")

        assert await box.max_message_for(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT) is None
        assert await box.max_message_for(DEFAULT_TG_CHAT_ID, 78, MAX_CHAT) == "2"


def _tg_message(reply_id=None, thread_id=10, topic_created=False):
    message = MagicMock()
    message.message_thread_id = thread_id
    if reply_id is None:
        message.reply_to_message = None
    else:
        message.reply_to_message = MagicMock(message_id=reply_id)
        message.reply_to_message.forum_topic_created = MagicMock() if topic_created else None
    return message


class TestWhatCountsAsAReply:
    def test_a_reply_to_a_message(self):
        assert _replied_tg_message_id(_tg_message(reply_id=77)) == 77

    def test_no_reply(self):
        assert _replied_tg_message_id(_tg_message()) is None

    def test_the_topic_s_own_opening_message_is_not_a_reply(self):
        """Telegram sets it on every message in a forum topic."""
        assert _replied_tg_message_id(_tg_message(reply_id=10, thread_id=10)) is None
        assert _replied_tg_message_id(_tg_message(reply_id=5, topic_created=True)) is None


def _replying_update(reply_id):
    update = _make_update("Да, согласен", thread_id=10)
    update.message.message_id = 90
    update.message.chat_id = DEFAULT_TG_CHAT_ID
    update.message.reply_to_message = MagicMock(message_id=reply_id, forum_topic_created=None)
    return update


class TestTelegramReplyGoesToMaxAsAReply:
    async def test_a_reply_to_a_paired_message_names_it(self):
        max_client = _make_max_client(send_message_return={"id": 5})
        max_client.max_reply_target = AsyncMock(return_value=117000000000000001)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(_replying_update(77), ctx)

        max_client.max_reply_target.assert_awaited_once_with(42, DEFAULT_TG_CHAT_ID, 77)
        max_client.send_message.assert_awaited_once_with(
            42, "Да, согласен", elements=[], reply_to=117000000000000001)

    async def test_a_reply_to_something_unpaired_goes_as_a_plain_message(self):
        max_client = _make_max_client(send_message_return={"id": 5})
        max_client.max_reply_target = AsyncMock(return_value=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(_replying_update(77), ctx)

        max_client.send_message.assert_awaited_once_with(42, "Да, согласен", elements=[])

    async def test_the_reply_is_kept_for_a_retry(self):
        max_client = _make_max_client(send_message_return={"id": 5})
        max_client.max_reply_target = AsyncMock(return_value=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(_replying_update(77), ctx)

        payload = max_client.outbox.add.await_args.args[1]
        assert payload["reply_to_tg_message_id"] == 77

    async def test_a_retry_still_answers_the_same_message(self):
        max_client = _make_max_client(send_message_return={"id": 5})
        max_client.max_reply_target = AsyncMock(return_value=117000000000000001)

        await redeliver_tg_to_max_text(max_client, AsyncMock(), {
            "max_chat_id": 42, "tg_chat_id": DEFAULT_TG_CHAT_ID, "thread_id": 10,
            "tg_message_id": 90, "reply_to_tg_message_id": 77,
            "text": "Да, согласен", "elements": [],
        })

        max_client.send_message.assert_awaited_once_with(
            42, "Да, согласен", elements=[], reply_to=117000000000000001)


class TestPyMaxClientPassesTheReply:
    def _client(self):
        client = PyMaxClient.__new__(PyMaxClient)
        client._client = MagicMock()
        client._client.send_message = AsyncMock(return_value=MagicMock(cid=None, id=5))
        client._outbound_cids = {}
        client.outbox = None
        return client

    async def test_reply_to_reaches_pymax(self):
        client = self._client()

        await client.send_message(MAX_CHAT, "текст", reply_to="117000000000000001")

        assert client._client.send_message.await_args.kwargs["reply_to"] == 117000000000000001

    async def test_without_it_nothing_extra_is_sent(self):
        client = self._client()

        await client.send_message(MAX_CHAT, "текст")

        assert "reply_to" not in client._client.send_message.await_args.kwargs


class TestTelegramSenderReplies:
    async def test_a_reply_is_sent_as_one_that_survives_a_deleted_original(self, tmp_path):
        from app.tg_sender import TelegramSender
        from app.topics import TopicStore

        with patch("app.tg_sender.Bot"):
            sender = TelegramSender("t", str(DEFAULT_TG_CHAT_ID),
                                    TopicStore(str(tmp_path / "topics.json")))
        sender._bot = MagicMock()
        sender._bot.send_message = AsyncMock()

        await sender.send("привет", message_thread_id=5, reply_to_message_id=77)

        params = sender._bot.send_message.await_args.kwargs["reply_parameters"]
        assert params.message_id == 77
        assert params.allow_sending_without_reply is True


def _max_reply(original_id, text="Да, согласен"):
    return MaxMessage(chat_id=MAX_CHAT, sender_id=100000001, text=text, timestamp=200,
                      message_id="117000000000000009", is_self=False, cid=None, attaches=[],
                      link={"type": "REPLY", "message": {"id": original_id, "text": "Встречаемся?"}},
                      raw={})


class TestMaxReplyArrivesAsATelegramReply:
    def _setup(self):
        from app.max_listener import configure_pymax_client
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(return_value=5)
        sender.ensure_topic = AsyncMock(return_value=5)
        sender.resolve_chat_id = MagicMock(return_value=str(DEFAULT_TG_CHAT_ID))
        sender.send = AsyncMock(return_value=MagicMock(message_id=91))

        client = _FakePyMaxClient()
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        return client, sender

    async def test_a_reply_to_a_paired_message_is_a_telegram_reply(self):
        client, sender = self._setup()
        await client.outbox.map_message(DEFAULT_TG_CHAT_ID, 77, MAX_CHAT, "117000000000000001")

        await client._on_message_cb(_max_reply("117000000000000001"))
        await asyncio.sleep(0)

        sender.send.assert_awaited_once()
        assert sender.send.await_args.kwargs["reply_to_message_id"] == 77
        assert "Встречаемся?" not in sender.send.await_args.args[0]
        # …and is itself paired, so a reply to the reply works too.
        assert await client.outbox.max_message_for(
            DEFAULT_TG_CHAT_ID, 91, MAX_CHAT) == "117000000000000009"
        await client.outbox.close()

    async def test_a_reply_to_something_unpaired_still_quotes_the_original(self):
        client, sender = self._setup()

        await client._on_message_cb(_max_reply("117000000000000001"))
        await asyncio.sleep(0)

        sent = [c.args[0] for c in sender.send.await_args_list]
        assert any("Встречаемся?" in text for text in sent)
        assert all("reply_to_message_id" not in c.kwargs for c in sender.send.await_args_list)
        await client.outbox.close()
