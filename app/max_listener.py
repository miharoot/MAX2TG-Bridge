import asyncio
import logging
import os
from dataclasses import fields as _dataclass_fields
from datetime import datetime
from html import escape
from typing import Any

from app import outbox
from app.config import Settings
from app.outbox import Outbox
from app.pymax_client import MaxMessage, MaxReactionEvent, MaxReadEvent, PyMaxClient
from app.resolver import SAVED_MESSAGES_TITLE, ContactResolver
from app.tg_sender import TelegramSender

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _max_message_to_payload(msg: MaxMessage) -> dict:
    """Like dataclasses.asdict(msg), but shallow.

    asdict() recursively copy.deepcopy()'s every nested value, all the
    way down. msg.raw/attaches/link are already plain dict/list (built by
    _model_dict in pymax_client.py) and about to be json.dumps(...,
    default=str)'d by Outbox.add() anyway, so a deep copy buys nothing —
    it only adds a way to crash before we ever reach json.dumps's
    graceful degradation. That crash is exactly what happened in
    production: pymax occasionally hits an unresolved-schema edge case
    right after startup (see the MockValSer handling in _model_dict) and
    falls back to grabbing a pydantic model's raw internal attributes,
    one of which turned out to be a generator — copy.deepcopy has no
    idea how to copy a generator and raises immediately, taking down the
    whole inbound-event dispatch (not just this one message). A shallow
    copy sidesteps deepcopy entirely, so worst case json.dumps just
    stringifies whatever the odd value was.
    """
    return {f.name: getattr(msg, f.name) for f in _dataclass_fields(msg)}


ATTACHMENT_LABELS = {
    "PHOTO": "📷 <i>[фото — не удалось загрузить]</i>",
    "VIDEO": "🎬 <i>[видео — не удалось загрузить]</i>",
    "FILE": "📎 <i>[файл — не удалось загрузить]</i>",
    "AUDIO": "🎵 <i>[аудио — не удалось загрузить]</i>",
    "STICKER": "🏷 <i>[стикер — не удалось загрузить]</i>",
    "LOCATION": "📍 <i>[геолокация — не удалось загрузить]</i>",
    "CONTACT": "👤 <i>[контакт — не удалось загрузить]</i>",
    "SHARE": "🔗 <i>[ссылка — не удалось загрузить]</i>",
}


def _attachment_failure(attach: dict) -> str:
    atype = attach.get("_type", "")
    if atype == "UNSUPPORTED" and attach.get("audioId") is not None:
        return "🎙 <i>[голосовое сообщение — не удалось загрузить]</i>"
    return ATTACHMENT_LABELS.get(
        atype,
        f"📎 <i>[вложение {escape(atype or 'неизвестного типа')} — не удалось загрузить]</i>",
    )


def _header(sender_label: str, chat_label: str, is_dm: bool,
            is_manual_self: bool = False) -> str:
    if is_manual_self:
        # Typed on your own phone/other MAX client, not through the bridge.
        return "📱 <b>Вы (с телефона)</b>" if is_dm else f"📱 <b>Вы (с телефона)</b> в {chat_label}"
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    """Extract the best available URL for a PHOTO attachment."""
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    """Extract download URL for a FILE attachment (url field takes priority)."""
    url = attach.get("url")
    if url and url.startswith("http"):
        return url
    return None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


async def _try_send_media_group(
    attaches: list[dict],
    client: PyMaxClient,
    sender: TelegramSender,
    caption: str,
    thread_id: int | None,
    msg: MaxMessage,
    target_chat_id: str | int | None = None,
):
    """Group compatible MAX attachments into one Telegram album.

    Returns the sent Telegram ``Message`` on success (so callers can track
    it the same way as any other forwarded message, for ✅ read-receipt
    mirroring), ``False`` if we had everything downloaded but Telegram
    itself failed to send the album, or ``None`` if grouping wasn't
    attempted at all (fewer than 2 attaches, unsupported mix of types, or
    a download failed) — callers fall back to sending attachments one by
    one in that case.
    """
    if len(attaches) < 2:
        return None

    types = {attach.get("_type") for attach in attaches}
    if types <= {"PHOTO", "VIDEO"}:
        items: list[tuple[str, bytes, str]] = []
        for index, attach in enumerate(attaches):
            if attach.get("_type") == "PHOTO":
                url = _extract_photo_url(attach)
                data = await client.download_file(url) if url else None
                if not data:
                    return None
                items.append(("photo", data, f"photo-{index + 1}.jpg"))
            else:
                video_id = attach.get("videoId")
                if video_id is None:
                    return None
                url = await client.download_video_url(
                    video_id, chat_id=msg.chat_id, message_id=msg.message_id
                )
                data = await client.download_file(url) if url else None
                if not data:
                    return None
                items.append(("video", data, f"{video_id}.mp4"))
        sent = await sender.send_media_group(
            items, caption=caption, message_thread_id=thread_id, chat_id=target_chat_id
        )
        return sent if sent is not None else False

    if types == {"FILE"}:
        items = []
        for attach in attaches:
            name = attach.get("name") or "file"
            url = _extract_file_url(attach)
            if not url and attach.get("fileId") is not None:
                url = await client.resolve_file_url(
                    msg.chat_id, msg.message_id, attach["fileId"]
                )
            data = await client.download_file(url) if url else None
            if not data:
                return None
            items.append(("document", data, name))
        sent = await sender.send_media_group(
            items, caption=caption, message_thread_id=thread_id, chat_id=target_chat_id
        )
        return sent if sent is not None else False

    return None


async def _resolve_file_download_url(
    attach: dict, client: PyMaxClient, msg: MaxMessage | None,
) -> str | None:
    """FILE attaches sometimes arrive with only a ``fileId`` and no direct
    ``url`` — resolve it via PyMax's native file-URL resolver, same as we
    already do for audio/video."""
    direct = _extract_file_url(attach)
    if direct:
        return direct
    file_id = attach.get("fileId")
    if file_id is None or msg is None or not msg.message_id:
        return None
    return await client.resolve_file_url(msg.chat_id, msg.message_id, file_id)


async def _send_attach(
    attach: dict,
    client: PyMaxClient,
    sender: TelegramSender,
    header_text: str,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
    chat_id: str | int | None = None,
):
    """Process and send a single attachment. Returns the sent Telegram
    Message (for read-receipt/reaction tracking), or None if unhandled."""
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype == "CONTROL" or atype == "WIDGET" or atype == "INLINE_KEYBOARD":
        return None

    # MAX's newer client sends voice messages with `_type=UNSUPPORTED` plus an
    # `audioId` + `token` (our ver=11 client doesn't speak its native AUDIO
    # variant). Treat this shape as audio.
    if atype == "UNSUPPORTED" and attach.get("audioId") is not None:
        audio_id = attach.get("audioId")
        token = attach.get("token")
        duration = attach.get("duration", 0)
        msg_chat_id = msg.chat_id if msg else None
        message_id = msg.message_id if msg else None
        url = None
        if msg_chat_id is not None and message_id:
            url = await client.download_audio_url(
                audio_id, msg_chat_id, message_id, token=token,
            )
        if url:
            data = await client.download_file(url)
            if data:
                sent = await sender.send_voice(data, caption=header_text,
                                                message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent
        dur_s = f" ({duration // 1000}с)" if duration else ""
        return await sender.send(
            f"{header_text}\n🎙 <i>[голосовое сообщение{dur_s} — не удалось скачать]</i>",
            message_thread_id=thread_id, chat_id=chat_id,
        )

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL: %s", attach)
            return await sender.send(
                f"{header_text}\n{_attachment_failure(attach)}",
                message_thread_id=thread_id, chat_id=chat_id,
            )
        data = await client.download_file(url)
        if data:
            sent = await sender.send_photo(data, caption=header_text, message_thread_id=thread_id, chat_id=chat_id)
            if sent:
                return sent
        return await sender.send(f"{header_text}\n<i>[фото — не удалось загрузить]</i>", message_thread_id=thread_id, chat_id=chat_id)

    if atype == "VIDEO":
        # Resolve the actual playable MP4 (not just a thumbnail) via the
        # dedicated VIDEO_DOWNLOAD_URL opcode when we have enough context.
        video_id = attach.get("videoId") or attach.get("video_id")
        video_url = None
        if video_id is not None and msg is not None and msg.message_id:
            video_url = await client.download_video_url(
                video_id, msg.chat_id, msg.message_id,
            )
        elif video_id is not None:
            log.warning(
                "Cannot resolve VIDEO without chat/message context: videoId=%s",
                video_id,
            )
        if video_url:
            data = await client.download_file(video_url)
            if data:
                sent = await sender.send_video(data, caption=header_text, filename=f"{video_id}.mp4",
                                                message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent

        thumb = attach.get("thumbnail")
        if thumb:
            data = await client.download_file(thumb)
            if data:
                sent = await sender.send_photo(data, caption=f"{header_text}\n<i>[видео — превью, не удалось скачать полностью]</i>",
                                                message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent
        return await sender.send(f"{header_text}\n<i>[видео — не удалось загрузить]</i>", message_thread_id=thread_id, chat_id=chat_id)

    if atype == "FILE":
        name = attach.get("name", "file")
        size = attach.get("size", 0)
        file_id = attach.get("fileId")
        token_url = await _resolve_file_download_url(attach, client, msg)
        if token_url:
            data = await client.download_file(token_url)
            if data:
                kind = _guess_media_kind(name)
                if kind == "photo":
                    sent = await sender.send_photo(data, caption=header_text, filename=name, message_thread_id=thread_id, chat_id=chat_id)
                elif kind == "video":
                    sent = await sender.send_video(data, caption=header_text, filename=name, message_thread_id=thread_id, chat_id=chat_id)
                else:
                    sent = await sender.send_document(data, caption=header_text, filename=name, message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent
        log.warning("FILE content unavailable; sending metadata only: fileId=%s name=%r", file_id, name)
        size_str = f" ({_human_size(size)})" if size else ""
        return await sender.send(
            f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}\n"
            "<i>[файл — не удалось загрузить]</i>",
            message_thread_id=thread_id, chat_id=chat_id,
        )

    if atype == "AUDIO":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                sent = await sender.send_voice(data, caption=header_text, message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent
        return await sender.send(f"{header_text}\n{_attachment_failure(attach)}", message_thread_id=thread_id, chat_id=chat_id)

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                sent = await sender.send_sticker(data, message_thread_id=thread_id, chat_id=chat_id)
                if sent:
                    return sent
        return await sender.send(f"{header_text}\n{_attachment_failure(attach)}", message_thread_id=thread_id, chat_id=chat_id)

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        return await sender.send("\n".join(parts), message_thread_id=thread_id, chat_id=chat_id)

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            return await sender.send(f"{header_text}\n📍 {lat}, {lon}", message_thread_id=thread_id, chat_id=chat_id)
        else:
            await sender.send(f"{header_text}\n<i>[геолокация]</i>", message_thread_id=thread_id, chat_id=chat_id)

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        return await sender.send(text, message_thread_id=thread_id, chat_id=chat_id)

    log.info("Unknown attach type %s, sending as info", atype)
    return await sender.send(f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>", message_thread_id=thread_id, chat_id=chat_id)


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: PyMaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
    chat_id: str | int | None = None,
):
    """Handle FORWARD or REPLY link inside a message. Returns the last sent
    Telegram Message (for read-receipt/reaction tracking), or None."""
    inner = link.get("message") or link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []

    fwd_sender_label = ""
    if fwd_sender_id:
        fwd_sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if fwd_sender_label:
            prefix = f"↩️ <b>Переслано от {fwd_sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if fwd_sender_label:
            prefix = f"↩ <b>Ответ на {fwd_sender_label}</b>"

    full_header = f"{header_text}\n{prefix}"

    fwd_meaningful = [
        a for a in fwd_attaches
        if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
    ]

    last_message = None
    if fwd_meaningful:
        group_caption = full_header
        if fwd_text:
            group_caption = f"{full_header}\n{escape(fwd_text)}"
        try:
            grouped = await _try_send_media_group(
                fwd_meaningful, client, sender, group_caption, thread_id, msg, chat_id
            ) if msg else None
        except Exception:
            log.exception("Failed to prepare linked attachment group")
            grouped = None
        if grouped is not None and grouped is not False:
            return grouped
        if grouped is False:
            return await sender.send(
                f"{group_caption}\n<i>[группа вложений — не удалось загрузить]</i>",
                message_thread_id=thread_id, chat_id=chat_id,
            )

        text_sent = False
        for i, attach in enumerate(fwd_meaningful):
            if i == 0 and fwd_text:
                cap = f"{full_header}\n{escape(fwd_text)}"
                text_sent = True
            else:
                cap = full_header
            try:
                result = await _send_attach(attach, client, sender, cap, thread_id=thread_id, msg=msg, chat_id=chat_id)
                if result is not None:
                    last_message = result
            except Exception:
                log.exception(
                    "Failed to forward linked attach _type=%s",
                    attach.get("_type"),
                )
                await sender.send(
                    f"{cap}\n{_attachment_failure(attach)}",
                    message_thread_id=thread_id, chat_id=chat_id,
                )

        if fwd_text and not text_sent:
            last_message = await sender.send(f"{full_header}\n{escape(fwd_text)}", message_thread_id=thread_id, chat_id=chat_id)
    elif fwd_text:
        last_message = await sender.send(f"{full_header}\n{escape(fwd_text)}", message_thread_id=thread_id, chat_id=chat_id)
    else:
        last_message = await sender.send(f"{full_header}\n<i>[без содержимого]</i>", message_thread_id=thread_id, chat_id=chat_id)

    return last_message


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


async def _topic_title_for_message(msg: MaxMessage, resolver: ContactResolver,
                                    raw_sender: str, is_dm: bool) -> tuple[str, bool]:
    """Decide the forum-topic title for a Max chat, and whether it's now a
    *confirmed* real title that should force-overwrite a previously wrong one.

    This is what fixes topics being created with the name of whoever added
    the bridge's account to a group, instead of the group's own name: a
    chat that isn't in the startup snapshot yet used to fall back straight
    to the sender's name. Now, for anything that isn't a DM, we first try a
    live MAX lookup (``resolver.resolve_chat``) for the real chat title
    before ever considering the sender as a fallback.
    """
    if is_dm and resolver.is_saved_messages(msg.chat_id):
        # A chat with yourself: the sender's name is your own, which says
        # nothing about what the topic is.
        return SAVED_MESSAGES_TITLE, True

    if is_dm:
        return raw_sender, False

    raw_chat = resolver.chat_name(msg.chat_id)
    chat_title_known = raw_chat != str(msg.chat_id) and not raw_chat.startswith("DM:")
    if chat_title_known:
        return raw_chat, True

    # Not in the snapshot (brand-new group/channel) — ask MAX directly.
    fetched = await resolver.resolve_chat(msg.chat_id)
    fetched_known = fetched != str(msg.chat_id) and not fetched.startswith("DM:")
    if fetched_known:
        return fetched, True

    # Genuinely unresolvable (fetch failed) — fall back to the numeric chat
    # ID rather than the sender's name, since that was the actual bug: a
    # topic named after the adder is worse than one named by chat ID (the
    # latter self-heals via force_rename on the next message once MAX
    # answers the lookup, since resolver caches failures but a later
    # snapshot/refresh will still populate the real title).
    return str(msg.chat_id), False



async def _control_event_line(msg, resolver) -> str | None:
    """Render MAX's service events — someone added, removed, left, the
    chat renamed — instead of calling them "нетекстовое сообщение".

    They arrive as a CONTROL attachment: ``{"_type": "CONTROL", "event":
    "add", "userIds": [...], "title": ...}``. The event names come from
    live traffic, so an unfamiliar one is shown as itself rather than
    swallowed — that's how the next one gets recognised.
    """
    control = next(
        (a for a in (msg.attaches or [])
         if isinstance(a, dict) and a.get("_type") == "CONTROL"),
        None,
    )
    if control is None:
        return None

    event = str(control.get("event") or "").lower()
    title = control.get("title")

    names = []
    for user_id in (control.get("userIds") or control.get("userids") or []):
        try:
            names.append(await resolver.resolve_user(user_id))
        except Exception:                              # noqa: BLE001
            names.append(str(user_id))
    who = ", ".join(escape(str(name)) for name in names)

    if event == "add":
        return f"➕ добавил(а) в чат: {who}" if who else "➕ добавил(а) участника"
    if event in ("remove", "kick"):
        return f"➖ убрал(а) из чата: {who}" if who else "➖ убрал(а) участника"
    if event == "leave":
        return "🚪 вышел(а) из чата"
    if event == "new":
        return (f"🆕 создал(а) чат «{escape(str(title))}»" if title
                else "🆕 создал(а) чат")
    if event in ("title", "rename"):
        return (f"✏️ переименовал(а) чат: «{escape(str(title))}»" if title
                else "✏️ переименовал(а) чат")
    if event == "pin":
        return "📌 закрепил(а) сообщение"
    return f"ℹ️ служебное событие MAX: {escape(event or '?')}"


def create_pymax_client(settings: Settings, sender: TelegramSender) -> PyMaxClient:
    client = PyMaxClient(settings)
    return configure_pymax_client(client, sender)


def configure_pymax_client(client: PyMaxClient, sender: TelegramSender):
    resolver = ContactResolver(client=client)
    # Expose for tg_handler commands like /profile.
    client.resolver = resolver

    # Shared by both directions (see app/outbox.py); tg_handler.py reaches
    # it via max_client.outbox the same way it already reaches .resolver.
    _settings = getattr(client, "settings", None)
    state_dir = getattr(_settings, "state_dir", None) or "state"
    client.outbox = Outbox(os.path.join(state_dir, "outbox.db"))

    # How many recent messages a freshly bound topic pulls in (0 = off).
    # Read off the client so tg_handler's /bind and /add don't need the
    # whole Settings object plumbed through build_tg_app.
    client.backfill_limit = (
        getattr(_settings, "backfill_limit", 0)
        if getattr(_settings, "backfill_enabled", False) else 0
    )

    _first_connect = True
    # Reconnect ("восстановлено") should only ever follow a disconnect
    # notice the user actually saw — otherwise pymax's internal
    # reconnects (or a throttled/suppressed disconnect) make "✅
    # восстановлено" show up with no matching "⚠️ потеряно" before it.
    _last_disconnect_notif_time: datetime | None = None
    _disconnect_notice_pending = False
    # The most recent Telegram message in each Max chat's topic:
    # max_chat_id -> (tg_chat_id, tg_message_id). Used to mirror MAX "read"
    # events as a ✅ reaction on that message (see @client.on_read below) —
    # same idea as the existing 👀 reaction already put on Telegram→MAX
    # replies once MAX confirms delivery. Lives on the client rather than
    # in this closure because the reply side (app/tg_handler.py) records
    # into it too, and because it is restored from the outbox at startup.

    def _can_notify_disconnect() -> bool:
        if _last_disconnect_notif_time is None:
            return True
        elapsed = (datetime.now() - _last_disconnect_notif_time).total_seconds()
        return elapsed >= 3600  # не чаще раза в час

    @client.on_ready
    async def handle_ready(snapshot: dict):
        nonlocal _first_connect, _disconnect_notice_pending
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved users: %s", resolver.users)

            log.info("Known chats: %s", resolver.chats)
            log.info("Known users: %s", resolver.users)

        # Status notifications go out to every Telegram group the bridge is
        # currently routing to (not just the default one) — otherwise
        # someone only watching a non-default group would never see them.
        if not _first_connect:
            if _disconnect_notice_pending:
                await sender.broadcast("✅ <b>Max:</b> соединение восстановлено")
                _disconnect_notice_pending = False
        else:
            chat_count = len(resolver.chats)
            await sender.broadcast(f"✅ <b>Max:</b> подключён | чатов: {chat_count}")
        _first_connect = False

        await _restore_read_state()

        if getattr(_settings, "catchup_enabled", False):
            asyncio.create_task(_catch_up_missed())

    async def _restore_read_state() -> None:
        """Bring back what the read markers need across a restart.

        Both halves used to live only in memory, and both are consulted
        long after the message they point at: the id a reply marks the MAX
        chat read up to, and the Telegram message a MAX read marker ticks
        with ✅. After a restart a reply marked nothing and an arriving
        read marker had nothing to tick — the two symptoms this restores.

        Never overwrites what this run already knows: a live message that
        arrived before this ran is newer than anything in the database.
        """
        try:
            seen_ids = await client.outbox.seen_message_ids()
            anchors = await client.outbox.last_tg_messages()
        except Exception:
            log.exception("Could not restore the read-marker state from the outbox")
            return
        for chat_id, message_id in seen_ids.items():
            client.last_message_ids.setdefault(_as_chat_key(chat_id), message_id)
        for chat_id, anchor in anchors.items():
            client.last_tg_message.setdefault(_as_chat_key(chat_id), anchor)
        log.info(
            "Restored read state: %d chats with a known MAX message id, "
            "%d with a Telegram message to mark",
            len(seen_ids), len(anchors),
        )

    async def _catch_up_missed() -> None:
        """Forward what MAX received while the bridge was down.

        Only chats that already have a topic are considered — a chat with
        no topic was never bridged, and pulling its history in on the
        first connect would open a topic full of old messages nobody
        asked for. The per-chat mark comes from the outbox database, so
        it survives restarts; chats with no mark are skipped for the same
        reason.
        """
        limit = getattr(_settings, "catchup_limit", 0) or 0
        if limit <= 0:
            return
        try:
            marks = await client.outbox.seen_marks()
        except Exception:
            log.exception("Catch-up: cannot read the seen marks")
            return
        if not marks:
            # Nothing forwarded on this version yet. Start the count from
            # where each chat stands now instead of skipping chats until
            # their first message arrives — otherwise a quiet chat stays
            # uncovered for as long as it stays quiet.
            await _seed_seen_marks()
            return
        total = 0
        for chat_id, mark in marks.items():
            key = _as_chat_key(chat_id)
            if sender.topic_store.get_topic(key) is None:
                continue
            try:
                total += await _ingest_history(key, limit, since=mark,
                                               what="подхват пропущенного")
            except Exception:
                log.exception("Catch-up failed for MAX chat %s", chat_id)
        if total:
            log.info("Catch-up: forwarded %d missed messages", total)

    async def _seed_seen_marks() -> None:
        """Mark every bound chat as forwarded up to its last message.

        Only ever runs with the table empty, and takes the time from MAX's
        own snapshot (``lastMessage.time``) rather than the clock here:
        message times come from MAX's server, so a container whose clock
        runs fast would otherwise skip messages as "older than the mark".
        ``lastEventTime`` is no good for this — it also moves on reads and
        joins, so it can jump past a message that was never forwarded.
        """
        seeded = 0
        for chat_id, chat in (resolver.chats_raw or {}).items():
            if sender.topic_store.get_topic(chat_id) is None:
                continue
            last = (chat or {}).get("lastMessage") or {}
            stamp = last.get("time") if isinstance(last, dict) else None
            if stamp is None:
                continue
            await client.outbox.mark_seen(chat_id, stamp, last.get("id"))
            seeded += 1
        log.info("Catch-up: starting from now — %d chat(s) marked; messages "
                 "that arrived before this are only in MAX", seeded)

    @client.on_qr
    async def handle_qr(qr_url: str, png_bytes: bytes):
        # Only the default chat gets the QR image (unlike the
        # connect/disconnect/crash notices below, which go to every
        # routed group) — logging into MAX is a one-off admin action,
        # no need to spam every group with a login QR.
        await sender.send_photo(
            png_bytes,
            caption=(
                "🔑 <b>Max:</b> требуется авторизация — отсканируйте QR-код в MAX\n"
                f"Ссылка: {escape(qr_url)}"
            ),
            filename="max_login_qr.png",
        )

    @client.on_disconnect
    async def handle_disconnect():
        nonlocal _last_disconnect_notif_time, _disconnect_notice_pending
        if not _can_notify_disconnect():
            log.info("Disconnect notification suppressed (throttle)")
            return
        _last_disconnect_notif_time = datetime.now()
        _disconnect_notice_pending = True
        await sender.broadcast("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_read
    async def handle_read(event: MaxReadEvent):
        """Mirror a MAX read-marker move as a ✅ reaction on the last
        message we forwarded into that chat's topic. Ignored when it's our
        own read marker moving (e.g. you read the chat on your phone) —
        only the *other* side reading is interesting to see in Telegram.

        Every step says what it did in the log: until this was here, a
        read marker that never arrived, one skipped as ours, one with no
        message to pin the ✅ to and one Telegram refused all looked the
        same from outside — nothing in the log at all.
        """
        log.info(
            "MAX read event: chat=%s user=%s mark=%s set_as_unread=%s my_id=%s",
            event.chat_id, event.user_id, event.mark,
            event.set_as_unread, client.my_id,
        )
        if event.set_as_unread:
            log.info("MAX read event: chat=%s marked UNREAD — no ✅", event.chat_id)
            return
        # Compared as strings: MAX ids travel as ints in some payloads and
        # as strings in others, and a type mismatch here fails open — the
        # ✅ would then be posted for your *own* read marker moving, which
        # reads in Telegram as "they read it" when nobody has.
        if client.my_id is not None and str(event.user_id) == str(client.my_id):
            log.info("MAX read event: chat=%s is our own read marker — no ✅",
                     event.chat_id)
            return
        last = client.last_tg_message.get(event.chat_id)
        if last is None:
            log.info(
                "MAX read event: chat=%s has no forwarded Telegram message to "
                "mark (known: %s) — no ✅",
                event.chat_id, sorted(map(str, client.last_tg_message)) or "нет",
            )
            return
        tg_chat_id, tg_message_id = last
        ok = await sender.set_reaction(tg_chat_id, tg_message_id, "✅")
        log.info("MAX read event: chat=%s → ✅ on Telegram message %s in %s: %s",
                 event.chat_id, tg_message_id, tg_chat_id,
                 "поставлена" if ok else "НЕ поставлена")

    @client.on_reaction
    async def handle_reaction(event: MaxReactionEvent):
        """Surface a MAX message-reaction change (someone reacted 👍/❤️/etc.
        to a message) as a short status line in that chat's topic. We don't
        currently track individual MAX message_id → Telegram message_id
        pairs, so this can't be attached as a Telegram reaction on the
        exact corresponding message (unlike read events, which only need
        the *latest* message) — a status line is the simplest faithful
        mirror without adding that extra bookkeeping."""
        if not event.counters:
            return
        thread_id = sender.topic_store.get_topic(event.chat_id)
        if thread_id is None:
            return  # no topic yet — nothing sent to this chat so far
        tg_chat_id = sender.resolve_chat_id(event.chat_id)
        parts = " ".join(
            f"{c.get('reaction', '?')}×{c.get('count', 1)}" for c in event.counters
        )
        await sender.send(f"👍 <i>Реакция на сообщение: {parts}</i>",
                          message_thread_id=thread_id, chat_id=tg_chat_id)

    def _as_chat_key(chat_id: Any) -> Any:
        """MAX chat ids come back from the database as text; everywhere
        else they are ints. Keep one shape, so a restored entry matches the
        key a live event looks up."""
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return chat_id

    async def _ingest_max_message(msg: MaxMessage) -> None:
        """Persist a MAX message, then deliver it to Telegram.

        The one path in: a live event, a message pulled in when a topic is
        created, and one picked up after downtime all go through here, so
        each is queued before any delivery is attempted and survives a
        Telegram outage the same way.
        """
        item_id = await client.outbox.add(outbox.MAX_TO_TG, _max_message_to_payload(msg))
        # Claim the item before the background retry sweep can see it as
        # pending (see Outbox.try_start) — held until the row is
        # finalized, so the sweep can't start a duplicate delivery while
        # this one is still in progress.
        client.outbox.try_start(item_id)
        try:
            try:
                await _deliver_max_message(msg)
            except Exception as exc:
                log.exception(
                    "Failed to forward MAX message chat=%s id=%s to Telegram; "
                    "kept in outbox (id=%s) for retry",
                    msg.chat_id, msg.message_id, item_id,
                )
                await client.outbox.mark_failed(item_id, str(exc))
                return
            await client.outbox.remove(item_id)
        finally:
            client.outbox.finish(item_id)
            # Marked even when delivery failed: the message is in the
            # outbox, so the retry loop owns it from here — a catch-up
            # must not queue it a second time.
            await client.outbox.mark_seen(msg.chat_id, msg.timestamp, msg.message_id)
            # What a reply from Telegram marks this chat read up to. The
            # live path records it in app/pymax_client.py as events arrive;
            # a message pulled from history never passed through there.
            if msg.message_id:
                client.last_message_ids[msg.chat_id] = msg.message_id

    @client.on_message
    async def handle_message(msg: MaxMessage):
        # An echo of a message the bridge itself just sent — drop it, or it
        # would loop back into Telegram. Not an outbox-worthy event: it was
        # never meant to be forwarded, so there's nothing to persist/retry.
        if client.is_bridge_echo(msg):
            return

        await _ingest_max_message(msg)

    async def _ingest_history(chat_id: Any, limit: int, *,
                              since: int | None = None, what: str = "догрузка") -> int:
        """Pull recent MAX messages into Telegram, oldest first.

        ``since`` keeps a catch-up to what arrived after the last forwarded
        message; without it (a freshly bound topic) the last ``limit``
        messages are taken as they are.
        """
        messages = await client.fetch_recent_messages(chat_id, limit)
        if since is not None:
            messages = [m for m in messages if (m.timestamp or 0) > since]
        if not messages:
            return 0
        log.info("%s: %d messages from MAX chat %s", what, len(messages), chat_id)
        forwarded = 0
        for msg in messages:
            # A message the bridge itself relayed into MAX comes back in
            # history like any other; forwarding it would put a Telegram
            # message back into its own topic. The seen mark still moves
            # past it — the messages arrive oldest first, so nothing
            # unforwarded is jumped over, and the next catch-up starts
            # after it instead of fetching it again.
            if await _sent_by_bridge(msg):
                log.info("%s: skipped the bridge's own message id=%s in MAX chat %s",
                         what, msg.message_id, chat_id)
                await client.outbox.mark_seen(msg.chat_id, msg.timestamp, msg.message_id)
                continue
            await _ingest_max_message(msg)
            forwarded += 1
        return forwarded

    async def _sent_by_bridge(msg: MaxMessage) -> bool:
        """True if the bridge sent this MAX message itself.

        ``is_bridge_echo`` only knows the live echo of a send made by this
        process; the outbox table also covers a history copy and survives
        a restart.
        """
        if client.is_bridge_echo(msg):
            return True
        if not msg.message_id:
            return False
        try:
            return await client.outbox.was_sent_by_bridge(msg.chat_id, msg.message_id)
        except Exception:
            log.exception("Could not check whether MAX message %s in chat %s "
                          "was sent by the bridge", msg.message_id, msg.chat_id)
            return False

    client.backfill_chat = _ingest_history

    async def _deliver_max_message(msg: MaxMessage):
        log.info(
            "New message: chat=%s sender=%s is_self=%s text=%r attaches=%d",
            msg.chat_id,
            msg.sender_id,
            msg.is_self,
            (msg.text[:80] + "…") if len(msg.text) > 80 else msg.text,
            len(msg.attaches),
        )

        # A message YOU typed manually on your own phone/other MAX client is
        # also is_self=True but is NOT a bridge echo — that one gets
        # forwarded, so your own outgoing messages stay visible in the
        # Telegram topic too.
        is_manual_self = msg.is_self

        raw_sender = await resolver.resolve_user(msg.sender_id)
        is_dm = resolver.is_dm(msg.chat_id)

        # One forum topic per Max chat, in whichever Telegram group that chat
        # is routed to (see TelegramSender.resolve_chat_id: an existing
        # binding, then MAX_CHAT_ROUTES, then the default group).
        topic_title, force_rename = await _topic_title_for_message(
            msg, resolver, raw_sender, is_dm,
        )
        raw_chat = resolver.chat_name(msg.chat_id)

        existing_thread = sender.topic_store.get_topic(msg.chat_id)
        thread_id = await sender.ensure_topic(msg.chat_id, topic_title, force_rename=force_rename)
        target_chat_id = sender.resolve_chat_id(msg.chat_id)

        # First time we touch this chat → publish a pinned profile card so the
        # topic starts with context (avatar, name, etc.).
        if existing_thread is None and thread_id is not None:
            from app.tg_handler import post_topic_intro
            asyncio.create_task(post_topic_intro(
                sender.bot, target_chat_id, client, msg.chat_id, thread_id,
            ))

        sender_label = escape(raw_sender)
        chat_label = escape(raw_chat)
        header_text = _header(sender_label, chat_label, is_dm, is_manual_self)

        last_message = None

        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        if link_type in ("FORWARD", "REPLY"):
            last_message = await _handle_linked_message(
                link, link_type, header_text, client, sender, resolver,
                thread_id=thread_id, msg=msg, chat_id=target_chat_id,
            )
            if msg.text:
                last_message = await sender.send(f"{header_text}\n{escape(msg.text)}", message_thread_id=thread_id, chat_id=target_chat_id)
            log.info("Forwarded link type=%s → TG", link_type)
            if last_message is not None:
                await client.remember_tg_anchor(msg.chat_id, target_chat_id, last_message.message_id)
            return

        meaningful_attaches = [
            a for a in msg.attaches
            if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
        ]

        if meaningful_attaches:
            group_caption = header_text
            if msg.text:
                group_caption = f"{header_text}\n{escape(msg.text)}"
            try:
                grouped = await _try_send_media_group(
                    meaningful_attaches,
                    client,
                    sender,
                    group_caption,
                    thread_id,
                    msg,
                    target_chat_id,
                )
            except Exception:
                log.exception("Failed to prepare attachment group")
                grouped = None
            if grouped is not None and grouped is not False:
                log.info("Forwarded %d attachments as media group", len(meaningful_attaches))
                await client.remember_tg_anchor(msg.chat_id, target_chat_id, grouped.message_id)
                return
            if grouped is False:
                fail_msg = await sender.send(
                    f"{group_caption}\n<i>[группа вложений — не удалось загрузить]</i>",
                    message_thread_id=thread_id, chat_id=target_chat_id,
                )
                if fail_msg is not None:
                    await client.remember_tg_anchor(msg.chat_id, target_chat_id, fail_msg.message_id)
                return

            text_sent = False
            for i, attach in enumerate(meaningful_attaches):
                if i == 0 and msg.text:
                    cap = f"{header_text}\n{escape(msg.text)}"
                    text_sent = True
                else:
                    cap = header_text
                try:
                    result = await _send_attach(attach, client, sender, cap, thread_id=thread_id, msg=msg, chat_id=target_chat_id)
                    if result is not None:
                        last_message = result
                except Exception:
                    log.exception(
                        "Failed to forward attach _type=%s",
                        attach.get("_type"),
                    )
                    await sender.send(
                        f"{cap}\n{_attachment_failure(attach)}",
                        message_thread_id=thread_id, chat_id=target_chat_id,
                    )
                log.info("Processed attach _type=%s", attach.get("_type"))

            if msg.text and not text_sent:
                last_message = await sender.send(f"{header_text}\n{escape(msg.text)}", message_thread_id=thread_id, chat_id=target_chat_id)
        else:
            if msg.text:
                body = escape(msg.text)
            else:
                control = await _control_event_line(msg, resolver)
                body = (f"<i>{control}</i>" if control
                        else "<i>[нетекстовое сообщение]</i>")
            last_message = await sender.send(f"{header_text}\n{body}", message_thread_id=thread_id, chat_id=target_chat_id)
            log.info("Forwarded text → TG")

        if last_message is not None:
            await client.remember_tg_anchor(msg.chat_id, target_chat_id, last_message.message_id)

    # Exposed for the background retry loop (see app/outbox_retry.py) to
    # re-attempt a MAX→TG message that failed and is still sitting in the
    # outbox — same delivery path a live message goes through, just called
    # again with the payload reloaded from the DB.
    client.redeliver_max_message = _deliver_max_message

    return client
