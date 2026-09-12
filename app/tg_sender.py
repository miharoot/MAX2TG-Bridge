import asyncio
import io
import logging

from telegram import Bot, InputFile, InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from app.topics import TopicStore

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
TG_TOPIC_NAME_MAX = 128
MAX_RETRIES = 3
TG_CONNECT_TIMEOUT = 20.0
TG_REQUEST_TIMEOUT = 180.0


def _looks_numeric(title: str) -> bool:
    """A title is 'placeholder' when it carries no human-readable name yet."""
    title = (title or "").strip()
    return not title or title.isdigit() or title.startswith("DM:")


class TelegramSender:
    def __init__(self, token: str, default_chat_id: str, topic_store: TopicStore,
                 proxy_url: str | None = None, chat_routes: dict[str, int] | None = None,
                 max_upload_bytes: int | None = None):
        request = HTTPXRequest(
            proxy=proxy_url,
            connect_timeout=TG_CONNECT_TIMEOUT,
            read_timeout=TG_REQUEST_TIMEOUT,
            write_timeout=TG_REQUEST_TIMEOUT,
            media_write_timeout=TG_REQUEST_TIMEOUT,
            pool_timeout=TG_CONNECT_TIMEOUT,
        )
        self._bot = Bot(token=token, request=request)
        self._default_chat_id = str(default_chat_id)
        self._topics = topic_store
        self._chat_routes = {str(k): int(v) for k, v in (chat_routes or {}).items()}
        self._topic_lock = asyncio.Lock()
        # Ceiling for anything uploaded *to* Telegram (TG_UPLOAD_MB). The
        # Bot API refuses files past its own limit, and a MAX chat can
        # legitimately carry something bigger than Telegram will take —
        # MAX_DOWNLOAD_MB governs what we pull from MAX, which is a
        # different question. None disables the check.
        self._max_upload_bytes = max_upload_bytes or None

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def chat_id(self) -> str:
        """Default/fallback Telegram chat — used for bot-status messages and
        as the target for Max chats that have no explicit route yet."""
        return self._default_chat_id

    @property
    def topic_store(self) -> TopicStore:
        return self._topics

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    # ── routing ─────────────────────────────────────────────────────

    def resolve_chat_id(self, max_chat_id) -> str:
        """Which Telegram supergroup a given Max chat should be forwarded to.

        Priority: (1) an existing topic binding (TopicStore) — this is the
        source of truth once a topic exists, since /bind may have created it
        in a non-default group; (2) a static MAX_CHAT_ROUTES entry, for
        chats that should land in a specific group the very first time a
        message arrives; (3) the configured default group.
        """
        bound = self._topics.get_chat_id(max_chat_id)
        if bound is not None:
            return str(bound)
        routed = self._chat_routes.get(str(max_chat_id))
        if routed is not None:
            return str(routed)
        return self._default_chat_id

    def all_known_chat_ids(self) -> set[int]:
        """Every distinct Telegram supergroup the bridge currently knows
        about: groups with at least one bound topic, groups pre-configured
        via MAX_CHAT_ROUTES, and the default group."""
        ids = set(self._topics.all_tg_chat_ids())
        ids.update(self._chat_routes.values())
        ids.add(int(self._default_chat_id))
        return ids

    async def broadcast(self, text: str) -> None:
        """Send a status message (General topic) to every Telegram group
        the bridge is routing to — not just the default one. Used for
        bot-wide notices like MAX connection loss/recovery, since with
        multi-group routing a person watching only a non-default group
        would otherwise never see them."""
        for chat_id in self.all_known_chat_ids():
            await self.send(text, chat_id=chat_id)

    async def broadcast_photo(self, data: bytes, caption: str = "",
                              filename: str = "photo.jpg") -> None:
        """Same as broadcast() but for a photo — e.g. the MAX login QR
        code, so it reaches every routed group as an actually-scannable
        image instead of only ASCII art in the server log."""
        for chat_id in self.all_known_chat_ids():
            await self.send_photo(data, caption=caption, filename=filename, chat_id=chat_id)

    async def set_reaction(self, chat_id: str | int, message_id: int, emoji: str) -> bool:
        """Best-effort: put a single emoji reaction on a Telegram message.

        Used to mirror a MAX "read" event (✅) onto the last message we
        forwarded into that chat's topic — matching the existing pattern
        where a Telegram→MAX reply gets a 👀 reaction once MAX confirms
        delivery. Returns False (and logs at debug level) on any failure —
        e.g. the message is too old for Telegram to accept a reaction on,
        or the bot lacks permission — since a missed reaction shouldn't be
        treated as a hard error.
        """
        try:
            await self._bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=emoji,
            )
            return True
        except Exception:
            log.debug("Could not set reaction %r on message %s in %s",
                     emoji, message_id, chat_id, exc_info=True)
            return False

    # ── forum topics ───────────────────────────────────────────────

    async def ensure_topic(self, max_chat_id, title: str, *, force_rename: bool = False) -> int | None:
        """Return the Telegram forum topic (thread) ID for a Max chat.

        Creates the topic (in the resolved target group — see
        ``resolve_chat_id``) on first use. If a previously created topic
        carries a different name, it's renamed when either the stored title
        looks like a placeholder (numeric ID or a "DM:" fallback) and the
        new one doesn't, or when ``force_rename=True`` — used by the caller
        once it has *confirmed* (via a live MAX lookup, not just a guess)
        what the chat is actually called, e.g. a group chat resolved after
        being created at runtime. Returns None if topic creation fails —
        callers then fall back to the General topic.
        """
        title = (title or str(max_chat_id)).strip()[:TG_TOPIC_NAME_MAX]
        target_chat_id = self.resolve_chat_id(max_chat_id)

        existing = self._topics.get_topic(max_chat_id)
        if existing is not None:
            stored = self._topics.get_title(max_chat_id) or ""
            should_rename = bool(title) and title != stored and (
                force_rename or (_looks_numeric(stored) and not _looks_numeric(title))
            )
            if should_rename:
                try:
                    await self._bot.edit_forum_topic(
                        chat_id=target_chat_id, message_thread_id=existing, name=title
                    )
                    self._topics.update_title(max_chat_id, title)
                    log.info("Renamed forum topic %s → %r", existing, title)
                except Exception:
                    log.exception("Failed to rename forum topic %s", existing)
            return existing

        async with self._topic_lock:
            existing = self._topics.get_topic(max_chat_id)
            if existing is not None:
                return existing
            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=target_chat_id, name=title
                )
            except Exception:
                log.exception(
                    "Failed to create forum topic for Max chat %s in %s — is the "
                    "supergroup a forum and is the bot an admin with 'Manage Topics'?",
                    max_chat_id, target_chat_id,
                )
                return None
            thread_id = topic.message_thread_id
            self._topics.set_topic(max_chat_id, int(target_chat_id), thread_id, title)
            log.info("Created forum topic %r (chat=%s thread=%s) for Max chat %s",
                     title, target_chat_id, thread_id, max_chat_id)
            return thread_id

    # ── helpers ────────────────────────────────────────────────────

    def _truncate_caption(self, text: str) -> str:
        if len(text) > TG_CAPTION_MAX:
            return text[: TG_CAPTION_MAX - 20] + "\n\n[...усечено]"
        return text

    def _too_large(self, data: bytes, what: str) -> bool:
        """True if Telegram would reject this upload for its size.

        Refusing here, before the request, turns a doomed Bot API call
        into a caller-visible ``None`` — every send_* caller in
        app/max_listener.py already treats that as "couldn't send" and
        posts a text fallback naming the attachment, so an oversized file
        is announced in the topic instead of vanishing."""
        if self._max_upload_bytes and len(data) > self._max_upload_bytes:
            log.warning(
                "Not uploading %s to Telegram: %d bytes exceeds the "
                "TG_UPLOAD_MB limit of %d bytes",
                what, len(data), self._max_upload_bytes,
            )
            return True
        return False

    async def _retry(self, coro_factory):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RetryAfter as e:
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except Exception:
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(2 * attempt)
        return None

    # ── send methods ───────────────────────────────────────────────
    # Every send_* method accepts an explicit ``chat_id``; callers that don't
    # care about routing (e.g. bot-status notifications) can omit it and it
    # falls back to the configured default group.

    async def send(self, text: str, message_thread_id: int | None = None,
                    chat_id: str | int | None = None):
        if not text:
            return None

        if len(text) > TG_MAX_LENGTH:
            text = text[: TG_MAX_LENGTH - 20] + "\n\n[...усечено]"

        return await self._retry(
            lambda: self._bot.send_message(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_photo(self, data: bytes, caption: str = "", filename: str = "photo.jpg",
                         message_thread_id: int | None = None,
                         chat_id: str | int | None = None):
        if self._too_large(data, f"photo {filename!r}"):
            return None
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_photo(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_document(self, data: bytes, caption: str = "", filename: str = "file",
                            message_thread_id: int | None = None,
                            chat_id: str | int | None = None):
        if self._too_large(data, f"document {filename!r}"):
            return None
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_document(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_video(self, data: bytes, caption: str = "", filename: str = "video.mp4",
                         message_thread_id: int | None = None,
                         chat_id: str | int | None = None):
        if self._too_large(data, f"video {filename!r}"):
            return None
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_video(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_voice(self, data: bytes, caption: str = "",
                         message_thread_id: int | None = None,
                         chat_id: str | int | None = None):
        if self._too_large(data, "voice message"):
            return None
        caption = self._truncate_caption(caption)
        target = chat_id if chat_id is not None else self._default_chat_id
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=target,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            result = await self._retry(
                lambda: self._bot.send_audio(
                    chat_id=target,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                )
            )
        return result

    async def send_sticker(self, data: bytes, message_thread_id: int | None = None,
                           chat_id: str | int | None = None):
        if self._too_large(data, "sticker"):
            return None
        return await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                message_thread_id=message_thread_id,
            )
        )

    async def send_media_group(
        self,
        items: list[tuple[str, bytes, str]],
        caption: str = "",
        message_thread_id: int | None = None,
        chat_id: str | int | None = None,
    ):
        """Send photos/videos or documents as Telegram albums, max 10 per
        group. Returns the last sent Message (so callers can track it for
        read-receipt mirroring, same as plain text sends), or None if every
        chunk failed to send."""
        caption = self._truncate_caption(caption)
        target = chat_id if chat_id is not None else self._default_chat_id
        last_message = None
        items = [item for item in items
                 if not self._too_large(item[1], f"album item {item[2]!r}")]
        for offset in range(0, len(items), 10):
            chunk = items[offset:offset + 10]

            def build_media():
                media = []
                for index, (kind, data, filename) in enumerate(chunk):
                    kwargs = {
                        "media": InputFile(data, filename=filename, attach=True),
                        "caption": caption if index == 0 else None,
                        "parse_mode": ParseMode.HTML if index == 0 else None,
                    }
                    if kind == "photo":
                        media.append(InputMediaPhoto(**kwargs))
                    elif kind == "video":
                        media.append(InputMediaVideo(**kwargs))
                    else:
                        media.append(InputMediaDocument(**kwargs))
                return media

            result = await self._retry(
                lambda: self._bot.send_media_group(
                    chat_id=target,
                    media=build_media(),
                    message_thread_id=message_thread_id,
                )
            )
            if result:
                last_message = result[-1]
        return last_message
