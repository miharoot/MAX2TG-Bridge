"""Tests for app/topics.py — TopicStore persistence."""

import json
import os

from app.topics import TopicStore


def _path(tmp_path) -> str:
    return os.path.join(str(tmp_path), "topics.json")


TG = -100999  # a stand-in Telegram supergroup id used across most tests


class TestSetAndGet:
    def test_set_then_get_topic(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "Alice")
        assert store.get_topic(42) == 10

    def test_get_topic_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.get_topic(999) is None

    def test_get_chat_id(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "Alice")
        assert store.get_chat_id(42) == TG

    def test_get_chat_id_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.get_chat_id(999) is None

    def test_get_title(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "Alice")
        assert store.get_title(42) == "Alice"

    def test_chat_for_topic(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "Alice")
        assert store.chat_for_topic(TG, 10) == 42

    def test_chat_for_topic_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.chat_for_topic(TG, 123) is None

    def test_chat_for_topic_same_thread_id_different_groups(self, tmp_path):
        """Two different Telegram supergroups can each have a topic with the
        same numeric thread_id — they must not collide in the reverse map."""
        store = TopicStore(_path(tmp_path))
        group_a, group_b = -100111, -100222
        store.set_topic(42, group_a, 5, "Alice")
        store.set_topic(43, group_b, 5, "Bob")
        assert store.chat_for_topic(group_a, 5) == 42
        assert store.chat_for_topic(group_b, 5) == 43

    def test_update_title(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "12345")
        store.update_title(42, "Alice")
        assert store.get_title(42) == "Alice"

    def test_all_tg_chat_ids(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, -100111, 5, "Alice")
        store.set_topic(43, -100222, 6, "Bob")
        store.set_topic(44, -100111, 7, "Carol")
        assert store.all_tg_chat_ids() == {-100111, -100222}


class TestPersistence:
    def test_mapping_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, TG, 10, "Alice")
        store.set_topic(-100777, TG, 20, "Team")

        reloaded = TopicStore(path)
        assert reloaded.get_topic(42) == 10
        assert reloaded.get_topic(-100777) == 20

    def test_reverse_lookup_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, TG, 10, "Alice")

        reloaded = TopicStore(path)
        # numeric chat IDs round-trip back to int, not str
        assert reloaded.chat_for_topic(TG, 10) == 42

    def test_title_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, TG, 10, "Alice")

        reloaded = TopicStore(path)
        assert reloaded.get_title(42) == "Alice"

    def test_chat_id_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, -100555, 10, "Alice")

        reloaded = TopicStore(path)
        assert reloaded.get_chat_id(42) == -100555

    def test_file_is_valid_json(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, TG, 10, "Alice")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["chats"]["42"]["topic_id"] == 10
        assert data["chats"]["42"]["tg_chat_id"] == TG

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = _path(tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")

        store = TopicStore(path)
        assert store.get_topic(42) is None

    def test_remove_returns_freed_chat_and_topic(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, TG, 10, "Alice")
        assert store.remove(42) == (TG, 10)
        assert store.get_topic(42) is None
        assert store.chat_for_topic(TG, 10) is None

    def test_remove_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.remove(999) is None
