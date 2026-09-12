from __future__ import annotations

import base64
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from yarl import URL

from app.config import Settings
from app.pymax_auth import build_pymax_client
from pymax.exceptions import ApiError

log = logging.getLogger(__name__)

_PATCHED_API_ERROR = False


class _LenientNotReadyCode(str):
    """A string that also compares equal to the exact literal
    ``"attachment.not.ready"``, as long as it itself ends in
    ``".not.ready"``.

    Works around a maxapi-python bug (present through at least 2.4.1, the
    latest release on PyPI as of writing): right after uploading a voice
    or video attachment, MAX's server can still be processing it and
    rejects an immediate send with an ``attachment ... not.ready`` error.
    pymax already has the *correct* handling for this — wait for the
    server's "processing finished" signal for that same upload, then
    resend without re-uploading (see ``MessagesService._process_attachment_error``
    / ``send_message`` in ``pymax/api/messages/service.py``) — but it only
    triggers on an *exact* match against the old short error code
    ``"attachment.not.ready"``. The server now sends more specific codes
    (observed: ``errors.process.attachment.video.not.ready``, used for
    voice messages too, since pymax uploads them through the video
    pipeline) that never match, so that correct retry path silently never
    fires and the send just fails outright.

    Patching ``ApiError.__init__`` to wrap ``error`` in this class lets
    pymax's own comparison (``e.error == "attachment.not.ready"``) start
    matching the new codes too, so its existing wait-and-resend logic
    takes over exactly as designed — we don't reimplement or duplicate
    any of it.
    """

    def __eq__(self, other):
        if other == "attachment.not.ready":
            return self.endswith(".not.ready")
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)


def _patch_api_error_not_ready_matching() -> None:
    """Idempotent — safe to call multiple times (e.g. once per PyMaxClient
    instance); only patches ApiError.__init__ the first time."""
    global _PATCHED_API_ERROR
    if _PATCHED_API_ERROR:
        return
    original_init = ApiError.__init__

    def patched_init(self, *, error=None, **kwargs):
        if error is not None and not isinstance(error, _LenientNotReadyCode):
            error = _LenientNotReadyCode(error)
        original_init(self, error=error, **kwargs)

    ApiError.__init__ = patched_init
    _PATCHED_API_ERROR = True
    log.debug("Patched pymax ApiError for attachment-not-ready code matching")


_PATCHED_VOICE_READY_RESOLUTION = False


def _fixed_resolve_attach(frame):
    """Replacement for pymax's dispatch.resolvers.resolve_attach.

    Works around a second maxapi-python bug (present alongside the
    ApiError one above, same root cause: voice messages are uploaded
    through MAX's video pipeline). The server's "upload ready"
    notification for a voice message includes *both* a ``videoId`` and
    an ``audioId`` field — since ``VoiceAttachPayload`` extends
    ``VideoAttachPayload`` and keeps the same underlying id — but
    pymax's own resolver checks ``VideoUploadSignal`` (which only
    requires ``video_id`` and, thanks to ``CamelModel``'s
    ``extra="allow"``, validates successfully against that payload too)
    *before* ``AudioUploadSignal``. Every voice-ready notification
    therefore gets classified as ``VIDEO_READY`` instead of
    ``VOICE_READY``, so ``on_video_attach`` resolves the wrong waiter
    dict (``video_upload_waiters`` instead of ``voice_upload_waiters``)
    — the real wait in ``_process_attachment_error`` (see the ApiError
    patch above, which is what makes that wait actually get reached at
    all) then always times out after 60s with "Timed out waiting for
    video processing", and the voice message never sends.

    Checking ``AudioUploadSignal`` first fixes the ordering: a genuine
    video-ready payload only carries ``videoId`` (confirmed against
    pymax's own ``VideoUploadSignal``/``AudioUploadSignal`` models,
    which have no optional fields to fall back on), so it correctly
    fails ``AudioUploadSignal`` validation (missing required
    ``audio_id``) and falls through to ``VideoUploadSignal`` unchanged.
    """
    from pydantic import ValidationError
    from pymax.dispatch.enums import EventType
    from pymax.types import AudioUploadSignal
    from pymax.types.events import FileUploadSignal, VideoUploadSignal

    try:
        FileUploadSignal.model_validate(frame.payload)
        return EventType.FILE_READY
    except ValidationError:
        pass

    try:
        AudioUploadSignal.model_validate(frame.payload)
        return EventType.VOICE_READY
    except ValidationError:
        pass

    try:
        VideoUploadSignal.model_validate(frame.payload)
        return EventType.VIDEO_READY
    except ValidationError:
        pass

    return None


def _patch_voice_ready_resolution() -> None:
    """Idempotent — safe to call multiple times."""
    global _PATCHED_VOICE_READY_RESOLUTION
    if _PATCHED_VOICE_READY_RESOLUTION:
        return
    from pymax.dispatch import mapping
    from pymax.protocol import Opcode

    # EVENT_MAP is looked up fresh on every dispatched frame (see
    # EventResolver.resolve), so mutating this module-level dict in
    # place takes effect immediately — no need to patch anything that
    # might have already captured the old function by reference.
    mapping.EVENT_MAP[Opcode.NOTIF_ATTACH] = _fixed_resolve_attach
    _PATCHED_VOICE_READY_RESOLUTION = True
    log.debug("Patched pymax attach-ready event resolution for voice messages")

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
_BROWSER_HEADERS = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_HTTP_HEADERS = {**_BROWSER_HEADERS, "Origin": "https://web.max.ru", "Referer": "https://web.max.ru/", "Accept": "*/*"}
_ALLOWED_DOWNLOAD_HOSTS = frozenset({"i.oneme.ru", "oneme.ru", "web.max.ru", "max.ru"})
_ALLOWED_DOWNLOAD_SUFFIXES = (".oneme.ru", ".max.ru", ".okcdn.ru")


def _is_allowed_download_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (
        host in _ALLOWED_DOWNLOAD_HOSTS
        or any(host.endswith(suffix) for suffix in _ALLOWED_DOWNLOAD_SUFFIXES)
    )


def _redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        query = [(key, "<redacted>") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


@dataclass
class MaxMessage:
    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    cid: Any = None
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class MaxReadEvent:
    """A chat's read marker moved — either the peer read up to some point,
    or (set_as_unread=True) explicitly marked it unread again."""
    chat_id: Any = None
    user_id: Any = None
    mark: Any = None
    set_as_unread: bool = False


@dataclass
class MaxReactionEvent:
    """A message's reaction counters changed (someone added/removed a
    reaction emoji, e.g. 👀/👍, on a MAX message)."""
    chat_id: Any = None
    message_id: str = ""
    counters: list = field(default_factory=list)   # [{"reaction": "👍", "count": 2}, ...]
    total_count: int = 0


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _model_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return _plain(value)
    if hasattr(value, "model_dump"):
        try:
            return _plain(value.model_dump(by_alias=True, mode="json"))
        except UnicodeDecodeError:
            return _plain(value.model_dump(by_alias=True, mode="python"))
        except TypeError as exc:
            # pymax/pydantic models can occasionally end up with an
            # unresolved forward-ref schema (MockValSer) right after
            # startup, due to circular imports inside pymax itself
            # (it never calls model_rebuild()). Force a rebuild once
            # and retry; if that still fails, degrade gracefully
            # instead of crashing the whole listener.
            if "MockValSer" not in str(exc) and "SchemaSerializer" not in str(exc):
                raise
            model_cls = type(value)
            try:
                model_cls.model_rebuild(force=True)
                return _plain(value.model_dump(by_alias=True, mode="json"))
            except Exception:
                log.warning(
                    "model_dump failed for %s (unresolved pydantic schema); "
                    "falling back to raw attributes",
                    model_cls.__name__,
                )
                return _plain(
                    {k: v for k, v in vars(value).items() if not k.startswith("_")}
                )
    return _plain(vars(value))


def _attachment_to_dict(attach: Any) -> dict:
    data = _model_dict(attach)
    atype = data.get("_type") or data.get("type")
    if isinstance(atype, str):
        data["_type"] = atype
    if "base_url" in data and "baseUrl" not in data:
        data["baseUrl"] = data["base_url"]
    if "file_id" in data and "fileId" not in data:
        data["fileId"] = data["file_id"]
    if "video_id" in data and "videoId" not in data:
        data["videoId"] = data["video_id"]
    if "audio_id" in data and "audioId" not in data:
        data["audioId"] = data["audio_id"]
    if "photo_url" in data and "photoUrl" not in data:
        data["photoUrl"] = data["photo_url"]
    return data


def _message_from_pymax(message: Any, my_id: Any = None) -> MaxMessage | None:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return None

    sender_id = getattr(message, "sender", None)
    raw = _model_dict(message)
    return MaxMessage(
        chat_id=chat_id,
        sender_id=sender_id,
        text=getattr(message, "text", "") or "",
        timestamp=getattr(message, "time", None),
        message_id=str(getattr(message, "id", "")),
        is_self=bool(my_id is not None and sender_id == my_id),
        cid=getattr(message, "cid", None),
        attaches=[
            _attachment_to_dict(attach)
            for attach in (getattr(message, "attaches", None) or [])
        ],
        link=_model_dict(getattr(message, "link", None)),
        raw=raw,
    )


def _name_to_dict(name: Any) -> dict:
    data = _model_dict(name)
    if "first_name" in data and "firstName" not in data:
        data["firstName"] = data["first_name"]
    if "last_name" in data and "lastName" not in data:
        data["lastName"] = data["last_name"]
    return data


def _user_to_dict(user: Any) -> dict:
    data = _model_dict(user)
    names = getattr(user, "names", None)
    if names is not None:
        data["names"] = [_name_to_dict(name) for name in names]
    if "base_url" in data and "baseUrl" not in data:
        data["baseUrl"] = data["base_url"]
    if "base_raw_url" in data and "baseRawUrl" not in data:
        data["baseRawUrl"] = data["base_raw_url"]
    return data


def _chat_to_dict(chat: Any) -> dict:
    data = _model_dict(chat)
    if "base_icon_url" in data and "baseIconUrl" not in data:
        data["baseIconUrl"] = data["base_icon_url"]
    if "base_raw_icon_url" in data and "baseRawIconUrl" not in data:
        data["baseRawIconUrl"] = data["base_raw_icon_url"]
    return data


def _safe_chat_to_dict(chat: Any) -> dict | None:
    """``_chat_to_dict`` wrapped so one broken chat can't blank out the
    whole snapshot (and with it ``resolver.chats_raw``) — see ``/list``
    reporting no chats when ``_build_snapshot`` raised partway through."""
    try:
        return _chat_to_dict(chat)
    except Exception:
        log.exception(
            "Failed to convert chat id=%s to dict; skipping it, "
            "other chats will still load",
            getattr(chat, "id", "?"),
        )
        return None


def _safe_user_to_dict(user: Any) -> dict | None:
    try:
        return _user_to_dict(user)
    except Exception:
        log.exception(
            "Failed to convert contact id=%s to dict; skipping it",
            getattr(user, "id", "?"),
        )
        return None


class PyMaxClient:
    """Bridge client backed exclusively by PyMax."""

    MESSAGE_DEDUPE_MAX = 4096
    OUTBOUND_ECHO_TTL_SEC = 60 * 60
    OUTBOUND_ECHO_MAX = 4096
    MAX_CHAT_LIST_PAGES = 50   # safety cap on fetch_chats pagination at startup

    def __init__(self, settings: Settings):
        _patch_api_error_not_ready_matching()
        _patch_voice_ready_resolution()
        self.settings = settings
        self.max_download_bytes = settings.max_download_mb * 1024 * 1024
        self.chat_ids: list[int] = []
        if settings.max_chat_ids:
            self.chat_ids += map(int, map(str.strip, settings.max_chat_ids.split(",")))
        self.ignore_chat_ids: list[int] = []
        if settings.max_ignore_chat_ids:
            self.ignore_chat_ids += map(
                int, map(str.strip, settings.max_ignore_chat_ids.split(","))
            )

        self._on_qr_cb = None
        self._client = build_pymax_client(settings, self)
        self._my_id: Any = None
        self._is_connected = False
        self._on_ready_cb = None
        self._on_message_cb = None
        self._on_disconnect_cb = None
        self._on_read_cb = None
        self._on_reaction_cb = None
        self._outbound_cids: OrderedDict[tuple[Any, str], float] = OrderedDict()
        self.resolver = None
        self.last_message_ids: dict[Any, str] = {}

        self._wire_events()

    @property
    def raw_client(self):
        return self._client

    @property
    def my_id(self) -> Any:
        return self._my_id

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    def on_read(self, func):
        self._on_read_cb = func
        return func

    def on_reaction(self, func):
        self._on_reaction_cb = func
        return func

    def on_qr(self, func):
        """Register a callback fired with (qr_url, png_bytes) whenever a
        fresh MAX login QR code is generated — lets the app broadcast it
        somewhere (e.g. Telegram) instead of only logging it."""
        self._on_qr_cb = func
        return func

    async def notify_qr(self, qr_url: str, png_bytes: bytes) -> None:
        """Called by the QR auth handler (see app/pymax_auth.py) — not
        part of the on_start/on_message event wiring since it can fire
        before the pymax client has even finished connecting."""
        if not self._on_qr_cb:
            return
        try:
            await self._on_qr_cb(qr_url, png_bytes)
        except Exception:
            log.exception("on_qr callback failed")

    def _mark_outbound_cid(self, chat_id: Any, cid: Any) -> None:
        if cid is None:
            return
        now = time.monotonic()
        cutoff = now - self.OUTBOUND_ECHO_TTL_SEC
        while self._outbound_cids:
            key, ts = next(iter(self._outbound_cids.items()))
            if ts >= cutoff:
                break
            self._outbound_cids.pop(key, None)
        self._outbound_cids[(chat_id, str(cid))] = now
        while len(self._outbound_cids) > self.OUTBOUND_ECHO_MAX:
            self._outbound_cids.popitem(last=False)

    def is_bridge_echo(self, msg: MaxMessage) -> bool:
        """True if this is an echo of a message the bridge itself just sent
        (same chat_id + cid we recorded on send), as opposed to a message
        typed manually on your own phone/other MAX client (also is_self,
        but not tracked here — so it's forwarded, not dropped)."""
        if msg.cid is None:
            return False
        return (msg.chat_id, str(msg.cid)) in self._outbound_cids

    def _wire_events(self) -> None:
        @self._client.on_start()
        async def _handle_start(pymax_client):
            self._my_id = self._extract_my_id(pymax_client)
            self._is_connected = True
            await self._fetch_all_chats(pymax_client)
            snapshot = self._build_snapshot(pymax_client)
            await self._add_configured_chats(snapshot)
            if self._on_ready_cb:
                await self._on_ready_cb(snapshot)

        @self._client.on_message()
        async def _handle_message(message, pymax_client):
            bridge_message = _message_from_pymax(message, self._my_id)
            if bridge_message is None:
                return
            if bridge_message.message_id:
                self.last_message_ids[bridge_message.chat_id] = bridge_message.message_id
            if self.chat_ids and bridge_message.chat_id not in self.chat_ids:
                return
            if self.ignore_chat_ids and bridge_message.chat_id in self.ignore_chat_ids:
                return
            if self._on_message_cb:
                await self._on_message_cb(bridge_message)

        @self._client.on_disconnect()
        async def _handle_disconnect(exc, reconnect, delay):
            self._is_connected = False
            log.warning(
                "PyMax disconnected: %s; reconnect=%s delay=%s",
                exc,
                reconnect,
                delay,
            )
            if self._on_disconnect_cb:
                await self._on_disconnect_cb()

        @self._client.on_message_read()
        async def _handle_message_read(event, pymax_client):
            if self._on_read_cb:
                await self._on_read_cb(MaxReadEvent(
                    chat_id=getattr(event, "chat_id", None),
                    user_id=getattr(event, "user_id", None),
                    mark=getattr(event, "mark", None),
                    set_as_unread=bool(getattr(event, "set_as_unread", False)),
                ))

        @self._client.on_reaction_update()
        async def _handle_reaction_update(event, pymax_client):
            if self._on_reaction_cb:
                counters = getattr(event, "counters", None) or []
                await self._on_reaction_cb(MaxReactionEvent(
                    chat_id=getattr(event, "chat_id", None),
                    message_id=str(getattr(event, "message_id", "")),
                    counters=[
                        {"reaction": getattr(c, "reaction", "?"), "count": getattr(c, "count", 1)}
                        for c in counters
                    ],
                    total_count=getattr(event, "total_count", 0),
                ))

    async def run(self):
        await self._client.start()

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close:
            await close()

    async def stop(self) -> None:
        stop = getattr(self._client, "stop", None)
        if stop:
            await stop()
            return
        await self.close()

    async def fetch_chat(self, chat_id) -> dict:
        try:
            chat = await self._client.get_chat(int(chat_id))
        except Exception as exc:
            log.warning("PyMax fetch_chat failed for %s: %s", chat_id, exc)
            return {"_max_error": {"message": str(exc)}}
        return {"chat": _chat_to_dict(chat)} if chat else {}

    async def fetch_contacts(self, contact_ids: list[int]) -> dict:
        if not contact_ids:
            return {}
        users = await self._client.get_users(contact_ids)
        return {"contacts": [_user_to_dict(user) for user in users if user]}

    async def resolve_file_url(self, chat_id, message_id, file_id) -> str | None:
        result = await self._client.get_file_by_id(int(chat_id), int(message_id), int(file_id))
        url = _model_dict(result).get("url")
        return url if isinstance(url, str) else None

    async def send_message(
        self,
        chat_id,
        text: str = "",
        elements=None,
        attaches=None,
    ) -> dict:
        try:
            message = await self._client.send_message(
                int(chat_id),
                text=text or None,
                attachments=attaches or None,
                notify=True,
            )
        except Exception as exc:
            log.exception("PyMax send_message failed for chat %s", chat_id)
            return {"_max_error": {"message": str(exc)}}
        sent_cid = getattr(message, "cid", None)
        if sent_cid is not None:
            self._mark_outbound_cid(chat_id, sent_cid)
        return _model_dict(message) or {"ok": True}

    async def read_message(self, chat_id, message_id) -> bool:
        """Mark the MAX chat as read up to (and including) message_id —
        triggers the same 'прочитано' state on the peer's side as opening
        the chat manually in a real MAX client would.

        message_id MUST be passed to pymax as an int, not a str. Our
        MaxMessage.message_id is a str (see _message_from_pymax), and
        pymax's ReadMessagesPayload.message_id is typed as ``str | int``
        (with a comment noting the socket actually wants a number) — a
        str value passes pydantic validation as-is and gets serialized
        as a JSON string, which the MAX server rejects outright with a
        generic "Ошибка валидации / Expected number at <n>" — and pymax
        treats that as a fatal, non-retryable protocol error that tears
        down and reconnects the whole websocket connection, not just a
        failed read_message call.
        """
        try:
            await self._client.read_message(int(message_id), int(chat_id))
        except Exception:
            log.exception(
                "PyMax read_message failed for chat_id=%s message_id=%s",
                chat_id, message_id,
            )
            return False
        return True

    async def upload_photo(
        self,
        data: bytes,
        chat_id=None,
        filename: str = "image.jpg",
        mimetype: str = "image/jpeg",
    ):
        from pymax import Photo

        return Photo(raw=data, name=filename)

    async def upload_file(
        self,
        data: bytes,
        chat_id=None,
        filename: str = "file.bin",
        mimetype: str = "application/octet-stream",
        attach_type: str = "FILE",
        timeout: float = 60.0,
    ):
        from pymax import File

        return File(raw=data, name=filename)

    async def upload_video(
        self,
        data: bytes,
        chat_id=None,
        filename: str = "video.mp4",
        mimetype: str = "video/mp4",
        timeout: float = 60.0,
    ):
        from pymax import Video

        return Video(raw=data, name=filename)

    async def upload_audio(
        self,
        data: bytes,
        chat_id=None,
        filename: str = "voice.ogg",
        mimetype: str = "audio/ogg",
        duration: int | None = None,
        timeout: float = 60.0,
    ):
        from pymax import Voice

        return Voice(raw=data, name=filename, duration=duration)

    async def open_by_link(self, link: str) -> dict:
        try:
            chat = await self._client.join_group(link)
        except ValueError:
            try:
                chat = await self._client.join_channel(link)
            except Exception as exc:
                log.exception("PyMax open_by_link failed: %s", _redact_url(link))
                return {"_max_error": {"message": str(exc)}}
        except Exception as exc:
            log.exception("PyMax open_by_link failed: %s", _redact_url(link))
            return {"_max_error": {"message": str(exc)}}
        return {"chatId": getattr(chat, "id", None), "chat": _chat_to_dict(chat)}

    async def download_audio_url(
        self,
        audio_id,
        chat_id,
        message_id,
        token: str | None = None,
    ) -> str | None:
        if token:
            return f"https://i.oneme.ru/i?r={token}"
        return None

    async def download_video_url(self, video_id, chat_id, message_id) -> str | None:
        video = await self._client.get_video_by_id(
            int(chat_id),
            int(message_id),
            int(video_id),
        )
        data = _model_dict(video)
        url = data.get("url")
        return url if isinstance(url, str) else None

    async def download_file(self, url: str) -> bytes | None:
        if not _is_allowed_download_url(url):
            log.warning("Blocked download from disallowed URL: %s", _redact_url(url)[:120])
            return None

        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        is_okcdn = host == "okcdn.ru" or host.endswith(".okcdn.ru")
        if is_okcdn:
            session = aiohttp.ClientSession(headers={"User-Agent": _USER_AGENT})
            request_headers = {}
            request_url = URL(url, encoded=True)
        else:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            request_headers = _HTTP_HEADERS
            request_url = url
        try:
            async with session.get(
                request_url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    error_body = (await resp.text(errors="replace"))[:200]
                    log.warning(
                        "Download failed %s - HTTP %d: %r",
                        _redact_url(url)[:120],
                        resp.status,
                        error_body,
                    )
                    return None

                declared = resp.headers.get("Content-Length")
                try:
                    declared_size = int(declared) if declared else None
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > self.max_download_bytes:
                    log.warning(
                        "Blocked oversized download %s: %s bytes > %s bytes",
                        _redact_url(url)[:120],
                        declared_size,
                        self.max_download_bytes,
                    )
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        log.warning(
                            "Blocked oversized streaming download %s: %d bytes > %d bytes",
                            _redact_url(url)[:120],
                            total,
                            self.max_download_bytes,
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception:
            log.exception("Download error: %s", _redact_url(url)[:120])
            return None
        finally:
            await session.close()

    async def _fetch_all_chats(self, pymax_client) -> None:
        """Page through the complete MAX chat list before building the snapshot.

        PyMax's login/sync only returns a limited window of the most
        recently *active* chats (the underlying MAX server applies its own
        default page size when the client doesn't request a specific count
        — historically as low as ~10 in the raw WS protocol this bridge
        used before migrating to PyMax). A group or channel you haven't
        posted in recently can be completely absent from
        ``pymax_client.chats`` right after login — which is exactly why
        ``/list`` (and topic auto-creation for anything not yet messaged)
        could report "no chats" even though the account has plenty.

        ``Client.fetch_chats(marker=...)`` pages backward in time and, per
        PyMax's own ``ChatService._cache_chat``, merges every chat it sees
        straight into ``pymax_client.chats`` — so nothing further needs to
        be merged manually here; ``_build_snapshot`` just needs to run
        after this completes. Pagination continues purely on "did this page
        have anything, and did the marker move further back" — NOT on
        whether the page introduced any chat *not already known*, since an
        early page can legitimately overlap entirely with what login-sync
        already returned while older, still-undiscovered chats remain
        further back.
        """
        fetch_chats = getattr(pymax_client, "fetch_chats", None)
        if fetch_chats is None:
            return

        total_seen = {chat.id for chat in (getattr(pymax_client, "chats", None) or [])}
        marker = None
        for _ in range(self.MAX_CHAT_LIST_PAGES):
            try:
                page = await fetch_chats(marker=marker)
            except Exception:
                log.exception("PyMax fetch_chats page failed (marker=%s)", marker)
                break
            if not page:
                break

            total_seen.update(chat.id for chat in page)
            event_times = [getattr(chat, "last_event_time", 0) for chat in page]
            event_times = [t for t in event_times if t]
            oldest = min(event_times) if event_times else None
            if not oldest or oldest <= 0:
                break

            next_marker = oldest - 1
            if marker is not None and next_marker >= marker:
                break  # marker isn't moving further back — avoid looping forever
            marker = next_marker

        log.info("PyMax full chat list loaded: %d chats total", len(total_seen))

    def _extract_my_id(self, pymax_client) -> Any:
        me = getattr(pymax_client, "me", None)
        contact = getattr(me, "contact", None)
        return getattr(contact, "id", None)

    def _build_snapshot(self, pymax_client) -> dict:
        me = getattr(pymax_client, "me", None)
        contact = getattr(me, "contact", None)
        chats = getattr(pymax_client, "chats", None) or []
        contacts = getattr(pymax_client, "contacts", None) or []
        try:
            profile = _user_to_dict(contact) if contact is not None else {}
        except Exception:
            log.exception("Failed to convert own profile to dict")
            profile = {}
        return {
            "profile": profile,
            "chats": [d for d in (_safe_chat_to_dict(chat) for chat in chats if chat) if d],
            "contacts": [d for d in (_safe_user_to_dict(user) for user in contacts if user) if d],
        }

    async def _add_configured_chats(self, snapshot: dict) -> None:
        """Fetch filtered chats omitted from PyMax's incremental login sync."""
        if not self.chat_ids:
            return

        known_ids = {chat.get("id") for chat in snapshot["chats"]}
        missing_ids = [chat_id for chat_id in self.chat_ids if chat_id not in known_ids]
        if not missing_ids:
            return

        try:
            chats = await self._client.get_chats(missing_ids)
        except Exception:
            log.exception("PyMax failed to fetch configured chats: %s", missing_ids)
            return

        snapshot["chats"].extend(
            d for d in (_safe_chat_to_dict(chat) for chat in chats if chat) if d
        )
