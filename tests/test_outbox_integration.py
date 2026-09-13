"""End-to-end checks of the delivery outbox against a real SQLite file.

The rest of the outbox tests mock the store or the retry dispatch. These
drive the actual loop — queue, fail, sweep, redeliver, remove — through
app.outbox.Outbox on disk, which is what has to hold up when a message
outlives a failure or a restart.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.outbox import (
    TG_TO_MAX_MEDIA,
    TG_TO_MAX_TEXT,
    Outbox,
    PermanentDeliveryFailure,
)
from app.outbox_retry import _retry_one, run_outbox_retry_loop


@pytest.fixture
async def store(tmp_path):
    ob = Outbox(str(tmp_path / "outbox.db"))
    yield ob
    await ob.close()


def _client(store):
    client = MagicMock()
    client.outbox = store
    return client


def _sender():
    sender = MagicMock()
    sender.bot = MagicMock()
    return sender


TEXT_PAYLOAD = {
    "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
    "tg_message_id": 501, "text": "hello", "elements": [],
}


class TestFailedDeliveryIsRetriedUntilItSucceeds:
    async def test_message_survives_a_failure_and_is_redelivered(self, store):
        """The core promise: a send that fails stays queued, and a later
        sweep delivers it and clears the row."""
        item_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        await store.mark_failed(item_id, "MAX unreachable")

        assert len(await store.pending()) == 1

        # attempts == 1 → backoff is 60s, so pretend that has passed
        item = (await store.pending())[0]
        item.last_attempt_at = time.time() - 120

        redeliver = AsyncMock(return_value=True)
        await _retry_one(_client(store), _sender(), 1024, item, redeliver, AsyncMock())

        redeliver.assert_awaited_once()
        assert await store.pending() == []  # delivered → no longer queued

    async def test_still_failing_delivery_stays_queued_and_counts_attempts(self, store):
        item_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        item = (await store.pending())[0]

        redeliver = AsyncMock(return_value=False)
        for _ in range(3):
            await _retry_one(_client(store), _sender(), 1024, item, redeliver, AsyncMock())

        rows = await store.pending()
        assert len(rows) == 1
        assert rows[0].id == item_id
        assert rows[0].attempts == 3
        assert rows[0].last_error

    async def test_an_exception_mid_delivery_keeps_the_row(self, store):
        """A crash in the middle of redelivery must not lose the message."""
        await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        item = (await store.pending())[0]

        redeliver = AsyncMock(side_effect=RuntimeError("connection reset"))
        await _retry_one(_client(store), _sender(), 1024, item, redeliver, AsyncMock())

        rows = await store.pending()
        assert len(rows) == 1
        assert "connection reset" in rows[0].last_error


class TestSurvivesRestart:
    async def test_queued_message_is_still_there_for_a_fresh_process(self, tmp_path):
        """The point of putting this on disk: a message queued before a
        crash is picked up by the process that comes back."""
        path = str(tmp_path / "outbox.db")

        before = Outbox(path)
        await before.add(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 7, "caption": "", "elements": [],
            "media_specs": [{"kind": "voice", "file_id": "abc"}],
        })
        await before.close()  # process dies here

        after = Outbox(path)
        items = await after.pending()
        assert len(items) == 1
        assert items[0].direction == TG_TO_MAX_MEDIA
        assert items[0].payload["media_specs"][0]["file_id"] == "abc"

        redeliver_media = AsyncMock(return_value=True)
        await _retry_one(
            _client(after), _sender(), 1024, items[0], AsyncMock(), redeliver_media,
        )

        redeliver_media.assert_awaited_once()
        assert await after.pending() == []
        await after.close()

    async def test_an_in_flight_claim_does_not_survive_a_restart(self, tmp_path):
        """Claims are per-process state, deliberately: a message claimed
        by a process that then died must not stay blocked forever."""
        path = str(tmp_path / "outbox.db")

        before = Outbox(path)
        item_id = await before.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        before.try_start(item_id)  # "being delivered" when the process dies
        await before.close()

        after = Outbox(path)
        assert after.try_start(item_id) is True
        await after.close()


class TestSweepLoop:
    """The loop itself, driven one tick at a time."""

    async def _one_tick(self, client, sender, monkeypatch):
        monkeypatch.setattr(
            "app.outbox_retry.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        )
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_retry_loop(client, sender, 1024)

    async def test_sweep_delivers_a_queued_message(self, store, monkeypatch):
        import app.tg_handler as tg_handler

        await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        redeliver = AsyncMock(return_value=True)
        monkeypatch.setattr(tg_handler, "redeliver_tg_to_max_text", redeliver)

        await self._one_tick(_client(store), _sender(), monkeypatch)

        redeliver.assert_awaited_once()
        assert await store.pending() == []

    async def test_sweep_waits_out_the_backoff(self, store, monkeypatch):
        """A message that just failed must not be hammered on the next
        tick — the whole point of the growing delay."""
        import app.tg_handler as tg_handler

        item_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        await store.mark_failed(item_id, "boom")  # attempts=1 → wait 60s

        redeliver = AsyncMock(return_value=True)
        monkeypatch.setattr(tg_handler, "redeliver_tg_to_max_text", redeliver)

        await self._one_tick(_client(store), _sender(), monkeypatch)

        redeliver.assert_not_awaited()
        assert len(await store.pending()) == 1

    async def test_sweep_skips_a_message_that_is_still_being_delivered(
        self, store, monkeypatch
    ):
        """The live send holds the claim; the sweep must not start a
        second delivery of the same message alongside it."""
        import app.tg_handler as tg_handler

        item_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        store.try_start(item_id)  # a live send is in progress

        redeliver = AsyncMock(return_value=True)
        monkeypatch.setattr(tg_handler, "redeliver_tg_to_max_text", redeliver)

        await self._one_tick(_client(store), _sender(), monkeypatch)

        redeliver.assert_not_awaited()
        assert len(await store.pending()) == 1

    async def test_one_bad_item_does_not_stop_the_others(self, store, monkeypatch):
        """A single failing message must not block the queue behind it."""
        import app.tg_handler as tg_handler

        await store.add(TG_TO_MAX_TEXT, {**TEXT_PAYLOAD, "text": "first"})
        await store.add(TG_TO_MAX_TEXT, {**TEXT_PAYLOAD, "text": "second"})

        async def _redeliver(_client_, _bot, payload):
            if payload["text"] == "first":
                raise RuntimeError("this one is broken")
            return True

        monkeypatch.setattr(
            tg_handler, "redeliver_tg_to_max_text", AsyncMock(side_effect=_redeliver),
        )

        await self._one_tick(_client(store), _sender(), monkeypatch)

        remaining = await store.pending()
        assert [r.payload["text"] for r in remaining] == ["first"]


class TestPermanentRefusalIsNotRetriedForever:
    """MAX refusing the content itself (a voice recording it will never
    accept) is different from MAX being unreachable: re-uploading the same
    bytes can only earn the same refusal, so the row goes away instead of
    riding the queue forever. Everything else stays queued."""

    async def test_a_permanent_refusal_drops_the_row(self, store):
        await store.add(TG_TO_MAX_MEDIA, {
            "max_chat_id": 42, "tg_chat_id": -100, "thread_id": 10,
            "tg_message_id": 7, "caption": "", "elements": [],
            "media_specs": [{"kind": "voice", "file_id": "abc"}],
        })
        item = (await store.pending())[0]

        redeliver = AsyncMock(
            side_effect=PermanentDeliveryFailure("AUDIO_VALIDATION_FAILED"))
        await _retry_one(_client(store), _sender(), 1024, item, AsyncMock(), redeliver)

        assert await store.pending() == []

    async def test_an_ordinary_failure_still_stays_queued(self, store):
        """The other side of the same decision — only an outright refusal
        drops a message; a plain error keeps its place."""
        await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        item = (await store.pending())[0]

        redeliver = AsyncMock(side_effect=TimeoutError("MAX did not answer"))
        await _retry_one(_client(store), _sender(), 1024, item, redeliver, AsyncMock())

        assert len(await store.pending()) == 1


class TestCorruptRows:
    async def test_an_unreadable_payload_is_dropped_not_skipped_forever(self, store):
        """Nothing can be delivered from a payload that won't parse, so it
        is removed rather than re-read and re-logged on every sweep."""
        good_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)
        bad_id = await store.add(TG_TO_MAX_TEXT, TEXT_PAYLOAD)

        conn = await store._get_conn()
        await conn.execute(
            "UPDATE outbox SET payload = ? WHERE id = ?", ("{not json", bad_id))
        await conn.commit()

        items = await store.pending()
        assert [i.id for i in items] == [good_id]
        assert await store.count() == 1  # the bad row is gone, not just skipped
