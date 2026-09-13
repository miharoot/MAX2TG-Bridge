"""Pulling MAX history in: on a freshly bound topic, and after downtime.

A topic bound to a chat used to open empty — everything said in MAX
before the binding stayed there. And a message that arrived while the
bridge was down was simply missed: MAX keeps it, but nothing asked for
it on reconnect.
"""

from unittest.mock import AsyncMock, MagicMock


from app.outbox import Outbox


class TestSeenMarks:
    """How far each chat has been forwarded, kept in the outbox database
    rather than a JSON file: it's written on every delivered message."""

    async def test_a_mark_is_stored_and_read_back(self, tmp_path):
        box = Outbox(str(tmp_path / "outbox.db"))
        await box.mark_seen(-42, 1700000000000, "msg-1")

        assert await box.seen_mark(-42) == 1700000000000
        assert await box.seen_marks() == {"-42": 1700000000000}
        await box.close()

    async def test_a_newer_message_moves_the_mark(self, tmp_path):
        box = Outbox(str(tmp_path / "outbox.db"))
        await box.mark_seen(-42, 100)
        await box.mark_seen(-42, 200)

        assert await box.seen_mark(-42) == 200
        await box.close()

    async def test_an_older_message_does_not_move_it_back(self, tmp_path):
        """A retry, or an out-of-order delivery, must not make the bridge
        re-send everything that came after."""
        box = Outbox(str(tmp_path / "outbox.db"))
        await box.mark_seen(-42, 200)
        await box.mark_seen(-42, 100)

        assert await box.seen_mark(-42) == 200
        await box.close()

    async def test_an_unknown_chat_has_no_mark(self, tmp_path):
        box = Outbox(str(tmp_path / "outbox.db"))
        assert await box.seen_mark(-42) is None
        await box.close()

    async def test_a_junk_timestamp_is_ignored(self, tmp_path):
        box = Outbox(str(tmp_path / "outbox.db"))
        await box.mark_seen(-42, None)

        assert await box.seen_mark(-42) is None
        await box.close()


class TestFetchRecentMessages:
    def _client(self, raw_messages):
        from app.pymax_client import PyMaxClient

        client = PyMaxClient.__new__(PyMaxClient)
        client._my_id = 100000002
        client._client = MagicMock()
        client._client.fetch_history = AsyncMock(return_value=raw_messages)
        return client

    def _raw(self, msg_id, time_ms, text):
        raw = MagicMock()
        raw.chat_id = -42
        raw.sender = 100000001
        raw.text = text
        raw.time = time_ms
        raw.id = msg_id
        raw.cid = None
        raw.attaches = []
        raw.link = None
        return raw

    async def test_messages_come_back_oldest_first(self):
        client = self._client([self._raw(2, 200, "второе"),
                               self._raw(1, 100, "первое")])

        messages = await client.fetch_recent_messages(-42, 10)

        assert [m.text for m in messages] == ["первое", "второе"]

    async def test_nothing_is_fetched_for_a_zero_limit(self):
        client = self._client([self._raw(1, 100, "x")])

        assert await client.fetch_recent_messages(-42, 0) == []
        client._client.fetch_history.assert_not_awaited()

    async def test_a_failing_history_call_is_not_fatal(self):
        """A chat whose history MAX refuses must not take the binding
        (or the reconnect catch-up) down with it."""
        client = self._client([])
        client._client.fetch_history = AsyncMock(side_effect=RuntimeError("no"))

        assert await client.fetch_recent_messages(-42, 10) == []


class TestBackfillOnBind:
    """The /bind and /add hook: off by default, capped when on."""

    def test_nothing_happens_while_the_option_is_off(self):
        from app.tg_handler import _backfill_new_topic

        client = MagicMock()
        client.backfill_limit = 0
        client.backfill_chat = MagicMock()

        _backfill_new_topic(client, -42)

        client.backfill_chat.assert_not_called()

    async def test_it_asks_for_exactly_the_configured_number(self):
        from app.tg_handler import _backfill_new_topic

        called = {}

        async def _backfill(chat_id, limit):
            called["args"] = (chat_id, limit)

        client = MagicMock()
        client.backfill_limit = 7
        client.backfill_chat = _backfill

        _backfill_new_topic(client, -42)
        import asyncio
        await asyncio.sleep(0)

        assert called["args"] == (-42, 7)


class TestIngestHistory:
    """client.backfill_chat: the same delivery path a live message takes,
    so history lands in the outbox first and survives a Telegram outage."""

    def _client(self, history, topics=None):
        from app.max_listener import configure_pymax_client
        from app.outbox import Outbox
        from tests.test_max_to_tg_outbox import _FakePyMaxClient

        sender = AsyncMock()
        sender.topic_store = MagicMock()
        topics = topics or {}
        sender.topic_store.get_topic = MagicMock(side_effect=lambda cid: topics.get(cid))
        sender.ensure_topic = AsyncMock(return_value=10)
        sender.resolve_chat_id = MagicMock(return_value="-100999")

        client = _FakePyMaxClient()
        client.fetch_recent_messages = AsyncMock(return_value=history)
        configure_pymax_client(client, sender)
        client.outbox = Outbox(":memory:")
        return client, sender

    def _msg(self, message_id, time_ms, text="привет"):
        from app.pymax_client import MaxMessage

        return MaxMessage(chat_id=-42, sender_id=100000001, text=text,
                          timestamp=time_ms, message_id=str(message_id),
                          is_self=False, cid=None, attaches=[], link=None, raw={})

    async def test_every_fetched_message_is_forwarded(self):
        client, sender = self._client([self._msg(1, 100), self._msg(2, 200)])

        count = await client.backfill_chat(-42, 10)

        assert count == 2
        assert sender.send.await_count == 2
        await client.outbox.close()

    async def test_a_catch_up_takes_only_what_is_newer_than_the_mark(self):
        client, sender = self._client(
            [self._msg(1, 100, "старое"), self._msg(2, 300, "новое")])

        count = await client.backfill_chat(-42, 10, since=200)

        assert count == 1
        assert "новое" in sender.send.await_args.args[0]
        await client.outbox.close()

    async def test_forwarding_moves_the_seen_mark(self):
        """Without this a catch-up would re-send the same messages after
        every reconnect."""
        client, _ = self._client([self._msg(1, 100), self._msg(2, 200)])

        await client.backfill_chat(-42, 10)

        assert await client.outbox.seen_mark(-42) == 200
        await client.outbox.close()

    async def test_an_empty_history_forwards_nothing(self):
        client, sender = self._client([])

        assert await client.backfill_chat(-42, 10) == 0
        sender.send.assert_not_awaited()
        await client.outbox.close()
