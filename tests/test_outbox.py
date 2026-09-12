"""Tests for app/outbox.py — the persistent SQLite delivery queue."""

import pytest

from app.outbox import MAX_TO_TG, TG_TO_MAX_TEXT, Outbox


@pytest.fixture
async def ob():
    o = Outbox(":memory:")
    yield o
    await o.close()


class TestAddAndPending:
    async def test_add_returns_incrementing_ids(self, ob):
        id1 = await ob.add(MAX_TO_TG, {"a": 1})
        id2 = await ob.add(MAX_TO_TG, {"a": 2})
        assert id2 > id1

    async def test_pending_returns_payload_round_tripped(self, ob):
        await ob.add(TG_TO_MAX_TEXT, {"text": "hello", "chat_id": 42, "nested": {"x": [1, 2]}})
        items = await ob.pending()
        assert len(items) == 1
        assert items[0].payload == {"text": "hello", "chat_id": 42, "nested": {"x": [1, 2]}}
        assert items[0].direction == TG_TO_MAX_TEXT
        assert items[0].attempts == 0
        assert items[0].last_error is None

    async def test_pending_orders_by_id(self, ob):
        await ob.add(MAX_TO_TG, {"n": 1})
        await ob.add(MAX_TO_TG, {"n": 2})
        await ob.add(MAX_TO_TG, {"n": 3})
        items = await ob.pending()
        assert [i.payload["n"] for i in items] == [1, 2, 3]

    async def test_empty_outbox_has_no_pending_items(self, ob):
        assert await ob.pending() == []

    async def test_non_ascii_payload_round_trips(self, ob):
        await ob.add(MAX_TO_TG, {"text": "Привет, мир! 👋"})
        items = await ob.pending()
        assert items[0].payload["text"] == "Привет, мир! 👋"


class TestRemove:
    async def test_remove_deletes_the_item(self, ob):
        item_id = await ob.add(MAX_TO_TG, {"a": 1})
        await ob.remove(item_id)
        assert await ob.pending() == []

    async def test_remove_only_deletes_the_matching_item(self, ob):
        id1 = await ob.add(MAX_TO_TG, {"n": 1})
        id2 = await ob.add(MAX_TO_TG, {"n": 2})
        await ob.remove(id1)
        items = await ob.pending()
        assert [i.id for i in items] == [id2]

    async def test_remove_nonexistent_id_is_a_no_op(self, ob):
        await ob.remove(9999)  # must not raise


class TestMarkFailed:
    async def test_increments_attempts(self, ob):
        item_id = await ob.add(MAX_TO_TG, {"a": 1})
        await ob.mark_failed(item_id, "connection refused")
        await ob.mark_failed(item_id, "timeout")
        items = await ob.pending()
        assert items[0].attempts == 2
        assert items[0].last_error == "timeout"

    async def test_sets_last_attempt_at(self, ob):
        item_id = await ob.add(MAX_TO_TG, {"a": 1})
        assert (await ob.pending())[0].last_attempt_at is None
        await ob.mark_failed(item_id, "boom")
        assert (await ob.pending())[0].last_attempt_at is not None

    async def test_truncates_very_long_error_messages(self, ob):
        item_id = await ob.add(MAX_TO_TG, {"a": 1})
        await ob.mark_failed(item_id, "x" * 10_000)
        items = await ob.pending()
        assert len(items[0].last_error) <= 500


class TestCount:
    async def test_count_matches_pending_length(self, ob):
        await ob.add(MAX_TO_TG, {"a": 1})
        await ob.add(MAX_TO_TG, {"a": 2})
        assert await ob.count() == 2

    async def test_count_reflects_removal(self, ob):
        item_id = await ob.add(MAX_TO_TG, {"a": 1})
        await ob.remove(item_id)
        assert await ob.count() == 0


class TestInFlightTracking:
    async def test_try_start_succeeds_for_a_fresh_item(self, ob):
        assert ob.try_start(1) is True

    async def test_try_start_fails_while_already_claimed(self, ob):
        ob.try_start(1)
        assert ob.try_start(1) is False

    async def test_try_start_succeeds_again_after_finish(self, ob):
        ob.try_start(1)
        ob.finish(1)
        assert ob.try_start(1) is True

    async def test_finish_is_a_no_op_for_an_unclaimed_item(self, ob):
        ob.finish(9999)  # must not raise

    async def test_claims_are_independent_per_item(self, ob):
        ob.try_start(1)
        assert ob.try_start(2) is True


class TestPersistenceAcrossConnections:
    async def test_survives_reopening_the_same_file(self, tmp_path):
        path = str(tmp_path / "outbox.db")
        ob1 = Outbox(path)
        await ob1.add(MAX_TO_TG, {"durable": True})
        await ob1.close()

        ob2 = Outbox(path)
        items = await ob2.pending()
        assert len(items) == 1
        assert items[0].payload == {"durable": True}
        await ob2.close()
