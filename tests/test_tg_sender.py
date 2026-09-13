"""Tests for app/tg_sender.py — TelegramSender routing (resolve_chat_id, ensure_topic)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tg_sender import TelegramSender
from app.topics import TopicStore

DEFAULT = "-100999"


def _sender(tmp_path, chat_routes=None):
    store = TopicStore(str(tmp_path / "topics.json"))
    with patch("app.tg_sender.Bot"):
        sender = TelegramSender("dummy-token", DEFAULT, store, chat_routes=chat_routes)
    sender._bot = MagicMock()
    return sender, store


# ---------------------------------------------------------------------------
# resolve_chat_id
# ---------------------------------------------------------------------------

class TestResolveChatId:
    def test_falls_back_to_default_when_unrouted(self, tmp_path):
        sender, _ = _sender(tmp_path)
        assert sender.resolve_chat_id(42) == DEFAULT

    def test_uses_static_route_when_configured(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"42": -100111})
        assert sender.resolve_chat_id(42) == "-100111"

    def test_existing_topic_binding_wins_over_static_route(self, tmp_path):
        """Once a topic exists (e.g. via /bind into a different group), that
        binding is the source of truth — a static route must not override it."""
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        store.set_topic(42, -100222, 5, "Alice")
        assert sender.resolve_chat_id(42) == "-100222"

    def test_unrouted_chat_ignores_other_chats_routes(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"42": -100111})
        assert sender.resolve_chat_id(999) == DEFAULT


# ---------------------------------------------------------------------------
# ensure_topic
# ---------------------------------------------------------------------------

class TestEnsureTopic:
    async def test_creates_topic_in_default_group_when_unrouted(self, tmp_path):
        sender, store = _sender(tmp_path)
        topic = MagicMock()
        topic.message_thread_id = 7
        sender._bot.create_forum_topic = AsyncMock(return_value=topic)

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 7
        sender._bot.create_forum_topic.assert_called_once_with(
            chat_id=DEFAULT, name="Alice"
        )
        assert store.get_chat_id(42) == int(DEFAULT)

    async def test_creates_topic_in_routed_group(self, tmp_path):
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        topic = MagicMock()
        topic.message_thread_id = 7
        sender._bot.create_forum_topic = AsyncMock(return_value=topic)

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 7
        sender._bot.create_forum_topic.assert_called_once_with(
            chat_id="-100111", name="Alice"
        )
        assert store.get_chat_id(42) == -100111

    async def test_returns_existing_without_recreating(self, tmp_path):
        sender, store = _sender(tmp_path, chat_routes={"42": -100111})
        store.set_topic(42, -100111, 9, "Alice")
        sender._bot.create_forum_topic = AsyncMock()

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id == 9
        sender._bot.create_forum_topic.assert_not_called()

    async def test_returns_none_on_failure(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.create_forum_topic = AsyncMock(side_effect=RuntimeError("no admin rights"))

        thread_id = await sender.ensure_topic(42, "Alice")

        assert thread_id is None


# ---------------------------------------------------------------------------
# send_* methods accept an explicit chat_id
# ---------------------------------------------------------------------------

class TestSendChatIdOverride:
    async def test_send_uses_explicit_chat_id(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.send_message = AsyncMock()

        await sender.send("hi", message_thread_id=3, chat_id="-100777")

        _, kwargs = sender._bot.send_message.call_args
        assert kwargs["chat_id"] == "-100777"

    async def test_send_falls_back_to_default_chat_id(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.send_message = AsyncMock()

        await sender.send("hi")

        _, kwargs = sender._bot.send_message.call_args
        assert kwargs["chat_id"] == DEFAULT


# ---------------------------------------------------------------------------
# ensure_topic — force_rename (confirmed real title always wins)
# ---------------------------------------------------------------------------

class TestEnsureTopicForceRename:
    async def test_force_rename_overwrites_even_non_placeholder_title(self, tmp_path):
        """A confirmed live-fetched chat title must win even over a stored
        title that doesn't look like a placeholder — e.g. the group was
        renamed on MAX's side after the topic was first created."""
        sender, store = _sender(tmp_path)
        store.set_topic(42, int(DEFAULT), 9, "Old Group Name")
        sender._bot.edit_forum_topic = AsyncMock()

        thread_id = await sender.ensure_topic(42, "New Group Name", force_rename=True)

        assert thread_id == 9
        sender._bot.edit_forum_topic.assert_called_once()
        assert store.get_title(42) == "New Group Name"

    async def test_without_force_rename_non_placeholder_title_is_kept(self, tmp_path):
        sender, store = _sender(tmp_path)
        store.set_topic(42, int(DEFAULT), 9, "Old Group Name")
        sender._bot.edit_forum_topic = AsyncMock()

        thread_id = await sender.ensure_topic(42, "New Group Name", force_rename=False)

        assert thread_id == 9
        sender._bot.edit_forum_topic.assert_not_called()
        assert store.get_title(42) == "Old Group Name"

    async def test_placeholder_title_still_self_heals_without_force_rename(self, tmp_path):
        """Regression guard: the original numeric-placeholder self-heal
        behavior must keep working even when force_rename isn't passed."""
        sender, store = _sender(tmp_path)
        store.set_topic(42, int(DEFAULT), 9, "42")
        sender._bot.edit_forum_topic = AsyncMock()

        thread_id = await sender.ensure_topic(42, "Real Name")

        assert thread_id == 9
        sender._bot.edit_forum_topic.assert_called_once()
        assert store.get_title(42) == "Real Name"


# ---------------------------------------------------------------------------
# broadcast / all_known_chat_ids
# ---------------------------------------------------------------------------

class TestBroadcast:
    def test_all_known_chat_ids_includes_default(self, tmp_path):
        sender, _ = _sender(tmp_path)
        assert sender.all_known_chat_ids() == {int(DEFAULT)}

    def test_all_known_chat_ids_includes_static_routes(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"1": -100111, "2": -100222})
        assert sender.all_known_chat_ids() == {int(DEFAULT), -100111, -100222}

    def test_all_known_chat_ids_includes_bound_topic_groups(self, tmp_path):
        sender, store = _sender(tmp_path)
        store.set_topic(1, -100333, 5, "A")
        assert sender.all_known_chat_ids() == {int(DEFAULT), -100333}

    async def test_broadcast_sends_to_every_known_group_once(self, tmp_path):
        sender, store = _sender(tmp_path, chat_routes={"1": -100111})
        store.set_topic(2, -100222, 5, "A")
        sender._bot.send_message = AsyncMock()

        await sender.broadcast("hello")

        sent_chat_ids = {c.kwargs["chat_id"] for c in sender._bot.send_message.call_args_list}
        assert sent_chat_ids == {int(DEFAULT), -100111, -100222}

    async def test_broadcast_deduplicates_default_and_route(self, tmp_path):
        sender, _ = _sender(tmp_path, chat_routes={"1": int(DEFAULT)})
        sender._bot.send_message = AsyncMock()

        await sender.broadcast("hello")

        assert sender._bot.send_message.call_count == 1


# ---------------------------------------------------------------------------
# set_reaction — used to mirror MAX read events onto Telegram messages
# ---------------------------------------------------------------------------

class TestSetReaction:
    async def test_calls_bot_set_message_reaction(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.set_message_reaction = AsyncMock()

        ok = await sender.set_reaction(chat_id="-100999", message_id=42, emoji="✅")

        assert ok is True
        sender._bot.set_message_reaction.assert_awaited_once_with(
            chat_id="-100999", message_id=42, reaction="✅",
        )

    async def test_returns_false_on_failure_without_raising(self, tmp_path):
        sender, _ = _sender(tmp_path)
        sender._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("too old"))

        ok = await sender.set_reaction(chat_id="-100999", message_id=42, emoji="✅")

        assert ok is False


# ---------------------------------------------------------------------------
# TG_UPLOAD_MB — the ceiling on what we hand to the Bot API
# ---------------------------------------------------------------------------

def _capped_sender(tmp_path, limit_bytes):
    store = TopicStore(str(tmp_path / "topics.json"))
    with patch("app.tg_sender.Bot"):
        sender = TelegramSender("dummy-token", DEFAULT, store,
                                max_upload_bytes=limit_bytes)
    sender._bot = MagicMock()
    for name in ("send_photo", "send_document", "send_video", "send_voice",
                 "send_sticker", "send_media_group"):
        setattr(sender._bot, name, AsyncMock(return_value=MagicMock()))
    return sender


class TestUploadSizeLimit:
    """A MAX chat can carry something bigger than Telegram will accept, so
    the ceiling is its own setting (TG_UPLOAD_MB) rather than whatever
    MAX_DOWNLOAD_MB let us fetch. Over it, the send is refused before the
    request: callers read None as "couldn't send" and post a text fallback
    naming the attachment, so nothing disappears silently."""

    @pytest.mark.parametrize("method,args", [
        ("send_photo", (b"x" * 200,)),
        ("send_document", (b"x" * 200,)),
        ("send_video", (b"x" * 200,)),
        ("send_voice", (b"x" * 200,)),
        ("send_sticker", (b"x" * 200,)),
    ])
    async def test_oversized_upload_is_refused_before_the_request(
        self, tmp_path, method, args,
    ):
        sender = _capped_sender(tmp_path, 100)

        assert await getattr(sender, method)(*args) is None
        getattr(sender._bot, method).assert_not_awaited()

    async def test_an_upload_within_the_limit_still_goes_out(self, tmp_path):
        sender = _capped_sender(tmp_path, 100)

        assert await sender.send_photo(b"x" * 50) is not None
        sender._bot.send_photo.assert_awaited_once()

    async def test_no_limit_configured_means_no_check(self, tmp_path):
        sender = _capped_sender(tmp_path, None)

        assert await sender.send_photo(b"x" * 10_000_000) is not None
        sender._bot.send_photo.assert_awaited_once()

    async def test_an_album_drops_only_the_oversized_items(self, tmp_path):
        """One huge photo must not cost the album the rest of its items."""
        sender = _capped_sender(tmp_path, 100)
        sender._bot.send_media_group = AsyncMock(return_value=[MagicMock()])

        await sender.send_media_group([
            ("photo", b"x" * 50, "small.jpg"),
            ("photo", b"x" * 500, "huge.jpg"),
            ("photo", b"x" * 60, "also-small.jpg"),
        ])

        sender._bot.send_media_group.assert_awaited_once()
        sent_media = sender._bot.send_media_group.await_args.kwargs["media"]
        assert len(sent_media) == 2

    async def test_an_album_of_nothing_but_oversized_items_sends_nothing(self, tmp_path):
        sender = _capped_sender(tmp_path, 100)
        sender._bot.send_media_group = AsyncMock()

        assert await sender.send_media_group([("photo", b"x" * 500, "huge.jpg")]) is None
        sender._bot.send_media_group.assert_not_awaited()


class TestStartWaitsForTheNetwork:
    """The bridge talks to Telegram through a SOCKS5 proxy that can come
    up after the container does. An unguarded get_me() on startup killed
    the process outright — the log ended mid-line with no bridge left."""

    async def test_it_retries_until_telegram_answers(self, tmp_path):
        from telegram.error import NetworkError

        sender, _ = _sender(tmp_path)
        me = MagicMock()
        me.username = "bridge_bot"
        sender._bot.initialize = AsyncMock()
        sender._bot.get_me = AsyncMock(
            side_effect=[NetworkError("proxy refused"), NetworkError("proxy refused"), me])

        with patch("app.tg_sender.asyncio.sleep", AsyncMock()) as slept:
            await sender.start()

        assert sender._bot.get_me.await_count == 3
        assert slept.await_count == 2

    async def test_a_bad_token_is_not_waited_out(self, tmp_path):
        """No amount of retrying fixes it, and retrying forever would hide
        the one startup error worth reporting."""
        from telegram.error import InvalidToken

        sender, _ = _sender(tmp_path)
        sender._bot.initialize = AsyncMock()
        sender._bot.get_me = AsyncMock(side_effect=InvalidToken("nope"))

        with pytest.raises(InvalidToken):
            await sender.start()

    async def test_the_wait_between_attempts_is_capped(self, tmp_path):
        from app.tg_sender import (
            TG_START_RETRY_MAX,
            TG_START_RETRY_STEP,
            retry_until_reachable,
        )

        from telegram.error import NetworkError

        attempts = {"n": 0}

        async def _fails_ten_times():
            attempts["n"] += 1
            if attempts["n"] <= 10:
                raise NetworkError("still down")

        with patch("app.tg_sender.asyncio.sleep", AsyncMock()) as slept:
            await retry_until_reachable("test", _fails_ten_times)

        delays = [call.args[0] for call in slept.await_args_list]
        assert delays[0] == TG_START_RETRY_STEP      # 10s, then 20s, 30s…
        assert delays == sorted(delays)
        assert max(delays) == TG_START_RETRY_MAX
