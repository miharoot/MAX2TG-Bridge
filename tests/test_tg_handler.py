"""Tests for app/tg_handler.py — topic-based reply routing."""

from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    TOPIC_STORE_KEY,
    _on_topic_message,
    build_tg_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_TG_CHAT_ID = -100999


def _make_topic_store(mapping: dict | None = None, tg_chat_id: int = DEFAULT_TG_CHAT_ID):
    """A TopicStore stand-in: chat_for_topic(tg_chat_id, thread_id) → max_chat_id.

    ``mapping`` keys by thread_id (as before); all entries are assumed to
    live in ``tg_chat_id`` unless the caller passes full (chat_id, thread_id)
    tuple keys instead.
    """
    mapping = mapping or {10: 42}

    def _lookup(cid, tid):
        if (cid, tid) in mapping:
            return mapping[(cid, tid)]
        if cid == tg_chat_id:
            return mapping.get(tid)
        return None

    store = MagicMock()
    store.chat_for_topic = MagicMock(side_effect=_lookup)
    return store


def _make_update(text="Hello", thread_id=10, is_topic_message=True, user_id=100,
                 tg_chat_id: int = DEFAULT_TG_CHAT_ID):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.is_topic_message = is_topic_message
    update.message.reply_text = AsyncMock()
    update.message.set_reaction = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = tg_chat_id
    return update


def _make_context(max_client=None, topic_store=None, allowed_user_id=None):
    ctx = MagicMock()
    bot_data = {ALLOWED_USER_KEY: frozenset({allowed_user_id}) if allowed_user_id else None}
    if max_client is not None:
        bot_data[MAX_CLIENT_KEY] = max_client
    if topic_store is not None:
        bot_data[TOPIC_STORE_KEY] = topic_store
    ctx.bot_data = bot_data
    return ctx


# ---------------------------------------------------------------------------
# _on_topic_message
# ---------------------------------------------------------------------------

class TestOnTopicMessage:
    async def test_routes_topic_text_to_max(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", elements=[])

    async def test_reacts_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.set_reaction.assert_called_once()

    async def test_ignores_general_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=None, is_topic_message=False)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_unknown_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=999)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_empty_text(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(text=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_respects_allowed_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(user_id=555)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_allows_matching_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update(user_id=100)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once()

    async def test_warns_when_max_client_missing(self):
        update = _make_update()
        ctx = _make_context(topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_send_failure(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=None)

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_exception(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# build_tg_app
# ---------------------------------------------------------------------------

class TestBuildTgApp:
    def test_wires_bot_data(self):
        max_client = MagicMock()
        topic_store = _make_topic_store()

        app = build_tg_app("123456:AAABBBCCC", max_client, "-100123456",
                            topic_store, allowed_user_ids={777})

        assert app.bot_data[MAX_CLIENT_KEY] is max_client
        assert app.bot_data[TOPIC_STORE_KEY] is topic_store
        assert app.bot_data[ALLOWED_USER_KEY] == frozenset({777})

    def test_wires_multiple_allowed_user_ids(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store(), allowed_user_ids={777, 888})

        assert app.bot_data[ALLOWED_USER_KEY] == frozenset({777, 888})

    def test_allowed_user_id_none_when_unset(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.bot_data[ALLOWED_USER_KEY] is None

    def test_registers_message_handler(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.handlers[0]
