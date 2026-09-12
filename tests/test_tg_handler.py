"""Tests for app/tg_handler.py — topic-based reply routing."""

import socket
from unittest.mock import AsyncMock, MagicMock

from telegram.error import TimedOut

from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    TOPIC_STORE_KEY,
    _download_tg_file,
    _media_spec_from_message,
    _on_topic_message,
    _send_topic_media_messages,
    _upload_media_by_spec,
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
    ctx.bot = AsyncMock()
    return ctx


def _make_max_client(last_message_ids=None, send_message_return=None,
                     send_message_side_effect=None, read_message_return=True):
    """A MagicMock max_client wired with an async .outbox (add/remove/
    mark_failed) — every _on_topic_message/_send_topic_media_messages call
    touches the outbox now, so every test needs one."""
    max_client = MagicMock()
    max_client.last_message_ids = last_message_ids if last_message_ids is not None else {}
    if send_message_side_effect is not None:
        max_client.send_message = AsyncMock(side_effect=send_message_side_effect)
    else:
        max_client.send_message = AsyncMock(return_value=send_message_return)
    max_client.read_message = AsyncMock(return_value=read_message_return)
    max_client.outbox = MagicMock()
    max_client.outbox.add = AsyncMock(return_value=1)
    max_client.outbox.remove = AsyncMock()
    max_client.outbox.mark_failed = AsyncMock()
    return max_client


# ---------------------------------------------------------------------------
# _on_topic_message
# ---------------------------------------------------------------------------

class TestOnTopicMessage:
    async def test_routes_topic_text_to_max(self):
        max_client = _make_max_client(send_message_return={"ok": True})

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", elements=[])

    async def test_reacts_on_success(self):
        max_client = _make_max_client(send_message_return={"ok": True})

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        ctx.bot.set_message_reaction.assert_called_once()

    async def test_removes_outbox_item_on_success(self):
        """The whole point of the outbox: a confirmed delivery must not
        stay queued for retry."""
        max_client = _make_max_client(send_message_return={"ok": True})

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.outbox.add.assert_awaited_once()
        max_client.outbox.remove.assert_awaited_once_with(1)
        max_client.outbox.mark_failed.assert_not_awaited()

    async def test_logs_warning_when_reaction_fails(self, caplog):
        """A failed 👀 reaction (e.g. missing Telegram permission) must be
        visible at warning level, not silently swallowed at debug."""
        max_client = _make_max_client(send_message_return={"ok": True})

        update = _make_update()
        update.message.chat_id = -100999
        update.message.message_id = 42
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())
        ctx.bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("Forbidden"))

        with caplog.at_level("WARNING", logger="app.tg_handler"):
            await _on_topic_message(update, ctx)

        assert any(
            r.levelname == "WARNING" and "reaction" in r.message.lower()
            for r in caplog.records
        )

    async def test_ignores_general_topic(self):
        max_client = _make_max_client()

        update = _make_update(thread_id=None, is_topic_message=False)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_unknown_topic(self):
        max_client = _make_max_client()

        update = _make_update(thread_id=999)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_empty_text(self):
        max_client = _make_max_client()

        update = _make_update(text=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_respects_allowed_user_id(self):
        max_client = _make_max_client()

        update = _make_update(user_id=555)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_allows_matching_user_id(self):
        max_client = _make_max_client(send_message_return={"ok": True})

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
        max_client = _make_max_client(send_message_return=None)

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_keeps_outbox_item_on_send_failure(self):
        """A failed/unconfirmed delivery must stay in the outbox for the
        retry loop to pick up — not get silently dropped."""
        max_client = _make_max_client(send_message_return=None)

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.outbox.remove.assert_not_awaited()
        max_client.outbox.mark_failed.assert_awaited_once()

    async def test_warns_on_exception(self):
        max_client = _make_max_client(send_message_side_effect=RuntimeError("boom"))

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_keeps_outbox_item_on_exception(self):
        max_client = _make_max_client(send_message_side_effect=RuntimeError("boom"))

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.outbox.remove.assert_not_awaited()
        max_client.outbox.mark_failed.assert_awaited_once()


# ---------------------------------------------------------------------------
# Read-receipt TG → MAX: replying in a topic marks the MAX chat as read up
# to the last message we saw from it. Telegram gives bots no signal for an
# actual "message read" event, so a reply is the best available proxy.
# ---------------------------------------------------------------------------

class TestReadReceiptOnReply:
    async def test_marks_chat_read_up_to_last_known_message_on_successful_reply(self):
        max_client = _make_max_client(
            last_message_ids={42: "max-msg-77"}, send_message_return={"ok": True},
        )

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.read_message.assert_awaited_once_with(42, "max-msg-77")

    async def test_does_not_mark_read_when_no_message_seen_yet(self):
        """A chat we've never received anything from has nothing to mark
        as read — must not call read_message with a bogus/None id."""
        max_client = _make_max_client(last_message_ids={}, send_message_return={"ok": True})

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.read_message.assert_not_awaited()

    async def test_does_not_mark_read_when_send_failed(self):
        """No point marking the chat read if our reply never actually
        went through to MAX."""
        max_client = _make_max_client(
            last_message_ids={42: "max-msg-77"}, send_message_return=None,
        )

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.read_message.assert_not_awaited()

    async def test_does_not_mark_read_on_max_error(self):
        max_client = _make_max_client(
            last_message_ids={42: "max-msg-77"},
            send_message_return={"_max_error": {"message": "rate limited"}},
        )

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.read_message.assert_not_awaited()

    async def test_read_message_failure_does_not_break_the_reply_flow(self):
        """If MAX rejects the read-mark call, the reply itself already
        succeeded and shouldn't be reported as failed to the user."""
        max_client = _make_max_client(
            last_message_ids={42: "max-msg-77"}, send_message_return={"ok": True},
            read_message_return=False,
        )

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        ctx.bot.set_message_reaction.assert_called_once()
        update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# build_tg_app
# ---------------------------------------------------------------------------

class TestBuildTgApp:
    def test_wires_bot_data(self):
        max_client = MagicMock()
        max_client.last_message_ids = {}
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

    def test_enables_tcp_keepalive_on_both_request_clients(self):
        """Regression test: TG_PROXY is often a mandatory SOCKS5 hop (see
        CLAUDE.md/README), and a proxy or intermediary NAT/firewall can
        silently drop an idle long-polling connection, surfacing later as
        httpx.RemoteProtocolError. TCP keepalive helps the OS notice a dead
        connection sooner on both the regular bot-API client and,
        especially, the get_updates (long-polling) client."""
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store(), proxy_url="socks5://127.0.0.1:1080")

        for request in app.bot._request:
            transport = request._client_kwargs["transport"]
            keepalive_opts = transport._pool._socket_options
            assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in keepalive_opts


class TestMediaGrouping:
    async def test_sends_album_as_one_max_message(self, monkeypatch):
        first = MagicMock()
        first.caption = "Album caption"
        first.caption_entities = []
        first.reply_text = AsyncMock()
        first.chat_id = -100999
        first.message_thread_id = 10
        first.message_id = 501
        second = MagicMock()
        second.caption = None
        second.caption_entities = []
        second.reply_text = AsyncMock()

        uploader = AsyncMock(side_effect=["attach-1", "attach-2"])
        monkeypatch.setattr("app.tg_handler._upload_media_by_spec", uploader)
        max_client = _make_max_client(send_message_return={"ok": True})
        bot = AsyncMock()

        await _send_topic_media_messages(
            [first, second], 42, max_client, 1024, bot
        )

        max_client.send_message.assert_awaited_once_with(
            42,
            text="Album caption",
            elements=[],
            attaches=["attach-1", "attach-2"],
        )
        bot.set_message_reaction.assert_awaited_once()
        max_client.outbox.remove.assert_awaited_once()

    async def test_keeps_outbox_item_when_all_uploads_fail(self, monkeypatch):
        first = MagicMock()
        first.caption = "Caption"
        first.caption_entities = []
        first.reply_text = AsyncMock()

        monkeypatch.setattr("app.tg_handler._upload_media_by_spec", AsyncMock(return_value=None))
        max_client = _make_max_client()
        bot = AsyncMock()

        await _send_topic_media_messages([first], 42, max_client, 1024, bot)

        max_client.send_message.assert_not_called()
        max_client.outbox.remove.assert_not_awaited()
        max_client.outbox.mark_failed.assert_awaited_once()


# ---------------------------------------------------------------------------
# Voice messages TG → MAX — end to end, without monkeypatching
# _upload_media_by_spec (unlike the album tests above), to actually
# exercise _media_spec_from_message + _upload_media_by_spec + the real
# PyMaxClient.upload_audio wiring together.
# ---------------------------------------------------------------------------

class TestVoiceMessageUpload:
    def test_media_spec_extracts_duration_in_milliseconds(self):
        message = MagicMock()
        message.photo = None
        message.voice = MagicMock(duration=5, file_id="voice123")
        message.audio = None
        message.document = None
        message.video = None

        spec = _media_spec_from_message(message)

        assert spec == {"kind": "voice", "file_id": "voice123", "duration_ms": 5000}

    def test_media_spec_handles_timedelta_duration(self):
        """Some PTB configurations report Voice.duration as a
        datetime.timedelta instead of a plain int of seconds."""
        from datetime import timedelta
        message = MagicMock()
        message.photo = None
        message.voice = MagicMock(duration=timedelta(seconds=5), file_id="voice123")
        message.audio = None
        message.document = None
        message.video = None

        spec = _media_spec_from_message(message)

        assert spec == {"kind": "voice", "file_id": "voice123", "duration_ms": 5000}

    def test_media_spec_handles_missing_duration(self):
        message = MagicMock()
        message.photo = None
        message.voice = MagicMock(duration=None, file_id="voice123")
        message.audio = None
        message.document = None
        message.video = None

        spec = _media_spec_from_message(message)

        assert spec == {"kind": "voice", "file_id": "voice123", "duration_ms": None}

    def test_media_spec_recognises_a_video_note(self):
        """Telegram's round video messages were falling through every
        branch, so they reached MAX as nothing at all — and, not being in
        the handler's filter either, without even a warning in the topic."""
        message = MagicMock()
        message.photo = None
        message.voice = None
        message.audio = None
        message.document = None
        message.video = None
        message.video_note = MagicMock(duration=5, file_id="note123")

        spec = _media_spec_from_message(message)

        assert spec == {
            "kind": "video_note", "file_id": "note123", "duration_ms": 5000,
        }

    def test_video_note_handler_filter_covers_it(self):
        """The filter is the other half: without VIDEO_NOTE in it the
        update never reaches _on_topic_media at all — which is why these
        vanished silently, with no warning posted to the topic."""
        from telegram import Update
        from telegram.ext import MessageHandler

        from app.tg_handler import _on_topic_media, build_tg_app

        app = build_tg_app(
            token="123:abc", max_client=MagicMock(), supergroup_id="-100",
            topic_store=MagicMock(),
        )
        media_handler = next(
            handler
            for group in app.handlers.values()
            for handler in group
            if isinstance(handler, MessageHandler) and handler.callback is _on_topic_media
        )

        message = MagicMock(spec=[
            "photo", "voice", "audio", "document", "video", "video_note",
            "text", "caption", "chat", "effective_attachment",
        ])
        message.photo = ()
        message.voice = None
        message.audio = None
        message.document = None
        message.video = None
        message.video_note = MagicMock()
        message.text = None
        message.caption = None
        message.chat = MagicMock(type="supergroup")
        update = MagicMock(spec=Update)
        update.effective_message = message
        update.channel_post = None
        update.edited_channel_post = None
        update.message = message

        assert media_handler.check_update(update)

    async def test_unsupported_attachment_says_so_instead_of_vanishing(self):
        """The safety net for the failure mode that hid missing
        video_note support: no route must ever mean silence."""
        from app.tg_handler import (
            ALLOWED_USER_KEY,
            MAX_CLIENT_KEY,
            TOPIC_STORE_KEY,
            _on_unsupported_attachment,
        )

        message = MagicMock()
        message.chat_id = -100999
        message.message_thread_id = 10
        message.is_topic_message = True
        message.reply_text = AsyncMock()
        message.effective_attachment = MagicMock()
        update = MagicMock()
        update.message = message
        update.effective_chat = MagicMock(id=-100999)
        update.effective_user = MagicMock(id=1)

        topic_store = MagicMock()
        topic_store.chat_for_topic = MagicMock(return_value=42)
        context = MagicMock()
        context.bot_data = {
            MAX_CLIENT_KEY: MagicMock(),
            TOPIC_STORE_KEY: topic_store,
            ALLOWED_USER_KEY: set(),
        }

        await _on_unsupported_attachment(update, context)

        message.reply_text.assert_awaited_once()
        assert "не умеет" in message.reply_text.await_args.args[0]

    async def test_upload_media_by_spec_sends_a_video_note_as_a_video_note(self):
        """MAX has its own round-video type, so these should not be
        flattened into a plain video."""
        spec = {"kind": "video_note", "file_id": "note123", "duration_ms": 5000}

        tg_file = MagicMock()
        tg_file.file_size = 1000
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"mp4-bytes"))
        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=tg_file)

        max_client = MagicMock()
        sent_attach = MagicMock()
        max_client.upload_video_note = AsyncMock(return_value=sent_attach)

        attach = await _upload_media_by_spec(bot, spec, max_client, 42, 10 * 1024 * 1024)

        bot.get_file.assert_awaited_once_with("note123")
        max_client.upload_video_note.assert_awaited_once_with(
            b"mp4-bytes", chat_id=42, filename="video_note.mp4", duration=5000,
        )
        assert attach is sent_attach

    async def test_upload_media_by_spec_downloads_and_calls_upload_audio(self):
        spec = {"kind": "voice", "file_id": "voice123", "duration_ms": 5000}

        tg_file = MagicMock()
        tg_file.file_size = 1000
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg-bytes"))
        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=tg_file)

        max_client = MagicMock()
        sent_voice_attach = MagicMock()
        max_client.upload_audio = AsyncMock(return_value=sent_voice_attach)

        attach = await _upload_media_by_spec(bot, spec, max_client, 42, 10 * 1024 * 1024)

        bot.get_file.assert_awaited_once_with("voice123")
        max_client.upload_audio.assert_awaited_once_with(
            b"ogg-bytes", chat_id=42, filename="voice.ogg",
            mimetype="audio/ogg", duration=5000,
        )
        assert attach is sent_voice_attach

    async def test_full_voice_message_reaches_max_send_message(self):
        """End-to-end: a Telegram voice message, through the real (not
        monkeypatched) _media_spec_from_message + _upload_media_by_spec,
        should end up as one attachment in max_client.send_message —
        this is the exact path a real voice-note reply exercises."""
        message = MagicMock()
        message.photo = None
        message.voice = MagicMock(duration=5, file_id="voice123")
        message.audio = None
        message.document = None
        message.video = None
        message.caption = None
        message.caption_entities = []
        message.reply_text = AsyncMock()
        message.chat_id = -100999
        message.message_thread_id = 10
        message.message_id = 501

        tg_file = MagicMock()
        tg_file.file_size = 1000
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg-bytes"))
        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=tg_file)

        sent_voice_attach = MagicMock()
        max_client = _make_max_client(send_message_return={"ok": True})
        max_client.upload_audio = AsyncMock(return_value=sent_voice_attach)

        await _send_topic_media_messages([message], 42, max_client, 10 * 1024 * 1024, bot)

        max_client.upload_audio.assert_awaited_once_with(
            b"ogg-bytes", chat_id=42, filename="voice.ogg",
            mimetype="audio/ogg", duration=5000,
        )
        max_client.send_message.assert_awaited_once_with(
            42, text="", elements=[], attaches=[sent_voice_attach],
        )
        max_client.outbox.remove.assert_awaited_once()
        message.reply_text.assert_not_called()  # no "не удалось" warning

    async def test_full_document_reaches_max_send_message(self):
        """Same end-to-end path for a plain file: spec -> download ->
        upload_file -> one attachment in send_message. Telegram also sets
        `document` for GIFs, so this is the route animations take too."""
        message = MagicMock()
        message.photo = None
        message.voice = None
        message.audio = None
        message.document = MagicMock(
            file_id="doc123", file_name="report.pdf", mime_type="application/pdf",
        )
        message.video = None
        message.video_note = None
        message.caption = "смотри"
        message.caption_entities = []
        message.reply_text = AsyncMock()
        message.chat_id = -100999
        message.message_thread_id = 10
        message.message_id = 502

        tg_file = MagicMock()
        tg_file.file_size = 2000
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"pdf-bytes"))
        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=tg_file)

        sent_file_attach = MagicMock()
        max_client = _make_max_client(send_message_return={"ok": True})
        max_client.upload_file = AsyncMock(return_value=sent_file_attach)

        await _send_topic_media_messages([message], 42, max_client, 10 * 1024 * 1024, bot)

        max_client.upload_file.assert_awaited_once_with(
            b"pdf-bytes", chat_id=42, filename="report.pdf",
            mimetype="application/pdf",
        )
        max_client.send_message.assert_awaited_once_with(
            42, text="смотри", elements=[], attaches=[sent_file_attach],
        )
        max_client.outbox.remove.assert_awaited_once()
        message.reply_text.assert_not_called()

    async def test_document_without_a_filename_still_uploads(self):
        """Telegram omits file_name for some documents; the upload must
        not break on it."""
        spec = {"kind": "document", "file_id": "doc123",
                "filename": None, "mimetype": None}

        tg_file = MagicMock()
        tg_file.file_size = 10
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"x"))
        bot = AsyncMock()
        bot.get_file = AsyncMock(return_value=tg_file)

        max_client = MagicMock()
        max_client.upload_file = AsyncMock(return_value=MagicMock())

        await _upload_media_by_spec(bot, spec, max_client, 42, 10 * 1024 * 1024)

        max_client.upload_file.assert_awaited_once_with(
            b"x", chat_id=42, filename="file",
            mimetype="application/octet-stream",
        )


# ---------------------------------------------------------------------------
# _download_tg_file
# ---------------------------------------------------------------------------

class TestDownloadTgFile:
    async def test_refuses_declared_oversized_file(self):
        file_obj = MagicMock()
        file_obj.file_size = 101
        file_obj.get_file = AsyncMock()

        data = await _download_tg_file(file_obj, max_bytes=100)

        assert data is None
        file_obj.get_file.assert_not_called()

    async def test_refuses_downloaded_oversized_file(self):
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"abcdef"))
        file_obj = MagicMock()
        file_obj.file_size = None
        file_obj.get_file = AsyncMock(return_value=tg_file)

        data = await _download_tg_file(file_obj, max_bytes=5)

        assert data is None

    async def test_retries_telegram_timeout(self, monkeypatch):
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ok"))
        file_obj = MagicMock()
        file_obj.file_size = 2
        file_obj.get_file = AsyncMock(side_effect=[TimedOut(), tg_file])
        monkeypatch.setattr("app.tg_handler.asyncio.sleep", AsyncMock())

        data = await _download_tg_file(file_obj, max_bytes=10)

        assert data == b"ok"
        assert file_obj.get_file.await_count == 2
