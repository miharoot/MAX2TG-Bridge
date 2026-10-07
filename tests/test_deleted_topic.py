"""A forum topic deleted by hand in Telegram while the bridge still has it
on file. Every MAX message for that chat used to be sent into the dead
topic, refused, and then taken out of the outbox as if delivered — lost
without a word, for as long as the binding stayed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from app.pymax_client import MaxMessage
from app.tg_sender import TelegramSender, TopicGone, _is_topic_gone
from app.topics import TopicStore

GROUP = -10000000000001
OTHER_GROUP = -10000000000002


def _sender(tmp_path):
    store = TopicStore(str(tmp_path / "topics.json"))
    with patch("app.tg_sender.Bot"):
        sender = TelegramSender("dummy-token", str(GROUP), store)
    sender._bot = MagicMock()
    return sender, store


class TestRecognisingADeletedTopic:
    @pytest.mark.parametrize("text", [
        "Message thread not found",
        "Bad Request: message thread not found",
        "TOPIC_DELETED",
        "Topic_id_invalid",
    ])
    def test_telegrams_wordings_are_recognised(self, text):
        assert _is_topic_gone(BadRequest(text))

    def test_a_closed_topic_is_not_a_deleted_one(self):
        assert not _is_topic_gone(BadRequest("TOPIC_CLOSED"))

    async def test_sending_into_it_raises_at_once(self, tmp_path, monkeypatch):
        """No three attempts and no quiet None — a topic doesn't come back."""
        sender, _ = _sender(tmp_path)
        monkeypatch.setattr("app.tg_sender.asyncio.sleep", AsyncMock())
        sender._bot.send_message = AsyncMock(side_effect=BadRequest("Message thread not found"))

        with pytest.raises(TopicGone):
            await sender.send("привет", message_thread_id=5, chat_id=GROUP)

        sender._bot.send_message.assert_awaited_once()

    async def test_any_other_refusal_still_ends_in_none(self, tmp_path, monkeypatch):
        sender, _ = _sender(tmp_path)
        monkeypatch.setattr("app.tg_sender.asyncio.sleep", AsyncMock())
        sender._bot.send_message = AsyncMock(side_effect=BadRequest("Can't parse entities"))

        assert await sender.send("привет", message_thread_id=5, chat_id=GROUP) is None


class TestRecreateTopic:
    async def test_same_group_and_title(self, tmp_path):
        """Recreating through ensure_topic would have sent a chat bound with
        /bind into another group back to the default one."""
        sender, store = _sender(tmp_path)
        store.set_topic(-42, OTHER_GROUP, 5, "Рабочий чат")
        sender._bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=9))

        thread, created = await sender.recreate_topic(-42, 5)

        assert (thread, created) == (9, True)
        sender._bot.create_forum_topic.assert_awaited_once_with(chat_id=OTHER_GROUP, name="Рабочий чат")
        assert store.get_topic(-42) == 9
        assert store.get_chat_id(-42) == OTHER_GROUP

    async def test_a_topic_already_replaced_is_not_replaced_again(self, tmp_path):
        """Two messages can hit the dead topic at once; only one new topic."""
        sender, store = _sender(tmp_path)
        store.set_topic(-42, GROUP, 9, "Рабочий чат")   # already recreated
        sender._bot.create_forum_topic = AsyncMock()

        assert await sender.recreate_topic(-42, 5) == (9, False)
        sender._bot.create_forum_topic.assert_not_awaited()

    async def test_a_refusal_to_create_is_reported_as_none(self, tmp_path):
        sender, store = _sender(tmp_path)
        store.set_topic(-42, GROUP, 5, "Рабочий чат")
        sender._bot.create_forum_topic = AsyncMock(side_effect=BadRequest("not enough rights"))

        assert await sender.recreate_topic(-42, 5) == (None, False)
        assert store.get_topic(-42) == 5


def _msg(message_id, time_ms, text="привет", chat_id=-42):
    return MaxMessage(chat_id=chat_id, sender_id=100000001, text=text,
                      timestamp=time_ms, message_id=str(message_id),
                      is_self=False, cid=None, attaches=[], link=None, raw={})


class TestDeliveryIntoADeletedTopic:
    def _setup(self, history=()):
        from app.max_listener import configure_pymax_client
        from app.outbox import Outbox
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        topics = {-42: 5}
        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(side_effect=lambda cid: topics.get(cid))
        sender.ensure_topic = AsyncMock(side_effect=lambda cid, *a, **k: topics.get(cid))
        sender.resolve_chat_id = MagicMock(return_value=str(GROUP))

        async def _recreate(cid, dead):
            topics[cid] = 9
            return 9, True
        sender.recreate_topic = AsyncMock(side_effect=_recreate)

        sent: list = []

        async def _send(text, message_thread_id=None, chat_id=None):
            if message_thread_id == 5:
                raise TopicGone("Message thread not found")
            sent.append((message_thread_id, text))
            return MagicMock(message_id=len(sent))
        sender.send = AsyncMock(side_effect=_send)

        client = _FakePyMaxClient()
        client.fetch_recent_messages = AsyncMock(return_value=list(history))
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        return client, sender, sent

    async def test_the_topic_is_recreated_and_the_message_delivered(self):
        client, sender, sent = self._setup()

        with patch("app.tg_handler.post_topic_intro", AsyncMock()) as intro:
            await client._on_message_cb(_msg(3, 300, "новое"))

        sender.recreate_topic.assert_awaited_once_with(-42, 5)
        intro.assert_awaited_once()
        assert [t for t, _ in sent] == [9]
        assert "новое" in sent[-1][1]
        assert await client.outbox.count() == 0
        await client.outbox.close()

    async def test_history_comes_first_and_stops_short_of_the_message(self):
        """The new topic gets the conversation leading up to the message —
        and not the message itself a second time."""
        history = [_msg(1, 100, "раньше"), _msg(2, 200, "ещё раньше?"), _msg(3, 300, "новое")]
        client, sender, sent = self._setup(history)

        with patch("app.tg_handler.post_topic_intro", AsyncMock()):
            await client._on_message_cb(_msg(3, 300, "новое"))

        texts = [text for _, text in sent]
        assert len(texts) == 3
        assert "раньше" in texts[0] and "ещё раньше?" in texts[1] and "новое" in texts[2]

    async def test_a_second_failure_keeps_the_message_queued(self):
        """If the new topic fails too, the outbox must still hold it."""
        client, sender, sent = self._setup()
        sender.recreate_topic = AsyncMock(return_value=(None, False))

        with patch("app.tg_handler.post_topic_intro", AsyncMock()):
            await client._on_message_cb(_msg(3, 300, "новое"))

        assert await client.outbox.count() == 1
        await client.outbox.close()

    async def test_the_old_read_receipt_anchor_is_forgotten(self):
        client, sender, sent = self._setup()
        client.last_tg_message[-42] = (str(GROUP), 77)

        with patch("app.tg_handler.post_topic_intro", AsyncMock()):
            await client._on_message_cb(_msg(3, 300, "новое"))

        assert client.last_tg_message[-42] != (str(GROUP), 77)


class TestTelegramRefusingIsNotDelivery:
    async def test_a_message_telegram_did_not_take_stays_queued(self):
        """send() gives up with None after its retries; that used to read
        as success and the message was taken out of the outbox."""
        from app.max_listener import configure_pymax_client
        from app.outbox import Outbox
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        sender.topic_store.get_topic = MagicMock(return_value=5)
        sender.ensure_topic = AsyncMock(return_value=5)
        sender.resolve_chat_id = MagicMock(return_value=str(GROUP))
        sender.send = AsyncMock(return_value=None)
        client = _FakePyMaxClient()
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")

        await client._on_message_cb(_msg(3, 300, "новое"))

        assert await client.outbox.count() == 1
        await client.outbox.close()
