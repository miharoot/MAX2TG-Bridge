"""Tests for app/max_listener.py — pure helper functions."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.pymax_client import MaxMessage
from app.max_listener import (
    _guess_media_kind,
    _human_size,
    _max_message_to_payload,
    _send_attach,
    _topic_title_for_message,
    _try_send_media_group,
)


# ---------------------------------------------------------------------------
# _human_size
# ---------------------------------------------------------------------------

class TestHumanSize:
    """Tests for the _human_size byte-formatter."""

    # Byte range (< 1024)
    def test_zero_bytes(self):
        assert _human_size(0) == "0 Б"

    def test_single_byte(self):
        assert _human_size(1) == "1 Б"

    def test_max_bytes(self):
        assert _human_size(1023) == "1023 Б"

    # Kilobyte range (1024 – 1024²-1)
    def test_exact_one_kb(self):
        assert _human_size(1024) == "1.0 КБ"

    def test_fractional_kb(self):
        assert _human_size(1536) == "1.5 КБ"

    def test_large_kb(self):
        assert _human_size(1023 * 1024) == "1023.0 КБ"

    # Megabyte range
    def test_exact_one_mb(self):
        assert _human_size(1024 ** 2) == "1.0 МБ"

    def test_fractional_mb(self):
        assert _human_size(int(2.5 * 1024 ** 2)) == "2.5 МБ"

    def test_large_mb(self):
        assert _human_size(500 * 1024 ** 2) == "500.0 МБ"

    # Gigabyte range
    def test_exact_one_gb(self):
        assert _human_size(1024 ** 3) == "1.0 ГБ"

    def test_fractional_gb(self):
        assert _human_size(int(1.5 * 1024 ** 3)) == "1.5 ГБ"

    # Terabyte range (overflow past ГБ loop)
    def test_terabyte(self):
        result = _human_size(1024 ** 4)
        assert "ТБ" in result

    def test_large_terabyte(self):
        result = _human_size(5 * 1024 ** 4)
        assert result.startswith("5")
        assert "ТБ" in result

    # Return type
    def test_returns_string(self):
        assert isinstance(_human_size(42), str)


# ---------------------------------------------------------------------------
# _guess_media_kind
# ---------------------------------------------------------------------------

class TestGuessMediaKind:
    """Tests for the filename-to-media-kind classifier."""

    # Photo extensions
    def test_jpg_is_photo(self):
        assert _guess_media_kind("image.jpg") == "photo"

    def test_jpeg_is_photo(self):
        assert _guess_media_kind("photo.jpeg") == "photo"

    def test_png_is_photo(self):
        assert _guess_media_kind("screenshot.png") == "photo"

    def test_gif_is_photo(self):
        assert _guess_media_kind("anim.gif") == "photo"

    def test_webp_is_photo(self):
        assert _guess_media_kind("sticker.webp") == "photo"

    def test_bmp_is_photo(self):
        assert _guess_media_kind("old.bmp") == "photo"

    # Video extensions
    def test_mp4_is_video(self):
        assert _guess_media_kind("clip.mp4") == "video"

    def test_mov_is_video(self):
        assert _guess_media_kind("recording.mov") == "video"

    def test_avi_is_video(self):
        assert _guess_media_kind("video.avi") == "video"

    def test_mkv_is_video(self):
        assert _guess_media_kind("movie.mkv") == "video"

    def test_webm_is_video(self):
        assert _guess_media_kind("stream.webm") == "video"

    # Document / unknown extensions
    def test_pdf_is_document(self):
        assert _guess_media_kind("report.pdf") == "document"

    def test_zip_is_document(self):
        assert _guess_media_kind("archive.zip") == "document"

    def test_docx_is_document(self):
        assert _guess_media_kind("contract.docx") == "document"

    def test_txt_is_document(self):
        assert _guess_media_kind("notes.txt") == "document"

    def test_no_extension_is_document(self):
        assert _guess_media_kind("README") == "document"

    def test_empty_string_is_document(self):
        assert _guess_media_kind("") == "document"

    # Case-insensitivity
    def test_uppercase_jpg_is_photo(self):
        assert _guess_media_kind("PHOTO.JPG") == "photo"

    def test_mixed_case_mp4_is_video(self):
        assert _guess_media_kind("Video.MP4") == "video"

    def test_mixed_case_png_is_photo(self):
        assert _guess_media_kind("Image.PNG") == "photo"

    # Paths with directories
    def test_full_path_jpg(self):
        assert _guess_media_kind("/tmp/uploads/img.jpg") == "photo"

    def test_full_path_mp4(self):
        assert _guess_media_kind("/home/user/videos/clip.mp4") == "video"

    # Extension appearing in the middle of filename should not trigger false match
    def test_mp4_in_name_not_extension_is_document(self):
        assert _guess_media_kind("mp4_notes.txt") == "document"


# ---------------------------------------------------------------------------
# _topic_title_for_message — the actual fix for "topic named after whoever
# added the bridge to a group, instead of the group's own name"
# ---------------------------------------------------------------------------

def _msg(chat_id=-100, sender_id=1):
    return MaxMessage(chat_id=chat_id, sender_id=sender_id, message_id="m1")


class TestTopicTitleForMessage:
    async def test_dm_uses_sender_name_and_never_force_renames(self):
        resolver = MagicMock()
        resolver.is_saved_messages.return_value = False   # an ordinary dialog
        title, force = await _topic_title_for_message(
            _msg(), resolver, raw_sender="Иван Петров", is_dm=True,
        )
        assert title == "Иван Петров"
        assert force is False
        resolver.resolve_chat.assert_not_called()

    async def test_group_with_known_title_from_snapshot(self):
        resolver = MagicMock()
        resolver.chat_name.return_value = "Рабочий чат"
        title, force = await _topic_title_for_message(
            _msg(), resolver, raw_sender="Иван Петров", is_dm=False,
        )
        assert title == "Рабочий чат"
        assert force is True
        resolver.resolve_chat.assert_not_called()

    async def test_brand_new_group_resolves_live_instead_of_using_sender_name(self):
        """This is the reported bug: a group the bridge's account was just
        added to isn't in the startup snapshot, so chat_name() falls back to
        the numeric chat ID — the old code then used the *sender's* name as
        the topic title. It must now do a live lookup instead."""
        resolver = MagicMock()
        resolver.chat_name.return_value = "-10000000000002"  # unknown → numeric fallback
        resolver.resolve_chat = AsyncMock(return_value="Дружная команда")

        title, force = await _topic_title_for_message(
            _msg(chat_id=-10000000000002), resolver,
            raw_sender="Иван Петров", is_dm=False,
        )

        assert title == "Дружная команда"
        assert title != "Иван Петров"
        assert force is True
        resolver.resolve_chat.assert_awaited_once_with(-10000000000002)

    async def test_unresolvable_new_group_falls_back_to_chat_id_not_sender(self):
        """If the live lookup also fails, fall back to the numeric chat ID
        — never to the sender's name, since that's the bug being fixed."""
        resolver = MagicMock()
        resolver.chat_name.return_value = "-555"
        resolver.resolve_chat = AsyncMock(return_value="-555")  # still unresolved

        title, force = await _topic_title_for_message(
            _msg(chat_id=-555), resolver, raw_sender="Иван Петров", is_dm=False,
        )

        assert title == "-555"
        assert title != "Иван Петров"
        assert force is False


# ---------------------------------------------------------------------------
# _send_attach — FILE / VIDEO edge cases against the PyMax client
# ---------------------------------------------------------------------------

class TestFileAttachment:
    @pytest.mark.asyncio
    async def test_resolves_file_id_and_sends_document(self):
        client = MagicMock()
        client.resolve_file_url = AsyncMock(return_value="https://i.oneme.ru/file.bin")
        client.download_file = AsyncMock(return_value=b"file contents")
        sender = MagicMock()
        sent_message = MagicMock(message_id=999)
        sender.send_document = AsyncMock(return_value=sent_message)
        sender.send = AsyncMock()
        msg = MaxMessage(chat_id=-78273486848085, message_id="message-1")

        result = await _send_attach(
            {"_type": "FILE", "name": "report.pdf", "fileId": 12345},
            client,
            sender,
            "header",
            thread_id=42,
            msg=msg,
        )

        # Success returns the actual sent Message (for ✅ read-receipt
        # tracking), not just a truthy bool.
        assert result is sent_message
        client.resolve_file_url.assert_awaited_once_with(
            -78273486848085, "message-1", 12345
        )
        client.download_file.assert_awaited_once_with("https://i.oneme.ru/file.bin")
        sender.send_document.assert_awaited_once_with(
            b"file contents",
            caption="header",
            filename="report.pdf",
            message_thread_id=42,
            chat_id=None,
        )
        sender.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_photo_preview_when_video_download_fails(self):
        client = MagicMock()
        client.download_video_url = AsyncMock(return_value=None)
        client.download_file = AsyncMock(return_value=b"preview bytes")
        sender = MagicMock()
        sender.send_video = AsyncMock()
        sent_message = MagicMock(message_id=999)
        sender.send_photo = AsyncMock(return_value=sent_message)
        sender.send = AsyncMock()
        msg = MaxMessage(chat_id=-78273486848085, message_id="message-1")

        result = await _send_attach(
            {"_type": "VIDEO", "videoId": 190714046, "thumbnail": "https://cdn/thumb.jpg"},
            client,
            sender,
            "header",
            thread_id=10,
            msg=msg,
        )

        assert result is sent_message
        sender.send_video.assert_not_awaited()
        sender.send_photo.assert_awaited_once_with(
            b"preview bytes",
            caption="header\n<i>[видео — превью, не удалось скачать полностью]</i>",
            message_thread_id=10,
            chat_id=None,
        )

    @pytest.mark.asyncio
    async def test_sends_video_marker_when_telegram_upload_fails(self):
        client = MagicMock()
        client.download_video_url = AsyncMock(return_value="https://vd.example/video.mp4")
        client.download_file = AsyncMock(return_value=b"video bytes")
        sender = MagicMock()
        sender.send_video = AsyncMock(return_value=False)
        sender.send = AsyncMock()
        msg = MaxMessage(chat_id=-78273486848085, message_id="message-1")

        result = await _send_attach(
            {"_type": "VIDEO", "videoId": 190714046},
            client,
            sender,
            "header",
            thread_id=10,
            msg=msg,
        )

        assert result is sender.send.return_value
        sender.send.assert_awaited_once_with(
            "header\n<i>[видео — не удалось загрузить]</i>",
            message_thread_id=10,
            chat_id=None,
        )


class TestAttachmentGrouping:
    @pytest.mark.asyncio
    async def test_groups_photos_and_videos_in_original_order(self):
        client = MagicMock()
        client.download_video_url = AsyncMock(return_value="https://cdn/video.mp4")
        client.download_file = AsyncMock(side_effect=[b"photo", b"video"])
        sender = MagicMock()
        sent_message = MagicMock(message_id=555)
        sender.send_media_group = AsyncMock(return_value=sent_message)
        msg = MaxMessage(chat_id=-10, message_id="77")

        grouped = await _try_send_media_group(
            [
                {"_type": "PHOTO", "baseUrl": "https://cdn/photo.jpg"},
                {"_type": "VIDEO", "videoId": 123},
            ],
            client,
            sender,
            "header",
            42,
            msg,
        )

        # Success returns the actual sent Message (for ✅ read-receipt
        # tracking), not just a bool.
        assert grouped is sent_message
        sender.send_media_group.assert_awaited_once_with(
            [
                ("photo", b"photo", "photo-1.jpg"),
                ("video", b"video", "123.mp4"),
            ],
            caption="header",
            message_thread_id=42,
            chat_id=None,
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_telegram_send_fails(self):
        """All downloads succeed, but Telegram itself rejects the album —
        distinguishable from 'not attempted' (None) so the caller can send
        a failure notice instead of silently falling back per-attachment."""
        client = MagicMock()
        client.download_video_url = AsyncMock(return_value="https://cdn/video.mp4")
        client.download_file = AsyncMock(side_effect=[b"photo", b"video"])
        sender = MagicMock()
        sender.send_media_group = AsyncMock(return_value=None)
        msg = MaxMessage(chat_id=-10, message_id="77")

        grouped = await _try_send_media_group(
            [
                {"_type": "PHOTO", "baseUrl": "https://cdn/photo.jpg"},
                {"_type": "VIDEO", "videoId": 123},
            ],
            client,
            sender,
            "header",
            42,
            msg,
        )

        assert grouped is False

    @pytest.mark.asyncio
    async def test_returns_none_when_a_download_fails(self):
        """Not attempted at all — caller should fall back to sending each
        attachment individually rather than showing a group failure."""
        client = MagicMock()
        client.download_video_url = AsyncMock(return_value=None)
        client.download_file = AsyncMock(return_value=b"photo")
        sender = MagicMock()
        sender.send_media_group = AsyncMock()
        msg = MaxMessage(chat_id=-10, message_id="77")

        grouped = await _try_send_media_group(
            [
                {"_type": "PHOTO", "baseUrl": "https://cdn/photo.jpg"},
                {"_type": "VIDEO", "videoId": 123},
            ],
            client,
            sender,
            "header",
            42,
            msg,
        )

        assert grouped is None
        sender.send_media_group.assert_not_awaited()


# ---------------------------------------------------------------------------
# _max_message_to_payload — regression test for a real production crash:
# dataclasses.asdict(msg) deep-copies everything (including msg.raw, a
# pydantic model_dump()) and blows up with "cannot pickle 'generator'
# object" if any stray non-deep-copyable value ever ends up in there
# (e.g. via _model_dict's vars()-fallback for an unresolved pydantic
# schema right after startup). A shallow copy sidesteps deepcopy
# entirely — outbox.add() will json.dumps(..., default=str) it, which
# degrades gracefully instead of crashing the inbound event dispatch.
# ---------------------------------------------------------------------------

class TestMaxMessageToPayload:
    def test_produces_same_keys_as_asdict_for_plain_data(self):
        from dataclasses import asdict
        msg = MaxMessage(
            chat_id=1, sender_id=2, text="hi", message_id="mid1",
            attaches=[{"a": 1}], link={"b": 2}, raw={"c": 3},
        )
        assert _max_message_to_payload(msg) == asdict(msg)

    def test_does_not_crash_on_a_non_deep_copyable_raw_value(self):
        """The exact failure mode seen in production: a generator ends
        up embedded in msg.raw (via _model_dict's fallback path) and
        dataclasses.asdict() dies trying to deepcopy it."""
        def _gen():
            yield 1

        msg = MaxMessage(
            chat_id=1, sender_id=2, text="hi", message_id="mid1",
            raw={"weird": _gen()},
        )

        payload = _max_message_to_payload(msg)

        assert payload["chat_id"] == 1
        assert "weird" in payload["raw"]

    def test_asdict_would_have_crashed_on_the_same_input(self):
        """Confirms the regression this guards against is real, not
        hypothetical — dataclasses.asdict() itself raises here."""
        from dataclasses import asdict

        def _gen():
            yield 1

        msg = MaxMessage(chat_id=1, raw={"weird": _gen()})

        with pytest.raises(TypeError, match="pickle"):
            asdict(msg)

    def test_result_is_json_serializable_with_default_str(self):
        """This is the actual contract that matters: whatever comes out
        must survive Outbox.add()'s json.dumps(payload, default=str)."""
        def _gen():
            yield 1

        msg = MaxMessage(
            chat_id=-100, sender_id=2, text="hi", message_id="mid1",
            attaches=[{"_type": "PHOTO"}], link={}, raw={"weird": _gen()},
        )

        payload = _max_message_to_payload(msg)
        serialized = json.dumps(payload, default=str, ensure_ascii=False)
        restored = json.loads(serialized)

        assert restored["chat_id"] == -100
        assert restored["message_id"] == "mid1"
        assert "generator object" in restored["raw"]["weird"]

    def test_result_round_trips_through_max_message_constructor(self):
        """This is what outbox_retry.py does on redelivery:
        MaxMessage(**item.payload) — the shallow dict must have exactly
        the right keys for that to work."""
        msg = MaxMessage(
            chat_id=-100, sender_id=2, text="hi", message_id="mid1",
            attaches=[{"_type": "PHOTO"}], link={}, raw={"ok": True},
        )

        payload = _max_message_to_payload(msg)
        rebuilt = MaxMessage(**payload)

        assert rebuilt == msg


class TestSavedMessagesTitle:
    """A chat with yourself would otherwise be titled with your own name,
    since in a dialog the sender is the topic's subject."""

    async def test_the_topic_is_called_saved_messages(self):
        from unittest.mock import MagicMock

        from app.max_listener import _topic_title_for_message

        resolver = MagicMock()
        resolver.is_saved_messages = MagicMock(return_value=True)
        msg = MagicMock()
        msg.chat_id = 0

        title, confirmed = await _topic_title_for_message(msg, resolver, "mihr", True)

        assert title == "Избранное"
        assert confirmed is True

    async def test_an_ordinary_dialog_still_uses_the_sender(self):
        from unittest.mock import MagicMock

        from app.max_listener import _topic_title_for_message

        resolver = MagicMock()
        resolver.is_saved_messages = MagicMock(return_value=False)
        msg = MagicMock()
        msg.chat_id = 100000001

        title, confirmed = await _topic_title_for_message(
            msg, resolver, "Иван Петров", True)

        assert title == "Иван Петров"
