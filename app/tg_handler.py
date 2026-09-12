import asyncio
import io
import logging
import re
import socket
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import MessageEntityType
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app import outbox
from app.outbox import PermanentDeliveryFailure
from app.pymax_client import PyMaxClient, _normalized_phone
from app.topics import TopicStore

log = logging.getLogger(__name__)

MAX_CLIENT_KEY = "max_client"
TOPIC_STORE_KEY = "topic_store"
ALLOWED_USER_KEY = "allowed_user_ids"
# Default/fallback Telegram supergroup — used for status messages and as the
# target for brand-new Max chats with no explicit route. Commands like /bind
# and /add now operate on whichever supergroup they're invoked in, so the
# bot is no longer limited to a single group.
SUPERGROUP_KEY = "supergroup_id"
MAX_UPLOAD_BYTES_KEY = "max_upload_bytes"
MEDIA_GROUPS_KEY = "pending_media_groups"
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MEDIA_GROUP_DELAY = 0.8
TG_FILE_RETRIES = 3
TG_CONNECT_TIMEOUT = 20.0
TG_FILE_TIMEOUT = 180.0

# TCP keepalive for both HTTPXRequest connections below (regular bot-API
# calls and, more importantly, long-polling get_updates). When TG_PROXY is
# a mandatory SOCKS5 hop (Telegram blocked directly, so there's no way
# around it), the proxy or an intermediate NAT/firewall can silently drop
# an idle TCP connection without telling either side — httpx/httpcore only
# discover this the next time they try to use it, surfacing as
# "Server disconnected without sending a response" (RemoteProtocolError)
# mid-poll. python-telegram-bot's own Updater.start_polling already
# retries this automatically (it's a NetworkError), so the bot keeps
# running either way — but enabling TCP keepalive lets the OS proactively
# probe the connection and notice it's dead sooner, which reduces (though
# can't fully eliminate, since it depends on the proxy's own behavior) how
# often this happens: probe after 30s idle, every 10s, give up after 3
# missed probes (~60s worst case to detect).
_TG_TCP_KEEPALIVE_OPTIONS = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
for _opt_name, _value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
    _opt = getattr(socket, _opt_name, None)
    if _opt is not None:
        _TG_TCP_KEEPALIVE_OPTIONS.append((socket.IPPROTO_TCP, _opt, _value))
    else:
        log.debug("socket.%s not available on this platform, skipping", _opt_name)

_MAX_URL_RE = re.compile(r"https?://(?:web\.)?max\.ru/(-?\d+)")

# Telegram entity type → MAX element type. The MAX names match what the
# existing codebase used (STRONG) and what MAX renders for the formatting
# styles surfaced in its UI (bold/italic/strike/underline/code).
_TG_TO_MAX_ELEMENT_TYPE = {
    MessageEntityType.BOLD: "STRONG",
    MessageEntityType.ITALIC: "EMPHASIZED",
    MessageEntityType.STRIKETHROUGH: "STRIKETHROUGH",
    MessageEntityType.UNDERLINE: "UNDERLINE",
    MessageEntityType.CODE: "MONOSPACED",
    # MAX's WebSocket protocol exposes a smaller element enum than its bot
    # HTTP API: BLOCKQUOTE / CODE_BLOCK / HIGHLIGHTED / HEADING are all
    # rejected with "No enum constant". Best fallback for multi-line code
    # is the same monospace style as inline code. Telegram blockquotes
    # have no MAX counterpart at all — let them through as plain text
    # rather than fail the whole send_message.
    MessageEntityType.PRE: "MONOSPACED",
}


def _utf16_to_char_offset(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-units offset (what Telegram uses for entity
    positions) into a Python codepoint index (which MAX appears to use)."""
    if utf16_offset <= 0 or not text:
        return 0
    encoded = text.encode("utf-16-le")
    truncated = encoded[: utf16_offset * 2]
    return len(truncated.decode("utf-16-le", errors="ignore"))


def _entities_to_max_elements(text: str, entities) -> list:
    """Map Telegram message entities to MAX `elements` descriptors so basic
    inline formatting (bold/italic/strike/underline/code/link) survives the
    Telegram → MAX bridge."""
    if not entities:
        return []
    elements: list[dict] = []
    for e in entities:
        start = _utf16_to_char_offset(text, e.offset)
        end = _utf16_to_char_offset(text, e.offset + e.length)
        length = end - start
        if length <= 0:
            continue
        max_type = _TG_TO_MAX_ELEMENT_TYPE.get(e.type)
        if max_type:
            elements.append({"type": max_type, "from": start, "length": length})
        elif e.type == MessageEntityType.TEXT_LINK and getattr(e, "url", None):
            elements.append({
                "type": "LINK",
                "from": start,
                "length": length,
                "attributes": {"url": e.url},
            })
    return elements


def _parse_max_chat_id(s: str) -> int | None:
    """Accept either a raw chat id or a web.max.ru URL."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    m = _MAX_URL_RE.match(s)
    if m:
        return int(m.group(1))
    return None


def _log_command(update: Update, name: str) -> None:
    """Log every command invocation: who ran it, where, and with what args.
    Called at the very top of each command handler, before any permission
    or validity checks, so denied/invalid attempts show up in the logs too."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    args = message.text if message and message.text else ""
    thread_id = message.message_thread_id if message else None
    log.info(
        "/%s invoked by user_id=%s (@%s) in chat_id=%s thread_id=%s: %r",
        name,
        user.id if user else None,
        user.username if user else None,
        chat.id if chat else None,
        thread_id,
        args,
    )


def _peer_id_in_dm(resolver, chat_id) -> int | None:
    """Return the other participant of a DIALOG chat (i.e., not us)."""
    chat = resolver.chats_raw.get(chat_id) or {}
    my_id = resolver.my_id
    for uid_str in chat.get("participants") or {}:
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if uid != my_id:
            return uid
    # MAX convention: positive chat_id == DM chat_id == peer's user id.
    # Falls back here for chats whose participants list wasn't fetched yet.
    try:
        return int(chat_id) if int(chat_id) > 0 else None
    except (TypeError, ValueError):
        return None


def _resolve_topic_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Common prelude for topic handlers. Returns (message, max_chat_id, max_client)
    if the message should be routed, or None to drop silently."""
    message = update.message
    if message is None:
        return None
    thread_id = message.message_thread_id
    if thread_id is None or not message.is_topic_message:
        return None
    topic_store: TopicStore | None = context.bot_data.get(TOPIC_STORE_KEY)
    tg_chat_id = update.effective_chat.id if update.effective_chat else None
    max_chat_id = (
        topic_store.chat_for_topic(tg_chat_id, thread_id)
        if topic_store and tg_chat_id is not None else None
    )
    if max_chat_id is None:
        return None
    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        return None
    max_client: PyMaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    return message, max_chat_id, max_client


async def _surface_send_result(resp, *, bot, tg_chat_id, tg_message_id, notify,
                                max_client=None, max_chat_id=None) -> bool:
    """Translate a Max send_message response into a Telegram reaction or
    warning, and — on success — mark the MAX chat as read up to the last
    message we saw from it. Returns True iff MAX actually confirmed
    delivery (this drives whether the caller removes the message from the
    outbox — see app/outbox.py).

    Takes a plain ``telegram.Bot`` + chat_id/message_id rather than a live
    PTB ``Message`` so it works identically for a fresh reply (bot =
    context.bot, from the reply-side Application) and for a background
    outbox retry replayed after a restart (bot = sender.bot — a different
    Bot instance, same token) where the original Message object no longer
    exists. ``notify`` is how errors get shown to the user — the live
    caller passes ``message.reply_text``; a retry has no message to reply
    to, so it posts into the topic instead (see callers below).

    Telegram's Bot API gives bots no way to know when a human actually
    reads a message, so there's no true "read receipt" trigger available.
    Replying in the topic is the best available signal that you've seen
    the conversation, so that's what drives the MAX-side read marker.
    """
    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "не удалось отправить сообщение")
        if err.get("permanent"):
            # MAX refused the content itself, not the moment — say so
            # plainly and let the caller drop it instead of re-uploading
            # the same rejected bytes on every sweep, forever.
            await notify(
                f"⚠️ MAX отклонил это сообщение, повторять не буду: {desc}"
            )
            raise PermanentDeliveryFailure(desc)
        await notify(f"⚠️ MAX: {desc}")
        return False
    if not resp:
        await notify("⚠️ Таймаут от MAX — сообщение не подтверждено.")
        return False
    if tg_message_id is not None:
        try:
            await bot.set_message_reaction(chat_id=tg_chat_id, message_id=tg_message_id,
                                            reaction="👀")
        except Exception:
            log.warning(
                "Could not set 👀 reaction on confirmed message (chat=%s, message_id=%s): "
                "likely missing permission or an unsupported reaction for this chat",
                tg_chat_id, tg_message_id, exc_info=True,
            )

    if max_client is not None and max_chat_id is not None:
        last_id = max_client.last_message_ids.get(max_chat_id)
        if last_id:
            ok = await max_client.read_message(max_chat_id, last_id)
            if ok:
                log.info(
                    "Marked MAX chat_id=%s as read up to message_id=%s (reply in topic)",
                    max_chat_id, last_id,
                )
    return True


async def _on_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a text message typed in a forum topic back to the matching Max chat."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target
    if not message.text:
        return

    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    elements = _entities_to_max_elements(message.text, message.entities)
    tg_chat_id = message.chat_id
    thread_id = message.message_thread_id

    item_id = await max_client.outbox.add(outbox.TG_TO_MAX_TEXT, {
        "max_chat_id": max_chat_id,
        "tg_chat_id": tg_chat_id,
        "thread_id": thread_id,
        "tg_message_id": message.message_id,
        "text": message.text,
        "elements": elements,
    })
    # Claim the item before the background retry sweep can see it as
    # pending (see Outbox.try_start) — this call can never fail here since
    # item_id was just minted above. Held until the row is finalized
    # below, so the sweep can't start a duplicate attempt while this one
    # is still in progress.
    max_client.outbox.try_start(item_id)
    try:
        try:
            resp = await max_client.send_message(max_chat_id, message.text,
                                                  elements=elements)
        except Exception as exc:
            log.exception("Failed to send reply to Max chat %s", max_chat_id)
            await message.reply_text("⚠️ Ошибка при отправке в Max. Повторю попытку позже.")
            await max_client.outbox.mark_failed(item_id, str(exc))
            return

        ok = await _surface_send_result(
            resp, bot=context.bot, tg_chat_id=tg_chat_id, tg_message_id=message.message_id,
            notify=message.reply_text, max_client=max_client, max_chat_id=max_chat_id,
        )
        if ok:
            await max_client.outbox.remove(item_id)
        else:
            await max_client.outbox.mark_failed(item_id, "MAX did not confirm delivery")
    finally:
        max_client.outbox.finish(item_id)


async def _download_tg_file(file_obj, max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES) -> bytes | None:
    """Pull bytes from a Telegram File object via the Bot API."""
    file_size = getattr(file_obj, "file_size", None)
    if file_size is not None and file_size > max_bytes:
        log.warning("Telegram file is too large: %s bytes > %s bytes", file_size, max_bytes)
        return None
    for attempt in range(1, TG_FILE_RETRIES + 1):
        try:
            tg_file = await file_obj.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            if len(data) > max_bytes:
                log.warning("Downloaded Telegram file is too large: %s bytes > %s bytes", len(data), max_bytes)
                return None
            return data
        except TimedOut:
            log.warning(
                "Telegram file download timeout (attempt %d/%d)",
                attempt,
                TG_FILE_RETRIES,
            )
            if attempt < TG_FILE_RETRIES:
                await asyncio.sleep(2 * attempt)
        except Exception:
            log.exception("Failed to download Telegram file")
            return None
    return None


async def _download_tg_file_by_id(bot, file_id: str, max_bytes: int) -> bytes | None:
    """Same as _download_tg_file, but from a bare file_id via the Bot API
    instead of a live attachment object off a Message — Telegram file_ids
    stay valid indefinitely, so this is what lets an outbox retry
    re-download media after a restart, with no Update/Message left."""
    for attempt in range(1, TG_FILE_RETRIES + 1):
        try:
            tg_file = await bot.get_file(file_id)
            file_size = getattr(tg_file, "file_size", None)
            if file_size is not None and file_size > max_bytes:
                log.warning("Telegram file is too large: %s bytes > %s bytes", file_size, max_bytes)
                return None
            data = bytes(await tg_file.download_as_bytearray())
            if len(data) > max_bytes:
                log.warning("Downloaded Telegram file is too large: %s bytes > %s bytes", len(data), max_bytes)
                return None
            return data
        except TimedOut:
            log.warning(
                "Telegram file download timeout (attempt %d/%d)",
                attempt,
                TG_FILE_RETRIES,
            )
            if attempt < TG_FILE_RETRIES:
                await asyncio.sleep(2 * attempt)
        except Exception:
            log.exception("Failed to download Telegram file by id")
            return None
    return None


def _duration_ms(duration) -> int | None:
    """Telegram durations in milliseconds, from either shape PTB hands us
    (a timedelta on newer versions, plain seconds on older ones)."""
    if duration is None:
        return None
    if hasattr(duration, "total_seconds"):
        return int(duration.total_seconds() * 1000)
    return int(duration * 1000)


def _media_spec_from_message(message) -> dict | None:
    """Extract a JSON-safe {kind, file_id, filename, mimetype, duration_ms}
    from a live PTB message's attachment — everything _upload_media_by_spec
    needs to redo the download+upload later from an outbox row, without
    holding on to the (non-serializable, restart-doesn't-survive) Message
    object itself."""
    if message.photo:
        photo = message.photo[-1]
        return {"kind": "photo", "file_id": photo.file_id}
    if message.voice:
        v = message.voice
        return {"kind": "voice", "file_id": v.file_id,
                "duration_ms": _duration_ms(v.duration)}
    if message.audio:
        a = message.audio
        return {"kind": "audio", "file_id": a.file_id,
                "filename": a.file_name, "mimetype": a.mime_type}
    if message.document:
        d = message.document
        return {"kind": "document", "file_id": d.file_id,
                "filename": d.file_name, "mimetype": d.mime_type}
    if message.video:
        v = message.video
        return {"kind": "video", "file_id": v.file_id,
                "filename": v.file_name, "mimetype": v.mime_type}
    if message.video_note:
        # Telegram's round video messages. MAX has its own equivalent,
        # so these go over as VideoNote rather than a plain video.
        vn = message.video_note
        return {"kind": "video_note", "file_id": vn.file_id,
                "duration_ms": _duration_ms(vn.duration)}
    return None


async def _upload_media_by_spec(bot, spec: dict, max_client, max_chat_id, max_upload_bytes):
    """Download one Telegram medium (by file_id) and upload it to MAX.
    Counterpart to _media_spec_from_message — this is the half that runs
    both for a live message and for an outbox retry."""
    kind = spec.get("kind")
    file_id = spec.get("file_id")
    if not kind or not file_id:
        return None
    data = await _download_tg_file_by_id(bot, file_id, max_upload_bytes)
    if data is None:
        return None

    if kind == "photo":
        return await max_client.upload_photo(data, chat_id=max_chat_id)
    if kind == "voice":
        return await max_client.upload_audio(
            data, chat_id=max_chat_id,
            filename="voice.ogg", mimetype="audio/ogg",
            duration=spec.get("duration_ms"),
        )
    if kind == "audio":
        return await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=spec.get("filename") or "audio",
            mimetype=spec.get("mimetype") or "audio/mpeg",
        )
    if kind == "document":
        return await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=spec.get("filename") or "file",
            mimetype=spec.get("mimetype") or "application/octet-stream",
        )
    if kind == "video":
        return await max_client.upload_video(
            data, chat_id=max_chat_id,
            filename=spec.get("filename") or "video.mp4",
            mimetype=spec.get("mimetype") or "video/mp4",
        )
    if kind == "video_note":
        return await max_client.upload_video_note(
            data, chat_id=max_chat_id,
            filename="video_note.mp4",
            duration=spec.get("duration_ms"),
        )
    return None


async def _deliver_tg_media(bot, max_client, max_chat_id, max_upload_bytes,
                             caption: str, elements: list, specs: list[dict], *,
                             notify, tg_chat_id, tg_message_id) -> bool:
    """Upload every attachment and send the MAX message. Shared by the live
    path (_send_topic_media_messages) and outbox retries — this is exactly
    the part that needs redoing on retry, since nothing before this point
    (the TG-side download source) is affected by MAX being unreachable."""
    attaches = []
    for spec in specs:
        try:
            attach = await _upload_media_by_spec(bot, spec, max_client, max_chat_id, max_upload_bytes)
        except Exception:
            log.exception("Failed to upload media (kind=%s) to MAX chat %s",
                          spec.get("kind"), max_chat_id)
            attach = None
        if attach:
            attaches.append(attach)
        else:
            await notify("⚠️ Не удалось загрузить файл в MAX.")

    if not attaches:
        return False

    try:
        resp = await max_client.send_message(
            max_chat_id, text=caption, elements=elements, attaches=attaches,
        )
    except Exception:
        log.exception("Failed to send media group to Max chat %s", max_chat_id)
        await notify("⚠️ Ошибка при отправке в Max. Повторю попытку позже.")
        return False

    return await _surface_send_result(
        resp, bot=bot, tg_chat_id=tg_chat_id, tg_message_id=tg_message_id,
        notify=notify, max_client=max_client, max_chat_id=max_chat_id,
    )


async def _send_topic_media_messages(messages, max_chat_id, max_client, max_upload_bytes, bot):
    """Upload a Telegram album and send all attachments in one MAX message."""
    specs = []
    for message in messages:
        spec = _media_spec_from_message(message)
        if spec is None:
            await message.reply_text("⚠️ Не удалось загрузить файл в MAX.")
            continue
        specs.append(spec)

    if not specs:
        return

    caption_message = next((message for message in messages if message.caption), messages[0])
    caption = caption_message.caption or ""
    elements = _entities_to_max_elements(caption, caption_message.caption_entities)
    tg_chat_id = caption_message.chat_id
    thread_id = caption_message.message_thread_id

    item_id = await max_client.outbox.add(outbox.TG_TO_MAX_MEDIA, {
        "max_chat_id": max_chat_id,
        "tg_chat_id": tg_chat_id,
        "thread_id": thread_id,
        "tg_message_id": caption_message.message_id,
        "caption": caption,
        "elements": elements,
        "media_specs": specs,
    })

    # Claim the item before the background retry sweep can see it as
    # pending (see Outbox.try_start) — held until the row is finalized
    # below. Voice/video attachments can legitimately take pymax's
    # internal "attachment not ready" retry up to a minute to resolve
    # (see app/pymax_client.py), far longer than the sweep's 20s poll
    # interval, so without this the sweep would start a duplicate upload
    # while this one is still in progress.
    max_client.outbox.try_start(item_id)
    try:
        try:
            ok = await _deliver_tg_media(
                bot, max_client, max_chat_id, max_upload_bytes, caption, elements, specs,
                notify=caption_message.reply_text,
                tg_chat_id=tg_chat_id, tg_message_id=caption_message.message_id,
            )
        except PermanentDeliveryFailure as exc:
            # Already reported into the topic; queueing it would only
            # re-upload the same refused content until the end of time.
            log.warning(
                "MAX permanently refused a message for chat %s, dropping it: %s",
                max_chat_id, exc,
            )
            await max_client.outbox.remove(item_id)
            return
        if ok:
            await max_client.outbox.remove(item_id)
        else:
            await max_client.outbox.mark_failed(item_id, "delivery failed")
    finally:
        max_client.outbox.finish(item_id)


async def _flush_media_group(key, context) -> None:
    await asyncio.sleep(MEDIA_GROUP_DELAY)
    groups = context.bot_data.get(MEDIA_GROUPS_KEY, {})
    group = groups.pop(key, None)
    if not group:
        return
    await _send_topic_media_messages(
        group["messages"],
        group["max_chat_id"],
        group["max_client"],
        group["max_upload_bytes"],
        context.bot,
    )


async def redeliver_tg_to_max_text(max_client, bot, payload: dict) -> bool:
    """Retry entry point for a TG_TO_MAX_TEXT outbox row — called by the
    background retry loop (app/outbox_retry.py), with no live Update
    around. Same delivery + result-surfacing as _on_topic_message, just
    fed from the stored payload instead of a fresh Message, and reporting
    back into the topic instead of replying to a (long gone) message."""
    max_chat_id = payload["max_chat_id"]
    tg_chat_id = payload["tg_chat_id"]
    thread_id = payload.get("thread_id")
    tg_message_id = payload.get("tg_message_id")
    text = payload["text"]
    elements = payload.get("elements") or []

    async def notify(text_: str) -> None:
        try:
            await bot.send_message(chat_id=tg_chat_id, message_thread_id=thread_id, text=text_)
        except Exception:
            log.exception("Outbox retry: failed to post status into chat %s", tg_chat_id)

    try:
        resp = await max_client.send_message(max_chat_id, text, elements=elements)
    except Exception:
        log.exception("Outbox retry: failed to send TG→MAX text to chat %s", max_chat_id)
        return False

    return await _surface_send_result(
        resp, bot=bot, tg_chat_id=tg_chat_id, tg_message_id=tg_message_id,
        notify=notify, max_client=max_client, max_chat_id=max_chat_id,
    )


async def redeliver_tg_to_max_media(max_client, bot, payload: dict, max_upload_bytes: int) -> bool:
    """Retry entry point for a TG_TO_MAX_MEDIA outbox row — see
    redeliver_tg_to_max_text. Re-downloads each attachment by its stored
    Telegram file_id (file_ids stay valid indefinitely) and re-uploads to
    MAX via the same _deliver_tg_media the live path uses."""
    max_chat_id = payload["max_chat_id"]
    tg_chat_id = payload["tg_chat_id"]
    thread_id = payload.get("thread_id")
    tg_message_id = payload.get("tg_message_id")
    caption = payload.get("caption") or ""
    elements = payload.get("elements") or []
    specs = payload.get("media_specs") or []

    async def notify(text_: str) -> None:
        try:
            await bot.send_message(chat_id=tg_chat_id, message_thread_id=thread_id, text=text_)
        except Exception:
            log.exception("Outbox retry: failed to post status into chat %s", tg_chat_id)

    return await _deliver_tg_media(
        bot, max_client, max_chat_id, max_upload_bytes, caption, elements, specs,
        notify=notify, tg_chat_id=tg_chat_id, tg_message_id=tg_message_id,
    )


async def _on_unsupported_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Say so when an attachment type isn't routed to MAX.

    Safety net for the failure mode that hid missing video_note support:
    an attachment outside the media handler's filter never reached any
    handler, so it vanished with no upload, no error and nothing in the
    topic. Whatever is unsupported now (stickers, polls, dice, ...) at
    least says so instead of disappearing.
    """
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, _max_client = target

    attachment = message.effective_attachment
    kind = type(attachment).__name__ if attachment is not None else "unknown"
    log.warning(
        "Unsupported Telegram attachment (%s) in topic for MAX chat %s — not forwarded",
        kind, max_chat_id,
    )
    await message.reply_text(
        "⚠️ Этот тип вложения мост пока не умеет отправлять в MAX."
    )


async def _on_topic_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a single medium or a complete Telegram album to one MAX message."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target

    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    max_upload_bytes = context.bot_data.get(MAX_UPLOAD_BYTES_KEY, DEFAULT_MAX_UPLOAD_BYTES)
    media_group_id = message.media_group_id
    if media_group_id:
        groups = context.bot_data.setdefault(MEDIA_GROUPS_KEY, {})
        key = (message.chat_id, media_group_id)
        group = groups.setdefault(
            key,
            {
                "messages": [],
                "max_chat_id": max_chat_id,
                "max_client": max_client,
                "max_upload_bytes": max_upload_bytes,
                "task": None,
            },
        )
        group["messages"].append(message)
        if group["task"]:
            group["task"].cancel()
        group["task"] = asyncio.create_task(_flush_media_group(key, context))
        return

    await _send_topic_media_messages(
        [message], max_chat_id, max_client, max_upload_bytes, context.bot
    )


async def post_topic_intro(bot, supergroup_id, max_client: PyMaxClient,
                            max_chat_id, thread_id: int, *,
                            pin: bool = True) -> None:
    """Publish a profile/info card as the first message of a topic, then pin
    it. Called when a topic is freshly created (either auto on first
    incoming message or manually via /bind)."""
    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        return

    is_dm = resolver.is_dm(max_chat_id)

    if is_dm:
        peer_id = _peer_id_in_dm(resolver, max_chat_id)
        if peer_id is None:
            return
        contact = resolver.contacts_raw.get(peer_id)
        if contact is None:
            try:
                await max_client.fetch_contacts([peer_id])
            except Exception:
                log.exception("post_topic_intro: fetch_contacts failed")
            contact = resolver.contacts_raw.get(peer_id)

        name = resolver.user_name(peer_id)
        phone = (contact or {}).get("phone") or ""
        about = ((contact or {}).get("description")
                 or (contact or {}).get("about")
                 or (contact or {}).get("status") or "")
        username = (contact or {}).get("link") or (contact or {}).get("username") or ""

        lines = [f"<b>{escape(str(name))}</b>",
                 f"id: <code>{peer_id}</code>"]
        if phone:
            lines.append(f"📞 <code>{escape(str(phone))}</code>")
        if username:
            lines.append(f"🔗 {escape(str(username))}")
        if about:
            lines.append(f"\n{escape(str(about))}")
        body = "\n".join(lines)

        photo_url = None
        photo_obj = (contact or {}).get("photo") or (contact or {}).get("avatar")
        if isinstance(photo_obj, dict):
            photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                         or photo_obj.get("photoUrl"))
        photo_url = (photo_url
                     or (contact or {}).get("baseUrl")
                     or (contact or {}).get("baseRawUrl")
                     or (contact or {}).get("photoUrl")
                     or (contact or {}).get("baseRawIconUrl"))
    else:
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""

        lines = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}",
                 f"id: <code>{max_chat_id}</code>",
                 f"Участников: <b>{len(participants)}</b>"]
        if descr:
            lines.append(f"\n{escape(str(descr))}")
        if link:
            lines.append(f"\n🔗 {escape(str(link))}")
        body = "\n".join(lines)
        photo_url = chat.get("baseRawIconUrl") or chat.get("baseUrl")

    sent = None
    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                sent = await bot.send_photo(
                    chat_id=int(supergroup_id),
                    photo=InputFile(io.BytesIO(data), filename="profile.jpg"),
                    caption=body, parse_mode="HTML",
                    message_thread_id=thread_id,
                )
            except Exception:
                log.exception("post_topic_intro: send_photo failed")
                sent = None

    if sent is None:
        try:
            sent = await bot.send_message(
                chat_id=int(supergroup_id), text=body, parse_mode="HTML",
                message_thread_id=thread_id,
            )
        except Exception:
            log.exception("post_topic_intro: send_message failed")
            return

    if pin and sent is not None:
        try:
            await bot.pin_chat_message(
                chat_id=int(supergroup_id),
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            log.exception("post_topic_intro: pin_chat_message failed")


async def _cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a forum topic bound to a specific Max chat id.

    Usage: `/bind <chat_id-or-url> [optional-title]` — typed in whichever
    Telegram supergroup you want that Max chat routed to. The bot creates a
    new forum topic there (or reports an existing binding) and stores the
    mapping so messages typed in that topic get forwarded to the Max chat,
    and future messages from that Max chat land in this same group.
    """
    message = update.message
    if message is None:
        return
    _log_command(update, "bind")
    target_chat_id = update.effective_chat.id if update.effective_chat else None
    if target_chat_id is None:
        return

    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        log.warning("/bind denied for user_id=%s (not in allowed list)",
                    update.effective_user.id)
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Использование: <code>/bind &lt;chat_id или https://web.max.ru/-...&gt; "
            "[название]</code>",
            parse_mode="HTML",
        )
        return

    max_chat_id = _parse_max_chat_id(args[0])
    if max_chat_id is None:
        await message.reply_text(
            "Не понял chat_id. Пример: <code>/bind -75107924425434</code>",
            parse_mode="HTML",
        )
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(max_chat_id)
    if existing is not None:
        await message.reply_text(
            f"Этот чат MAX уже привязан к топику (thread_id=<code>{existing}</code>).",
            parse_mode="HTML",
        )
        return

    max_client: PyMaxClient = context.bot_data[MAX_CLIENT_KEY]
    resolver = getattr(max_client, "resolver", None)

    # Build a topic title: explicit second arg → known chat title → chat id.
    if len(args) > 1:
        title = " ".join(args[1:]).strip()
    elif resolver and resolver.chat_name(max_chat_id) != str(max_chat_id):
        title = resolver.chat_name(max_chat_id)
    else:
        title = str(max_chat_id)
    title = title[:128]

    try:
        topic = await context.bot.create_forum_topic(
            chat_id=target_chat_id, name=title,
        )
    except Exception as exc:
        log.exception("Failed to create forum topic for %s in %s", max_chat_id, target_chat_id)
        await message.reply_text(
            f"Не удалось создать топик: {exc}\n\n"
            "Убедитесь, что в этой группе включены темы (Topics) и бот — "
            "администратор с правом «Управление темами»."
        )
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(max_chat_id, int(target_chat_id), thread_id, title)
    log.info("/bind: created topic thread=%s title=%r for max_chat_id=%s in tg_chat_id=%s",
             thread_id, title, max_chat_id, target_chat_id)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{max_chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>) в этой группе. "
        "Пиши в новом топике — улетит в MAX.",
        parse_mode="HTML",
    )
    # Post & pin a profile card in the freshly-created topic.
    asyncio.create_task(
        post_topic_intro(context.bot, target_chat_id, max_client,
                          max_chat_id, thread_id)
    )


_MAX_LINK_RE = re.compile(r"https?://max\.ru/[A-Za-z0-9_\-/]+")


def _extract_chat_id_from_open(resp: dict) -> int | None:
    """Pick a chat id out of the various shapes opcode 57 returns."""
    if not isinstance(resp, dict):
        return None
    # Direct fields seen in practice.
    for key in ("chatId", "conversationId"):
        v = resp.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                pass
    # Nested chat object.
    chat = resp.get("chat")
    if isinstance(chat, dict):
        cid = chat.get("id")
        if isinstance(cid, int):
            return cid
        if isinstance(cid, str):
            try:
                return int(cid)
            except ValueError:
                pass
    return None


def _chat_is_known(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Whether MAX has already told us about this chat.

    Decides what a positive number means in /add: a dialog's chat id is
    positive and so is a user id, so a number we hold as a chat is that
    chat, and anything else is taken for a person.
    """
    max_client = context.bot_data.get(MAX_CLIENT_KEY)
    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        return False
    return chat_id in (resolver.chats_raw or {})


async def _cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Join a MAX chat by link if we aren't in it, then bind it to a topic.

    Usage: `/add https://max.ru/join/<token>` for an invitation, or
    `/add https://max.ru/id<digits>_gos` for a public group/channel
    handle — which binds straight away when we're subscribed already.
    See PyMaxClient.open_by_link for how each is resolved. A one-to-one
    chat has no link to paste, so `/add +79991234567` (MAX resolves the
    number) or `/add <user id>` takes the person instead and derives
    their dialog. `/add <chat id>` binds a chat straight off, like /bind:
    there is nothing to join by id.
    """
    message = update.message
    if message is None:
        return
    _log_command(update, "add")
    target_chat_id = update.effective_chat.id if update.effective_chat else None
    if target_chat_id is None:
        return

    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        log.warning("/add denied for user_id=%s (not in allowed list)",
                    update.effective_user.id)
        return
    # Primary source: the command argument itself (`/add <link-or-id>`).
    # Only an actual URL counts as the link — the argument is just as
    # often an id or a phone number, and taking those for a link left
    # their branches below unreachable.
    args = context.args or []
    first = args[0] if args else ""
    link = first if first.startswith(("http://", "https://")) else ""
    # Try to extract a max.ru link from anywhere in the message text too,
    # so `/add` works if the link was just pasted alongside the command.
    if not link and message.text:
        m = _MAX_LINK_RE.search(message.text)
        if m:
            link = m.group(0)

    # Numbers, and what they mean. A leading + is a phone — the only
    # thing separating one from an id, both being digits otherwise.
    # A negative number is a group or channel: MAX numbers those below
    # zero, and there's nothing to join by id anyway, so it binds exactly
    # as /bind would. A positive number is a user id and the dialog with
    # them is derived from the pair (PyMaxClient.open_dialog_with_user) —
    # unless it names a chat we already know, since a dialog's own id is
    # positive too and the chat we have beats the person we'd infer.
    user_id = None
    phone = None
    known_chat_id = None
    if not link and args:
        argument = " ".join(args).strip()
        if _normalized_phone(argument) is not None:
            phone = argument
        else:
            parsed = _parse_max_chat_id(args[0])
            if parsed is not None and parsed < 0:
                known_chat_id = parsed
            elif parsed is not None and _chat_is_known(context, parsed):
                known_chat_id = parsed
            elif parsed is not None:
                user_id = parsed

    if (user_id is None and phone is None and known_chat_id is None
            and (not link.startswith(("http://", "https://"))
                 or "max.ru/" not in link)):
        await message.reply_text(
            "Использование: <code>/add https://max.ru/join/...</code> "
            "(приглашение), <code>/add https://max.ru/id..._gos</code> "
            "(публичная ссылка группы/канала), <code>/add +79991234567</code> "
            "(по номеру телефона), <code>/add &lt;id пользователя&gt;</code> "
            "или <code>/add &lt;id чата&gt;</code>.",
            parse_mode="HTML",
        )
        return

    max_client: PyMaxClient = context.bot_data[MAX_CLIENT_KEY]
    try:
        if known_chat_id is not None:
            # Nothing to open: we have the id, and joining by id isn't a
            # thing in MAX. Hand the rest of this function the same shape
            # open_by_link would have returned, so binding, the title and
            # the intro card all happen exactly as they do for a link.
            log.info("/add: binding known MAX chat %s", known_chat_id)
            resolver_now = getattr(max_client, "resolver", None)
            chat_obj = None
            if resolver_now is not None:
                chat_obj = (resolver_now.chats_raw or {}).get(known_chat_id)
            resp = {"chatId": known_chat_id,
                    "chat": chat_obj if isinstance(chat_obj, dict) else {"id": known_chat_id}}
        elif phone is not None:
            log.info("/add: looking up a MAX user by phone")
            resp = await max_client.open_dialog_by_phone(phone)
        elif user_id is not None:
            log.info("/add: opening dialog with MAX user %s", user_id)
            resp = await max_client.open_dialog_with_user(user_id)
        else:
            log.info("/add: opening link %s", link)
            resp = await max_client.open_by_link(link)
    except Exception as exc:
        log.exception("open_by_link failed")
        await message.reply_text(f"⚠️ Ошибка при обращении к MAX: {exc}")
        return

    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "MAX отказал")
        await message.reply_text(f"⚠️ MAX: {desc}")
        return
    if not resp:
        await message.reply_text("⚠️ Таймаут от MAX, ссылка не открылась.")
        return

    chat_id = _extract_chat_id_from_open(resp)
    if chat_id is None:
        log.warning("/add: cannot extract chat_id from response: %s", resp)
        await message.reply_text(
            "MAX принял ссылку, но не вернул chat_id, который я понимаю. "
            "Лог: <code>" + escape(str(resp)[:300]) + "</code>",
            parse_mode="HTML",
        )
        return

    # Let the resolver pick up the freshly arrived chat metadata, if any.
    resolver = getattr(max_client, "resolver", None)
    if resolver is not None and isinstance(resp.get("chat"), dict):
        chat_obj = resp["chat"]
        resolver.chats_raw[chat_id] = chat_obj
        if chat_obj.get("type"):
            resolver.chat_types[chat_id] = chat_obj["type"]
        if chat_obj.get("title"):
            resolver.chats[chat_id] = chat_obj["title"]

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(chat_id)
    if existing is not None:
        await message.reply_text(
            f"Этот чат MAX уже привязан к топику (thread_id=<code>{existing}</code>).",
            parse_mode="HTML",
        )
        return

    # Pick a title — prefer chat title or peer name from resolver.
    title = None
    if resolver is not None:
        title = resolver.chat_name(chat_id)
        if title == str(chat_id) and resolver.is_dm(chat_id):
            peer_id = _peer_id_in_dm(resolver, chat_id)
            if peer_id is not None:
                # Best-effort fetch contact name now.
                try:
                    await max_client.fetch_contacts([peer_id])
                except Exception:
                    pass
                title = resolver.user_name(peer_id)
    if not title or title == str(chat_id):
        title = str(chat_id)
    title = title[:128]

    try:
        topic = await context.bot.create_forum_topic(
            chat_id=target_chat_id, name=title,
        )
    except Exception as exc:
        log.exception("/add: create_forum_topic failed for %s", target_chat_id)
        await message.reply_text(
            f"Не удалось создать топик: {exc}\n\n"
            "Убедитесь, что в этой группе включены темы (Topics) и бот — "
            "администратор с правом «Управление темами»."
        )
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(chat_id, int(target_chat_id), thread_id, title)
    log.info("/add: created topic thread=%s title=%r for max_chat_id=%s in tg_chat_id=%s",
             thread_id, title, chat_id, target_chat_id)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>) в этой группе.",
        parse_mode="HTML",
    )
    asyncio.create_task(
        post_topic_intro(context.bot, target_chat_id, max_client,
                          chat_id, thread_id)
    )


HELP_TEXT = (
    "<b>max2tg — мост MAX ↔ Telegram</b>\n\n"
    "Бот можно добавить в несколько Telegram-групп (с включёнными темами) — "
    "команды ниже работают в той группе, где их вводишь, и привязывают "
    "MAX-чат именно к ней. Так один и тот же MAX-аккаунт можно "
    "маршрутизировать в разные Telegram-группы: часть контактов — в одну, "
    "часть — в другую.\n\n"
    "Команды в супергруппе:\n"
    "• <code>/bind &lt;chat_id или URL&gt; [название]</code> — привязать "
    "новый топик к чату MAX в этой группе.\n"
    "• <code>/add &lt;https://max.ru/join/...&gt;</code> — открыть "
    "групповую/канальную ссылку MAX, создать топик в этой группе и "
    "поставить карточку.\n"
    "• <code>/list</code> — только в основной группе (<code>TG_CHAT_ID</code>): "
    "список всех чатов MAX (группы, каналы и личные сообщения) с id и "
    "ссылкой, чтобы скопировать нужный chat_id в <code>/bind</code>.\n"
    "• <code>/profile</code> — внутри топика: показать профиль собеседника "
    "из MAX (имя, id, аватар).\n"
    "• <code>/intro</code> — перепостить и закрепить карточку профиля "
    "в текущем топике (полезно после смены аватара).\n"
    "• <code>/del</code> — удалить текущий топик и связь с MAX-чатом "
    "(спросит подтверждение).\n"
    "• <code>/help</code> — эта справка.\n\n"
    "Просто пиши в любом привязанном топике — сообщение уйдёт в "
    "соответствующий чат MAX. Поддерживается жирный/курсив/зачёркнутый/"
    "подчёркнутый текст, моноширинный код, цитаты и ссылки. Фото, "
    "документы, видео и голосовые сообщения тоже передаются напрямую.\n\n"
    "Как только твой ответ из топика доставлен в MAX, чат там "
    "отмечается прочитанным (статус «прочитано» для собеседника). "
    "Учти: Telegram не сообщает боту, когда ты именно <i>прочитал</i> "
    "сообщение (такого события у ботов нет) — отметка ставится по "
    "факту ответа, а не по факту открытия топика.\n\n"
    "Если кто-то новый пишет тебе в MAX — топик создастся автоматически "
    "и в нём сразу появится карточка собеседника."
)


async def _cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all MAX chats known to the bridge, with their chat_id and a
    web.max.ru link — so you can copy a chat_id/link straight into
    `/bind` (or `/add`) to route it to a topic.

    Restricted to the *main* Telegram supergroup (``TG_CHAT_ID``) — the
    default/fallback group — rather than any group the bot happens to be
    in, since this is meant as a one-stop directory of the whole MAX
    account, not something you'd want repeated in every routed group.
    """
    message = update.message
    if message is None:
        return
    _log_command(update, "list")
    target_chat_id = update.effective_chat.id if update.effective_chat else None
    if target_chat_id is None:
        return

    supergroup_id = context.bot_data.get(SUPERGROUP_KEY)
    if supergroup_id is None or target_chat_id != supergroup_id:
        log.info("/list rejected: invoked outside main supergroup (chat_id=%s, expected=%s)",
                  target_chat_id, supergroup_id)
        await message.reply_text(
            "Команда <code>/list</code> доступна только в основной "
            "Telegram-группе (задана в <code>TG_CHAT_ID</code>).",
            parse_mode="HTML",
        )
        return

    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        log.warning("/list denied for user_id=%s (not in allowed list)",
                    update.effective_user.id)
        return

    max_client: PyMaxClient = context.bot_data[MAX_CLIENT_KEY]
    resolver = getattr(max_client, "resolver", None)
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]

    if not resolver or not resolver.chats_raw:
        await message.reply_text("Список чатов MAX пока пуст (нет данных снапшота).")
        return

    entries = []
    for chat_id, chat in resolver.chats_raw.items():
        chat_type = resolver.chat_types.get(chat_id, chat.get("type", "?"))
        if resolver.is_dm(chat_id):
            # DM chats don't get a real "title" from MAX — resolve the
            # peer's name instead of showing the "DM:<id>" placeholder.
            peer_id = None
            for uid_str in chat.get("participants", {}) or {}:
                try:
                    uid_int = int(uid_str)
                except (TypeError, ValueError):
                    continue
                if uid_int != resolver.my_id:
                    peer_id = uid_int
                    break
            title = resolver.user_name(peer_id) if peer_id is not None else None
            if not title or title == str(peer_id):
                title = chat.get("title") or (
                    f"Личный чат {peer_id}" if peer_id is not None else "(без названия)"
                )
        else:
            title = resolver.chat_name(chat_id)
            if title == str(chat_id):
                title = chat.get("title") or "(без названия)"
        # MAX ships a group's own invite link in the snapshot (used by
        # /profile and the topic card already), so showing it costs no
        # request. Absent for DMs and for groups without one — the only
        # way to conjure one is rework_invite_link, which *revokes* the
        # current link, so listing chats must never do that.
        entries.append((title, chat_id, chat_type, chat.get("link") or ""))

    if not entries:
        await message.reply_text("В MAX пока нет чатов.")
        return

    log.info("/list: showing %d MAX chats to user_id=%s",
             len(entries), update.effective_user.id if update.effective_user else None)

    # Group by chat type for readability instead of one flat alphabetical
    # list — groups/channels/DMs answer different questions ("what can I
    # bind?" vs "who messaged me directly?").
    SECTIONS = [
        ("CHAT", "👥 Группы"),
        ("CHANNEL", "📢 Каналы"),
        ("DIALOG", "👤 Личные сообщения"),
    ]
    by_type: dict[str, list] = {key: [] for key, _ in SECTIONS}
    other: list = []
    for entry in entries:
        bucket = by_type.get(str(entry[2]))
        (bucket if bucket is not None else other).append(entry)

    counts = ", ".join(
        f"{label.split(' ', 1)[1].lower()}: {len(by_type[key])}"
        for key, label in SECTIONS
        if by_type[key]
    )
    lines = [
        f"<b>Чаты MAX</b> — всего {len(entries)} ({counts})" if counts
        else f"<b>Чаты MAX</b> — всего {len(entries)}",
        "Скопируй <code>chat_id</code> в <code>/bind</code>, чтобы привязать к топику. "
        "Вторая ссылка (если есть) — приглашение в чат от самого MAX.",
    ]

    def _add_section(title: str, chats: list) -> None:
        if not chats:
            return
        chats = sorted(chats, key=lambda e: e[0].lower())
        lines.append(f"\n<b>{title}</b>")
        for chat_title, chat_id, _chat_type, join_link in chats:
            bound_thread = topic_store.get_topic(chat_id)
            status = f"🔗 топик #{bound_thread}" if bound_thread is not None else "◌ не привязан"
            # Ids and links go in monospace, each on its own line: they
            # exist to be copied out of the message — into /bind, a
            # browser, a note — which a word merely carrying a href is
            # not, and which a run-on line makes fiddly to select.
            entry_lines = [
                f"• <b>{escape(chat_title)}</b>",
                f"  <code>{chat_id}</code> · {status}",
                f"  <code>https://web.max.ru/{chat_id}</code>",
            ]
            if join_link:
                entry_lines.append(f"  <code>{escape(str(join_link))}</code>")
            lines.append("\n".join(entry_lines))

    for key, label in SECTIONS:
        _add_section(label, by_type[key])
    _add_section("❓ Прочее", other)

    # Telegram caps messages at 4096 chars — chunk if the directory is large.
    chunk: list[str] = []
    chunk_len = 0
    chunks: list[str] = []
    for line in lines:
        if chunk_len + len(line) + 1 > 3800 and chunk:
            chunks.append("\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        chunks.append("\n".join(chunk))

    for part in chunks:
        await message.reply_text(part, parse_mode="HTML", disable_web_page_preview=True)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    _log_command(update, "help")
    await message.reply_text(HELP_TEXT, parse_mode="HTML",
                              disable_web_page_preview=True)


async def _cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to confirm deletion of the current topic. The actual
    deletion happens in ``_on_del_callback`` when the inline button is
    pressed."""
    message = update.message
    if message is None:
        return
    _log_command(update, "del")

    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        log.warning("/del denied for user_id=%s (not in allowed list)",
                    update.effective_user.id)
        return

    target = _resolve_topic_target(update, context)
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с MAX-чатом."
        )
        return
    _, max_chat_id, _ = target
    thread_id = message.message_thread_id
    tg_chat_id = update.effective_chat.id

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить топик",
                                 callback_data=f"del:ok:{tg_chat_id}:{thread_id}:{max_chat_id}"),
            InlineKeyboardButton("Отмена", callback_data="del:cancel"),
        ]
    ])
    await message.reply_text(
        "Удалить этот топик вместе со всеми сообщениями и снять связь "
        f"с MAX-чатом <code>{max_chat_id}</code>?\n\n"
        "Восстановить нельзя. Новый топик создастся, если собеседник снова "
        "тебе напишет.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _on_del_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    user = update.effective_user
    log.info("/del callback %r from user_id=%s (@%s)",
             query.data, user.id if user else None, user.username if user else None)

    allowed_user_ids = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_ids and update.effective_user and update.effective_user.id not in allowed_user_ids:
        log.warning("/del callback denied for user_id=%s (not in allowed list)",
                    update.effective_user.id)
        return

    parts = query.data.split(":")
    if parts[:2] == ["del", "cancel"]:
        try:
            await query.edit_message_text("Отменено.")
        except Exception:
            pass
        return

    if len(parts) != 5 or parts[0] != "del" or parts[1] != "ok":
        return
    try:
        tg_chat_id = int(parts[2])
        thread_id = int(parts[3])
    except ValueError:
        return
    try:
        max_chat_id: int | str = int(parts[4])
    except ValueError:
        max_chat_id = parts[4]

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]

    # Remove mapping first — even if delete_forum_topic fails the stale link
    # is gone, and a fresh topic can be made via /bind.
    topic_store.remove(max_chat_id)

    try:
        await context.bot.delete_forum_topic(
            chat_id=tg_chat_id, message_thread_id=thread_id,
        )
    except Exception as exc:
        log.exception("/del: delete_forum_topic failed")
        try:
            await query.edit_message_text(
                f"⚠️ Связь снята, но удалить топик не получилось: {exc}"
            )
        except Exception:
            pass
        return

    log.info("/del: removed topic thread=%s for max_chat_id=%s",
             thread_id, max_chat_id)
    # The edit_message_text below will fail if the topic is already gone;
    # that's fine — the chat-level confirmation isn't critical.


async def _cmd_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-publish the pinned profile card in the current topic.

    Useful for topics that were created before this feature existed, or to
    refresh the card after the contact updated their photo/name.
    """
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    _log_command(update, "intro")
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return
    await post_topic_intro(
        context.bot, update.effective_chat.id, max_client, max_chat_id,
        message.message_thread_id,
    )


async def _cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile of the Max peer linked to the current topic."""
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    _log_command(update, "profile")
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        await message.reply_text("⚠️ Кеш контактов недоступен.")
        return

    is_dm = resolver.is_dm(max_chat_id)
    if not is_dm:
        # Group / channel: show whatever the snapshot exposes.
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""
        parts = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}"]
        parts.append(f"Участников: <b>{len(participants)}</b>")
        if descr:
            parts.append(f"\n{escape(str(descr))}")
        if link:
            parts.append(f"\n🔗 {escape(str(link))}")
        await message.reply_text("\n".join(parts), parse_mode="HTML")
        return

    peer_id = _peer_id_in_dm(resolver, max_chat_id)
    if peer_id is None:
        await message.reply_text("Не нашёл собеседника в этом чате.")
        return

    contact = resolver.contacts_raw.get(peer_id)
    if contact is None:
        try:
            resolver._parse_contacts_response(await max_client.fetch_contacts([peer_id]))
        except Exception:
            log.exception("/profile: PyMax contact fetch failed")
        contact = resolver.contacts_raw.get(peer_id)

    if contact is None:
        # MAX won't share extended profile for users that aren't in your
        # contact list. Show whatever we already know.
        name = resolver.users.get(peer_id)
        if name:
            await message.reply_text(
                f"<b>{escape(str(name))}</b>\nid: <code>{peer_id}</code>\n\n"
                "<i>MAX не отдал расширенный профиль для этого собеседника "
                "(скорее всего, он не у тебя в контактах).</i>",
                parse_mode="HTML",
            )
            return
        await message.reply_text(
            f"Не удалось получить профиль из MAX. id: <code>{peer_id}</code>",
            parse_mode="HTML",
        )
        return

    log.info("/profile contact raw fields: %s", list(contact.keys()))

    name = resolver.user_name(peer_id)
    phone = contact.get("phone") or ""
    about = (contact.get("description") or contact.get("about")
             or contact.get("status") or "")
    username = contact.get("link") or contact.get("username") or ""

    lines = [f"<b>{escape(str(name))}</b>",
             f"id: <code>{peer_id}</code>"]
    if phone:
        lines.append(f"📞 <code>{escape(str(phone))}</code>")
    if username:
        lines.append(f"🔗 {escape(str(username))}")
    if about:
        lines.append(f"\n{escape(str(about))}")
    body = "\n".join(lines)

    # Find a photo if any. MAX puts the avatar URL at the top level of the
    # contact dict as `baseUrl` / `baseRawUrl`, sometimes also wrapped in a
    # nested photo/avatar dict.
    photo_url = None
    photo_obj = contact.get("photo") or contact.get("avatar")
    if isinstance(photo_obj, dict):
        photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                     or photo_obj.get("photoUrl"))
    photo_url = (photo_url
                 or contact.get("baseUrl")
                 or contact.get("baseRawUrl")
                 or contact.get("photoUrl")
                 or contact.get("baseRawIconUrl"))

    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=data,
                    caption=body,
                    parse_mode="HTML",
                    message_thread_id=message.message_thread_id,
                )
                return
            except Exception:
                log.exception("send_photo failed in /profile")

    await message.reply_text(body, parse_mode="HTML")


def build_tg_app(token: str, max_client: PyMaxClient, supergroup_id: str,
                 topic_store: TopicStore, allowed_user_id: int | None = None,
                 proxy_url: str | None = None,
                 max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
                 allowed_user_ids: set[int] | frozenset[int] | None = None) -> Application:
    """Build the Telegram Application that routes topic replies back to Max.

    ``supergroup_id`` is kept only as the *default* group (bot-status
    messages, fallback target for brand-new unbound Max chats) — it is no
    longer the only group the bot will respond in. Commands and topic
    messages are accepted from *any* supergroup the bot is a member of, so
    you can add the bot to several Telegram groups and use /bind or /add in
    each one to route specific Max chats there.
    """
    request = HTTPXRequest(
        proxy=proxy_url,
        connect_timeout=TG_CONNECT_TIMEOUT,
        read_timeout=TG_FILE_TIMEOUT,
        write_timeout=TG_FILE_TIMEOUT,
        media_write_timeout=TG_FILE_TIMEOUT,
        pool_timeout=TG_CONNECT_TIMEOUT,
        socket_options=_TG_TCP_KEEPALIVE_OPTIONS,
    )
    # get_updates (long-polling) uses its own connection/request object in
    # PTB — separate from the one above, which is for regular bot-API calls
    # (sendMessage, sendPhoto, etc). Once .request() is set on the builder,
    # .get_updates_proxy() can no longer be used (PTB raises), so the proxy
    # has to be applied to a second request instance and passed via
    # .get_updates_request() instead — otherwise long-polling would silently
    # skip the proxy entirely.
    get_updates_request = HTTPXRequest(
        proxy=proxy_url,
        connect_timeout=TG_CONNECT_TIMEOUT,
        read_timeout=TG_CONNECT_TIMEOUT,
        pool_timeout=TG_CONNECT_TIMEOUT,
        socket_options=_TG_TCP_KEEPALIVE_OPTIONS,
    )
    builder = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
    )
    app = builder.build()
    app.bot_data[MAX_CLIENT_KEY] = max_client
    app.bot_data[TOPIC_STORE_KEY] = topic_store
    if allowed_user_ids:
        app.bot_data[ALLOWED_USER_KEY] = frozenset(map(int, allowed_user_ids))
    elif allowed_user_id:
        app.bot_data[ALLOWED_USER_KEY] = frozenset({int(allowed_user_id)})
    else:
        app.bot_data[ALLOWED_USER_KEY] = None
    app.bot_data[SUPERGROUP_KEY] = int(supergroup_id)
    app.bot_data[MAX_UPLOAD_BYTES_KEY] = max_upload_bytes

    # Any supergroup, not just the configured default — routing is decided
    # per-command (/bind, /add operate on whichever group they're called in)
    # and per-topic (TopicStore keys on (tg_chat_id, thread_id)).
    chat_filter = filters.ChatType.SUPERGROUP
    app.add_handler(CommandHandler("bind", _cmd_bind, filters=chat_filter))
    app.add_handler(CommandHandler("add", _cmd_add, filters=chat_filter))
    app.add_handler(CommandHandler("list", _cmd_list, filters=chat_filter))
    app.add_handler(CommandHandler("profile", _cmd_profile, filters=chat_filter))
    app.add_handler(CommandHandler("intro", _cmd_intro, filters=chat_filter))
    app.add_handler(CommandHandler("del", _cmd_del, filters=chat_filter))
    app.add_handler(CommandHandler("help", _cmd_help, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(_on_del_callback, pattern=r"^del:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, _on_topic_message)
    )
    media_filter = (
        filters.PHOTO | filters.VOICE | filters.AUDIO
        | filters.Document.ALL | filters.VIDEO | filters.VIDEO_NOTE
    )
    app.add_handler(
        MessageHandler(media_filter & chat_filter, _on_topic_media)
    )
    # Registered after the media handler, so anything it already covers
    # never gets here — this only catches attachment types the bridge
    # has no route for, which would otherwise vanish silently.
    app.add_handler(
        MessageHandler(
            filters.ATTACHMENT & ~media_filter & chat_filter,
            _on_unsupported_attachment,
        )
    )

    return app
