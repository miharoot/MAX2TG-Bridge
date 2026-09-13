from __future__ import annotations

import asyncio
import base64
import contextlib
import re
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
from pymax.exceptions import ApiError, UploadError

log = logging.getLogger(__name__)

# How long to wait for MAX to answer a contact lookup before giving up
# (see PyMaxClient.fetch_contacts): it can stay silent indefinitely.
USER_LOOKUP_TIMEOUT = 15

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


_PATCHED_ATTACHMENT_WAIT_TIMEOUT = False
_ATTACHMENT_READY_WAIT_SECONDS = 8
_ATTACHMENT_READY_MAX_ATTEMPTS = 5


async def _short_wait_for_upload_signal(self, waiters, video_id) -> None:
    """Replacement for pymax's ``MessageService._wait_for_upload_signal``.

    Third bug in the same voice-upload chain as the two patches above,
    but this one is on MAX's server side rather than pymax's: production
    debug logs (checked across several independent captures) show the
    "attachment ready" push notification (``NOTIF_ATTACH``, opcode 136)
    is simply never sent for these voice uploads — the wait always runs
    the full default 60 seconds and times out with "Timed out waiting
    for video processing notification", never resolved by an actual
    event. The two patches above make pymax *capable* of reacting
    correctly to that notification; they can't make MAX's server send
    one that never arrives.

    Waiting a full minute per attempt for a signal that structurally
    never comes just wastes time — shortened here to a few seconds so
    that ``PyMaxClient.send_message``'s own retry loop (see
    ``_ATTACHMENT_READY_MAX_ATTEMPTS``) gets several fresh attempts
    (each a full re-upload + resend, since the token from a "not ready"
    attempt cannot be reused) within roughly the same overall time
    budget the old single 60s wait used to spend on one doomed attempt.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    waiters[video_id] = future
    try:
        await asyncio.wait_for(future, timeout=_ATTACHMENT_READY_WAIT_SECONDS)
    except TimeoutError:
        log.warning(
            "Timed out waiting for attachment processing notification "
            "video_id=%s (shortened %ss wait — MAX never sent a ready "
            "signal for this upload)",
            video_id, _ATTACHMENT_READY_WAIT_SECONDS,
        )
        raise UploadError(f"Timed out waiting for video processing video_id={video_id}")
    finally:
        waiters.pop(video_id, None)


def _patch_attachment_wait_timeout() -> None:
    """Idempotent — safe to call multiple times."""
    global _PATCHED_ATTACHMENT_WAIT_TIMEOUT
    if _PATCHED_ATTACHMENT_WAIT_TIMEOUT:
        return
    from pymax.api.messages.service import MessageService

    MessageService._wait_for_upload_signal = _short_wait_for_upload_signal
    _PATCHED_ATTACHMENT_WAIT_TIMEOUT = True
    log.debug(
        "Patched pymax attachment-ready wait timeout to %ss",
        _ATTACHMENT_READY_WAIT_SECONDS,
    )


_AUDIO_CONTENT_TYPES = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def _audio_content_type(name: str) -> str:
    """Content-Type for an audio upload, from its file extension."""
    suffix = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return _AUDIO_CONTENT_TYPES.get(f".{suffix}", "application/octet-stream")


def _upload_user_agent(config) -> str:
    """The ``User-Agent`` to send with an upload HTTP request.

    pymax builds this as ``OKMessages/{config.app_version} (...)`` — the
    shape its *Android* client uses. On a web session (which is what this
    bridge runs) that is wrong twice over: ``config.app_version`` is the
    Android-client field and is ``None`` there, so the header literally
    went out as ``OKMessages/None (Linux; Chrome; ...)``, and a real web
    client would send its browser User-Agent anyway. MAX answered such an
    upload with ``{"error_code":"4","error_data":"BAD_REQUEST"}``.

    So: send the same ``headerUserAgent`` the session already introduced
    itself with during the handshake when there is one (web), and fall
    back to the OKMessages form otherwise — with a version that actually
    exists, preferring the user-agent payload's own ``app_version`` over
    the possibly-unset client-level one.
    """
    user_agent = getattr(getattr(config, "device", None), "user_agent", None)

    header_ua = getattr(user_agent, "header_user_agent", None)
    if header_ua:
        return header_ua

    version = (
        getattr(user_agent, "app_version", None)
        or getattr(config, "app_version", None)
        or ""
    )
    return (
        f"OKMessages/{version}"
        f" ({getattr(user_agent, 'os_version', '')};"
        f" {getattr(user_agent, 'device_name', '')};"
        f" {getattr(user_agent, 'screen', '')})"
    )


# How the multipart file part is labelled, tried in this order. MAX's
# audio endpoint is undocumented, so these are the plausible spellings
# of the same Opus-in-OGG payload; see _voice_upload_variants.
# How to re-package the recording before uploading, tried in this order.
# (variant name, ffmpeg arguments, filename, content type); ffmpeg args
# of None means "send the Telegram file untouched".
#
# Settled against the live MAX server. Telegram voice notes are already
# Opus in an OGG container, yet MAX answers AUDIO_VALIDATION_FAILED for
# them however the upload is framed or labelled — and equally for a
# WebM/Opus remux or re-encode. What it does accept is the same audio
# re-encoded to plain 48 kHz mono Opus, so the container was never the
# problem: MAX objects to something about how Telegram encodes its
# recordings. Re-encoding is therefore the normal path, with the
# untouched file left as the fallback for hosts with no ffmpeg.
_VOICE_UPLOAD_FORMATS = (
    (
        "ogg-opus-reencode",
        ["-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", "-f", "ogg"],
        "voice.ogg",
        "audio/ogg",
    ),
    ("original-ogg", None, None, None),
)


def _ffmpeg_executable() -> str | None:
    """Path to an ffmpeg binary, or ``None`` if there isn't one.

    Prefers a system ffmpeg: the ``imageio-ffmpeg`` wheel's binary is
    glibc-linked and will not run on the musl-based Docker image, which
    installs ffmpeg through apk instead. On a plain host install (no
    container) the wheel is what supplies it.
    """
    import shutil

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        log.debug("imageio-ffmpeg could not provide an ffmpeg binary: %s", e)
        return None


async def _transcode_audio(body: bytes, args: list[str]) -> bytes | None:
    """Re-package audio with ffmpeg, or ``None`` if that isn't possible.

    Returns ``None`` rather than raising when ffmpeg is missing or fails,
    so a deployment without it (or a stream ffmpeg cannot read) just
    falls through to the remaining upload variants.
    """
    import asyncio

    executable = _ffmpeg_executable()
    if executable is None:
        log.warning("ffmpeg is not available, sending the recording as-is")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", *args, "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        log.warning("ffmpeg could not be started, sending the recording as-is: %s", e)
        return None

    try:
        out, err = await asyncio.wait_for(proc.communicate(body), timeout=60)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("ffmpeg timed out re-packaging the recording")
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None

    if proc.returncode != 0 or not out:
        log.warning(
            "ffmpeg could not re-package the recording (rc=%s): %s",
            proc.returncode, err.decode("utf-8", "replace")[:300],
        )
        return None

    return out


async def _voice_upload_variants(name, body, content_type):
    """The payloads to try when POSTing a voice recording to MAX.

    Two rounds of production sweeps narrowed this down. The *envelope*
    is settled: pymax posts the raw body with a ``Content-Range``
    (copied from its video upload) and MAX answers every spelling of
    that with ``{"error_code":"4","error_data":"BAD_REQUEST"}``, while a
    **multipart form** — how pymax's working *photo* upload posts — gets
    ``{"error_code":"1","error_data":"AUDIO_VALIDATION_FAILED"}``
    instead. A different error means the request was understood and got
    as far as inspecting the audio.

    The *labelling* is ruled out too: every field name / filename /
    content-type spelling of the multipart part gets the same
    AUDIO_VALIDATION_FAILED. What MAX objects to is the recording
    itself — and not its container either, since a WebM/Opus remux and
    re-encode are refused just the same while plain 48 kHz mono Opus
    back in OGG is accepted. See ``_VOICE_UPLOAD_FORMATS``.

    Yields ``(variant_name, form)``. Yielding is lazy on purpose: each
    variant may have to shell out to ffmpeg, and there is no point
    paying for that once MAX has accepted an earlier one.
    """
    for variant, ffmpeg_args, filename, part_type in _VOICE_UPLOAD_FORMATS:
        if ffmpeg_args is None:
            payload, part_name, part_ct = body, name, content_type
        else:
            payload = await _transcode_audio(body, ffmpeg_args)
            if payload is None:
                log.debug("Skipping voice upload variant=%s (ffmpeg unavailable)", variant)
                continue
            part_name, part_ct = filename, part_type

        form = aiohttp.FormData()
        form.add_field(name="file", value=payload, filename=part_name, content_type=part_ct)
        yield variant, form


async def _request_voice_upload_slot(app):
    """Ask MAX for a fresh audio upload URL (and its id/token).

    Each upload attempt needs its own: MAX appears to burn the slot on a
    rejected POST, so replaying a second shape against the same ``cid``
    only ever answers BAD_REQUEST — which would make trying several
    shapes meaningless.
    """
    from pymax.api.uploads.models import VideoUploadResponse
    from pymax.api.uploads.payloads import UploadPayload
    from pymax.protocol import Opcode

    payload = UploadPayload(type=2, uploader_type=1).to_payload()
    try:
        data = await app.invoke(Opcode.VIDEO_UPLOAD, payload=payload)
    except Exception as e:
        raise UploadError("Failed to request voice upload URL") from e

    try:
        return VideoUploadResponse.model_validate(data.payload).info[0]
    except IndexError as e:
        raise UploadError("voice upload response info is empty") from e
    except Exception as e:
        raise UploadError("Invalid voice upload response model") from e


class VoiceRejectedByMax(UploadError):
    """MAX's server rejected the uploaded audio *itself* — as opposed to
    it merely not having finished processing yet.

    MAX answers the audio upload with an HTTP 200 whose body carries the
    real verdict (e.g. ``{"error_code":"1","error_data":
    "AUDIO_VALIDATION_FAILED"}``); pymax never reads that body, which is
    why this looked for so long like a "not ready yet" timing problem.
    It is not: a rejected recording never becomes sendable, so waiting
    and re-uploading it can only burn time.
    """


_PATCHED_VOICE_UPLOAD_USER_AGENT = False


async def _upload_voice_without_mangled_user_agent(self, voice):
    """Replacement for pymax's ``UploadService.upload_voice``.

    Fixes two things in the upstream version, both about how a voice
    upload is handed to the server:

    1. **The attach is referenced by ``audioId``, not by the video
       token.** This is what actually makes voice sending work.
       ``upload_voice`` returns a ``VoiceAttachPayload`` carrying the
       ``token`` from MAX's *video* upload pipeline (voice is uploaded
       through it), so ``MSG_SEND`` ends up sending
       ``{_type: AUDIO, token: <video token>, ...}`` — and MAX answers
       ``errors.process.attachment.video.not.ready``: a *video* error
       for an *audio* attach, i.e. it resolved that token through the
       video pipeline, where nothing is ever going to become ready. The
       attachment therefore stays "not ready" forever, no amount of
       waiting or retrying helps (confirmed in production over many
       attempts, and in upstream bug MaxApiTeam/PyMax#103).
       ``VideoAttachPayload.serialize_attachment`` already has the
       correct shape for this — when an AUDIO attach carries no token it
       serializes as ``{_type: AUDIO, audioId: <id>, ...}`` — but that
       branch is dead code upstream, because ``upload_voice`` always
       fills the token in. Passing an empty token here is what reaches
       it. Corroborated by the ready-notification for a voice upload
       carrying *both* ``videoId`` and ``audioId`` (see
       ``_fixed_resolve_attach``): the server does track these under an
       ``audioId``, and that is the id an audio attach should name.
    2. **The upload declares what it actually is.** pymax hardcodes
       ``Content-Type: application/octet-stream``, so MAX's audio
       validator gets no hint about the payload and answers the upload
       with ``{"error_code":"1","error_data":"AUDIO_VALIDATION_FAILED"}``
       — in an HTTP *200* body, which is why this looked like a timing
       problem for so long (pymax never reads that body; its source has
       a TODO saying as much). MAX does accept Opus, and a Telegram
       voice note already is Opus in an OGG container, so the codec was
       never the problem — only that nothing said so.
    3. **The ``User-Agent`` identifies this session honestly.** pymax
       sends ``OKMessages/{config.app_version} (...)`` — its *Android*
       client's shape — and on a web session that field is unset, so the
       header went out as ``OKMessages/None (Linux; Chrome; ...)``, to
       which MAX replied ``{"error_code":"4","error_data":"BAD_REQUEST"}``.
       See ``_upload_user_agent``. It is also sent as-is rather than
       through ``urllib.parse.quote()``, which percent-escaped the
       spaces/parens/semicolons in it (a header value is not a URL
       component); ``upload_video``/``upload_file`` send no such header
       at all.

    Otherwise a faithful copy of pymax 2.4.1's ``upload_voice``
    (maxapi-python), down to log messages and error handling. The upload
    response body is logged at debug level because MAX reports upload
    errors *with* an HTTP 200 (pymax's own source has a TODO about
    exactly that), so the body is the only place a failure would show.
    """
    from http import HTTPStatus

    from pymax.api.uploads.payloads import VoiceAttachPayload

    logger = log

    logger.info("Uploading voice")

    try:
        body_bytes = await voice.read()
    except Exception as e:
        logger.exception("Failed to read voice bytes")
        raise UploadError("Failed to read voice bytes") from e

    user_agent = _upload_user_agent(self.app.config)
    content_type = _audio_content_type(voice.name)
    timeout = aiohttp.ClientTimeout(total=self.app.config.upload_timeout, sock_read=60)

    failures: list[str] = []
    # A 200 whose body carries an error_code is MAX's verdict on the
    # recording; anything else (5xx, a dropped connection) is transport
    # trouble that may well succeed on a later attempt.
    verdict_from_max = False
    video_id = None

    async for name, data in _voice_upload_variants(voice.name, body_bytes, content_type):
        # A fresh slot per variant: MAX burns the cid on a rejected POST,
        # so reusing it would answer BAD_REQUEST regardless of the shape
        # and make trying several shapes meaningless. A fresh connection
        # too — MAX drops the socket after rejecting an upload.
        upload_info = await _request_voice_upload_slot(self.app)
        video_id = upload_info.video_id
        headers = {"User-Agent": user_agent}

        logger.debug(
            "Voice upload attempt variant=%s voice_id=%s url=%s",
            name, video_id, _redact_url(upload_info.url),
        )

        try:
            async with aiohttp.ClientSession(
                timeout=timeout, proxy=self.app.config.proxy
            ) as session:
                async with session.post(
                    url=upload_info.url, headers=headers, data=data
                ) as resp:
                    # MAX reports upload errors with an HTTP 200, so the
                    # body is the only place such a failure surfaces.
                    try:
                        body = (await resp.text())[:500]
                    except Exception:
                        body = "<unreadable>"

                    if resp.status != HTTPStatus.OK or "error_code" in body:
                        logger.warning(
                            "Voice upload variant=%s rejected: HTTP %s %r",
                            name, resp.status, body,
                        )
                        failures.append(f"{name}: HTTP {resp.status} {body}")
                        if resp.status == HTTPStatus.OK:
                            verdict_from_max = True
                        continue

                    logger.info(
                        "Voice upload accepted by MAX using variant=%s voice_id=%s",
                        name, video_id,
                    )

                    # Empty token on purpose — that is what makes pymax
                    # serialize this attach as {_type: AUDIO, audioId: ...}
                    # instead of naming a video-pipeline token MAX can
                    # never resolve. See this function's docstring.
                    return VoiceAttachPayload(
                        video_id=video_id,
                        token="",
                        duration=await voice.get_duration(),
                        wave=b"\x00" * 80,
                    )
        except (aiohttp.ClientError, TimeoutError) as e:
            # Don't abandon the remaining variants over one bad socket.
            logger.warning(
                "Voice upload variant=%s failed at transport level: %s: %s",
                name, type(e).__name__, e,
            )
            failures.append(f"{name}: {type(e).__name__}: {e}")
            continue

    detail = "; ".join(failures)
    logger.error(
        "Voice upload failed in every variant voice_id=%s: %s", video_id, detail,
    )
    if verdict_from_max:
        raise VoiceRejectedByMax(
            f"MAX rejected the audio upload in all {len(failures)} variants "
            f"({detail}) voice_id={video_id}"
        )
    # Never got a verdict out of MAX — transport trouble, worth retrying.
    raise UploadError(
        f"Voice upload failed in all {len(failures)} variants ({detail}) "
        f"voice_id={video_id}"
    )


def _patch_voice_upload_user_agent() -> None:
    """Idempotent — safe to call multiple times."""
    global _PATCHED_VOICE_UPLOAD_USER_AGENT
    if _PATCHED_VOICE_UPLOAD_USER_AGENT:
        return
    from pymax.api.uploads.service import UploadService

    UploadService.upload_voice = _upload_voice_without_mangled_user_agent
    _PATCHED_VOICE_UPLOAD_USER_AGENT = True
    log.debug("Patched pymax voice upload to stop mangling the User-Agent header")

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


# Some max.ru links carry digits that *may* be a user id
# (https://max.ru/id123456789). May, not do: a public group or channel
# handle looks exactly the same — https://max.ru/id6633015816_gos is a
# channel — so this is a hint to check with MAX, never a conclusion. What
# settles it is whether MAX knows a user by that id; see
# PyMaxClient.open_dialog_with_user, which refuses to bind without that.
_PROFILE_LINK_RE = re.compile(r"^https?://(?:web\.)?max\.ru/id(\d+)\w*/?$", re.IGNORECASE)


def _user_id_in_link(link: str) -> int | None:
    """The digits a max.ru/id… link carries, if it has that shape."""
    match = _PROFILE_LINK_RE.match((link or "").strip())
    return int(match.group(1)) if match else None


_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


def _normalized_phone(text: str) -> str | None:
    """A phone number as MAX wants it, or None if this isn't one.

    Only an explicit ``+`` marks a phone apart from a user id: both are
    just digits otherwise, and binding the wrong one of the two would
    open a dialog with a stranger.
    """
    cleaned = re.sub(r"[\s\-()]", "", (text or "").strip())
    if not cleaned.startswith("+") or not _PHONE_RE.match(cleaned):
        return None
    return cleaned


def _user_id_in_payload(payload: dict) -> int | None:
    """A user id in a LINK_INFO answer, if MAX answered with a person.

    A personal link (``max.ru/u/<token>``) names someone, not a chat, and
    MAX is the only one who can read the token. Which key it returns them
    under isn't documented, so the plausible ones are all accepted — the
    id is what matters, and open_dialog_with_user turns it into the
    dialog. Anything unrecognised is logged by the caller rather than
    guessed at.
    """
    for key in ("contact", "user", "profile"):
        item = payload.get(key)
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                return int(item["id"])
            except (TypeError, ValueError):
                return None
    for key in ("contacts", "users"):
        items = payload.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            candidate = items[0].get("id")
            if candidate is not None:
                try:
                    return int(candidate)
                except (TypeError, ValueError):
                    return None
    return None


def _normalized_link(link: str) -> str:
    """A max.ru link reduced to what identifies it, for comparison."""
    text = (link or "").strip().lower().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.removeprefix("web.")


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
        _patch_attachment_wait_timeout()
        _patch_voice_upload_user_agent()
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
        """Look up contacts, giving up rather than waiting forever.

        MAX can leave a lookup unanswered indefinitely — seen live for a
        profile it won't describe — and this call sits on the path of the
        topic intro card, the resolver's name lookups and /add's title
        pick. Unbounded, one silent server meant the card was never
        posted at all. A missing name only costs a name; a hang costs the
        whole feature.
        """
        if not contact_ids:
            return {}
        try:
            users = await asyncio.wait_for(
                self._client.get_users(contact_ids), timeout=USER_LOOKUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("MAX did not answer a contact lookup for %s in %ss",
                        contact_ids, USER_LOOKUP_TIMEOUT)
            return {}
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
        # A voice/video attachment can raise UploadError("Timed out
        # waiting for video processing ...") — MAX's own "attachment
        # ready" notification structurally never arrives for these
        # (see _patch_attachment_wait_timeout), so each attempt is a
        # fresh re-upload with only a short wait. Retry a few times
        # before giving up; anything else fails immediately as before.
        last_exc: Exception | None = None
        for attempt in range(1, _ATTACHMENT_READY_MAX_ATTEMPTS + 1):
            try:
                message = await self._client.send_message(
                    int(chat_id),
                    text=text or None,
                    attachments=attaches or None,
                    notify=True,
                )
            except VoiceRejectedByMax as exc:
                # Not a timing problem — re-uploading the same rejected
                # recording would fail identically every time, so mark it
                # as permanent and spare the outbox retrying it forever.
                log.error("MAX rejected the audio for chat %s: %s", chat_id, exc)
                return {"_max_error": {"message": str(exc), "permanent": True}}
            except UploadError as exc:
                last_exc = exc
                log.warning(
                    "MAX attachment still not ready for chat %s (attempt %d/%d): %s",
                    chat_id, attempt, _ATTACHMENT_READY_MAX_ATTEMPTS, exc,
                )
                continue
            except Exception as exc:
                log.exception("PyMax send_message failed for chat %s", chat_id)
                return {"_max_error": {"message": str(exc)}}
            else:
                break
        else:
            log.error(
                "PyMax send_message: MAX attachment never became ready for "
                "chat %s after %d attempts",
                chat_id, _ATTACHMENT_READY_MAX_ATTEMPTS,
            )
            return {"_max_error": {"message": str(last_exc)}}
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

    async def upload_video_note(
        self,
        data: bytes,
        chat_id=None,
        filename: str = "video_note.mp4",
        duration: int | None = None,
    ):
        """Telegram's round video message → MAX's own equivalent.

        Uploaded through the same pipeline as a plain video, except MAX
        confirms these synchronously — pymax's upload_video only waits
        for a processing notification when it isn't a VideoNote.
        """
        from pymax import VideoNote

        return VideoNote(raw=data, name=filename, duration=duration)

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
        """Resolve any max.ru link to a chat the bridge can bind.

        Three steps, in the order they're tried:

        1. **A chat we already have.** MAX gives every public group and
           channel a handle link (``max.ru/id6633015816_gos``) and ships
           it with the chat, so a link we already hold needs no request
           and no joining — we're in it, that's why we have it. This also
           keeps such a handle from being mistaken for something else:
           the digits in it are the *chat's*, not a user id.
        2. **A join link** (``.../join/<token>``) — what pymax supports;
           it looks for that literal ``join/`` and refuses anything else.
        3. **Anything left** is put to MAX itself via LINK_INFO, which is
           how a client resolves a link it can't read on its own.
        4. **A person, finally.** If the link carries digits that could be
           a user id and MAX confirms a user by that id, the one-to-one
           chat with them is bound — derived, not joined. Last, because
           the same shape is also how public chats are addressed, and
           only on MAX's confirmation, which is what tells the two apart.
        """
        known = self._known_chat_by_link(link)
        if known is not None:
            return {"chatId": known.get("id"), "chat": known}

        try:
            chat = await self._client.join_group(link)
        except ValueError:
            # No join token in the string at all — pymax refused before
            # reaching the wire.
            try:
                chat = await self._client.join_channel(link)
            except Exception as exc:
                log.warning("PyMax join failed for %s: %s — asking MAX to resolve it",
                            _redact_url(link), exc)
                return await self._unjoinable(link, exc)
        except Exception as exc:
            # MAX itself refused the join — "not.found" being the common
            # one, which it also answers for a chat you are already in.
            # Resolving the link read-only still finds that chat, and is
            # exactly what /add wants: a chat id to bind, not membership.
            log.warning("PyMax join rejected for %s: %s — asking MAX to resolve it",
                        _redact_url(link), exc)
            return await self._unjoinable(link, exc)
        return {"chatId": getattr(chat, "id", None), "chat": _chat_to_dict(chat)}

    async def _unjoinable(self, link: str, join_error: Exception) -> dict:
        """A link we couldn't join: ask MAX about the chat, then the person.

        Order matters. A chat is the common case and LINK_INFO answers
        for it directly; only if that finds nothing do the digits in the
        link get their chance, and then only if MAX confirms a user.
        """
        resolved = await self._resolve_link_via_max(link, join_error)
        if "_max_error" not in resolved:
            return resolved

        user_id = _user_id_in_link(link)
        if user_id is None:
            return resolved
        dialog = await self.open_dialog_with_user(user_id)
        if "_max_error" not in dialog:
            return dialog
        log.info("Link %s looked like a profile, but MAX knows no user %s",
                 _redact_url(link), user_id)
        return resolved

    async def leave_or_delete_chat(self, chat_id: int) -> dict:
        """Leave a group, unsubscribe from a channel, or delete a dialog.

        Which of the three is decided by what the chat is, since MAX has a
        separate call for each. Irreversible on MAX's side — the caller is
        expected to have asked first.

        A dialog is deleted **for us only**: pymax's delete_chat defaults
        to for_all=True, which would erase the conversation from the other
        person's account as well. Nobody asks for that by asking to leave
        a chat, so it is pinned to False here and not exposed.

        Returns {"left": <what was done>} or the usual _max_error shape.
        """
        resolver = self.resolver
        chat = (resolver.chats_raw.get(chat_id) or {}) if resolver is not None else {}
        chat_type = str(chat.get("type") or "").upper()
        if not chat_type and resolver is not None:
            chat_type = str(resolver.chat_types.get(chat_id) or "").upper()

        try:
            if chat_type == "CHANNEL":
                await self._client.leave_channel(int(chat_id))
                action = "отписался от канала"
            elif chat_type == "DIALOG":
                await self._client.delete_chat(
                    chat_id=int(chat_id),
                    last_event_time=chat.get("lastEventTime"),
                    for_all=False,
                )
                action = "удалил диалог"
            else:
                # Unknown type included: a group is the common case, and
                # MAX answers with an error rather than doing damage if
                # this chat isn't one.
                await self._client.leave_group(int(chat_id))
                action = "вышел из чата"
        except Exception as exc:
            log.exception("Leaving MAX chat %s failed", chat_id)
            return {"_max_error": {"message": str(exc)}}

        log.info("MAX chat %s: %s", chat_id, action)
        return {"left": action}

    async def open_dialog_by_phone(self, phone: str) -> dict:
        """Address the dialog with whoever owns this phone number.

        MAX resolves the number itself (CONTACT_INFO_BY_PHONE, exposed by
        pymax as search_by_phone); from the user it returns, the dialog
        follows the same way as everywhere else. Finding nobody is
        reported rather than bound — the point of asking was to be sure.
        """
        normalized = _normalized_phone(phone)
        if normalized is None:
            return {"_max_error": {"message": f"Не похоже на номер телефона: {phone}"}}
        try:
            user = await asyncio.wait_for(
                self._client.search_by_phone(normalized), timeout=USER_LOOKUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("MAX did not answer a phone lookup in %ss", USER_LOOKUP_TIMEOUT)
            return {"_max_error": {"message": "MAX не ответил на поиск по номеру"}}
        except Exception as exc:
            log.exception("PyMax search_by_phone failed")
            return {"_max_error": {"message": str(exc)}}

        user_id = getattr(user, "id", None)
        if user_id is None:
            return {"_max_error": {"message": "В MAX нет пользователя с таким номером"}}

        # Seed the name so open_dialog_with_user takes it as confirmation
        # and doesn't look the same person up a second time.
        if self.resolver is not None:
            name = self.resolver._extract_name_from_contact(_user_to_dict(user))
            if name:
                self.resolver.users[int(user_id)] = name
        log.info("Phone lookup resolved to MAX user %s", user_id)
        return await self.open_dialog_with_user(int(user_id))

    async def open_dialog_with_user(self, user_id: int) -> dict:
        """Address the one-to-one chat with a MAX user by their id.

        A DM is not joined, it is derived: MAX's id for the chat between
        two people is the XOR of their user ids, computed locally by
        pymax's ``get_chat_id`` with no request at all — checked against
        live dialogs, it holds exactly.

        So the one thing worth asking the server is whether the person
        exists, and that answer is required, not decorative: the digits
        in a link are just as likely to belong to a public channel
        (max.ru/id6633015816_gos is one), and MAX answers a lookup for
        those with an empty list. Binding on a guess produced a topic
        wired to a chat that could never receive anything.

        Returns the same shape as open_by_link, so callers need no
        special case.
        """
        if self._my_id is None:
            return {"_max_error": {
                "message": "MAX ещё не сообщил мой собственный id — попробуйте позже",
            }}

        # A name MAX already gave us is itself proof the person exists,
        # and costs nothing: contacts and everyone sharing a chat with us
        # are resolved at startup.
        title = ""
        if self.resolver is not None:
            cached = self.resolver.users.get(int(user_id))
            if cached and cached != str(user_id):
                title = cached

        user = None
        if not title:
            # Bounded: MAX can leave a lookup unanswered indefinitely.
            try:
                user = await asyncio.wait_for(
                    self._client.get_user(int(user_id)), timeout=USER_LOOKUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning("MAX did not answer a lookup for user %s in %ss",
                            user_id, USER_LOOKUP_TIMEOUT)
            except Exception:
                log.exception("PyMax get_user failed for %s", user_id)
            if user is None:
                return {"_max_error": {
                    "message": f"MAX не знает пользователя {user_id}",
                }}
            if self.resolver is not None:
                # The resolver already knows how MAX shapes a name (the
                # "names" array, firstName/lastName, friendly, ...).
                title = self.resolver._extract_name_from_contact(_user_to_dict(user))

        chat_id = self._client.get_chat_id(int(self._my_id), int(user_id))
        log.info("MAX user %s (%r) → dialog chat_id=%s",
                 user_id, title or "no name yet", chat_id)

        if title and self.resolver is not None:
            self.resolver.users[int(user_id)] = title

        chat = {
            "id": chat_id,
            "type": "DIALOG",
            "participants": {str(self._my_id): 0, str(user_id): 0},
        }
        # Only a real name goes in. A stand-in id would look like a title
        # downstream and suppress the naming others would do — /add's own
        # peer lookup, and ensure_topic's rename once MAX says who this is.
        if title:
            chat["title"] = title
        return {"chatId": chat_id, "chat": chat}

    def _known_chat_by_link(self, link: str) -> dict | None:
        """A chat from our own list whose invite link is this one.

        MAX hands out a handle link for public groups and channels and
        includes it in the chat's own data, so the chat behind such a
        link is often already in front of us — subscribed to, listed by
        /list, and needing nothing from the server to bind.
        """
        resolver = self.resolver
        if resolver is None or not link:
            return None
        wanted = _normalized_link(link)
        if not wanted:
            return None
        for chat_id, chat in (resolver.chats_raw or {}).items():
            if not isinstance(chat, dict):
                continue
            if _normalized_link(chat.get("link") or "") == wanted:
                log.info("Link %s belongs to chat %s (%r) we already have",
                         _redact_url(link), chat_id, chat.get("title"))
                return chat
        return None

    def _is_participant(self, chat: dict) -> bool:
        """Whether we're already in this chat, per MAX's own participant list."""
        participants = chat.get("participants") or {}
        return any(str(key) == str(self._my_id) for key in participants)

    async def _resolve_link_via_max(self, link: str, join_error: Exception) -> dict:
        """Last resort for a link pymax itself can't classify: ask MAX.

        LINK_INFO is the opcode pymax uses behind resolve_group_by_link,
        but that method rejects anything without a ``join/`` token before
        it ever reaches the wire, so the call is made directly here.

        Resolving is not joining, and /add exists to join — so the answer
        is only accepted when MAX lists us among the chat's participants,
        i.e. we're in it already and the refused join was redundant.
        Binding a chat we never entered would hand back a topic that can
        never receive a message, which is worse than the error. If MAX
        answers with nothing, or with a chat we're not in, the *join*
        error is reported: it describes what the user actually typed.
        """
        from pymax.api.chats.payloads import LinkInfoPayload
        from pymax.protocol import Opcode

        try:
            response = await self._client._app.invoke(
                Opcode.LINK_INFO, LinkInfoPayload(link=link).to_payload(),
            )
        except Exception:
            log.exception("LINK_INFO failed for %s", _redact_url(link))
            return {"_max_error": {"message": str(join_error)}}

        payload = getattr(response, "payload", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        # Logged on every resolution: this is the one place that sees what
        # MAX makes of a link it alone can read (a personal max.ru/u/<token>
        # among them), and the shape of that answer is not documented
        # anywhere — the keys it carries are how we learn it.
        log.info("LINK_INFO for %s answered with keys=%s",
                 _redact_url(link), sorted(payload))

        user_id = _user_id_in_payload(payload)
        if user_id is not None:
            log.info("LINK_INFO resolved %s to user %s", _redact_url(link), user_id)
            dialog = await self.open_dialog_with_user(user_id)
            if "_max_error" not in dialog:
                return dialog

        chat = payload.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            log.warning("LINK_INFO gave no chat for %s: %s",
                        _redact_url(link), str(payload)[:300])
            return {"_max_error": {"message": str(join_error)}}
        if not self._is_participant(chat):
            log.warning("LINK_INFO resolved %s to chat %s, but we are not in it — "
                        "reporting the join failure instead of binding it",
                        _redact_url(link), chat.get("id"))
            # Naming the chat turns a dead end into a next step: join it
            # from the MAX app, then /bind that id (or repeat /add).
            # Plain text on purpose: /add reports MAX errors without
            # parse_mode, so markup would show up as markup.
            title = chat.get("title")
            found = f"«{title}», id: {chat['id']}" if title else f"id: {chat['id']}"
            return {"_max_error": {"message": (
                f"{join_error}\n\nЧат найден — {found}. Вступите "
                f"в него в MAX, затем повторите /add или используйте /bind "
                f"с этим id."
            )}}
        log.info("Already a member of chat %s; binding it despite the refused join",
                 chat.get("id"))
        return {"chatId": chat["id"], "chat": chat}

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
