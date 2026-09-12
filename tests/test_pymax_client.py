import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.pymax_client import (
    _ATTACHMENT_READY_MAX_ATTEMPTS,
    PyMaxClient,
    _fixed_resolve_attach,
    _message_from_pymax,
    _patch_api_error_not_ready_matching,
    _patch_attachment_wait_timeout,
    _patch_voice_ready_resolution,
    _patch_voice_upload_user_agent,
)
from pymax.exceptions import ApiError, UploadError


class FakeModel:
    def __init__(self, **data):
        self.__dict__.update(data)

    def model_dump(self, by_alias=False, mode="python"):
        return dict(self.__dict__)


class FakeRawClient:
    def __init__(self):
        self.start_handlers = []
        self.message_handlers = []
        self.disconnect_handlers = []
        self.message_read_handlers = []
        self.reaction_update_handlers = []
        self.me = SimpleNamespace(
            contact=FakeModel(
                id=7,
                names=[FakeModel(firstName="Max", lastName="User")],
            )
        )
        self.chats = [
            FakeModel(id=10, type="DIALOG", title=None, participants={7: 0, 8: 0}),
            FakeModel(id=20, type="CHAT", title="Team", participants={7: 0, 9: 0}),
        ]
        self.contacts = [FakeModel(id=8, names=[FakeModel(firstName="Peer")])]
        self.get_users = AsyncMock(
            return_value=[FakeModel(id=8, names=[FakeModel(firstName="Peer")])]
        )
        self.get_chats = AsyncMock(return_value=self.chats)
        self.get_chat = AsyncMock(return_value=self.chats[1])
        self.fetch_chats = AsyncMock(return_value=[])
        self.get_file_by_id = AsyncMock(
            return_value=FakeModel(url="https://i.oneme.ru/file.bin", unsafe=False)
        )
        self.get_video_by_id = AsyncMock(
            return_value=FakeModel(url="https://i.oneme.ru/video.mp4")
        )
        self.send_message = AsyncMock(return_value=FakeModel(id=123, chatId=20))
        self.read_message = AsyncMock(return_value=FakeModel(chat_id=20))

    def on_start(self):
        def decorator(func):
            self.start_handlers.append(func)
            return func
        return decorator

    def on_message(self):
        def decorator(func):
            self.message_handlers.append(func)
            return func
        return decorator

    def on_disconnect(self):
        def decorator(func):
            self.disconnect_handlers.append(func)
            return func
        return decorator

    def on_message_read(self):
        def decorator(func):
            self.message_read_handlers.append(func)
            return func
        return decorator

    def on_reaction_update(self):
        def decorator(func):
            self.reaction_update_handlers.append(func)
            return func
        return decorator

    async def start(self):
        for handler in self.start_handlers:
            await handler(self)


@pytest.fixture
def adapter(monkeypatch):
    raw = FakeRawClient()
    monkeypatch.setattr("app.pymax_client.build_pymax_client", lambda settings, bridge_client=None: raw)
    settings = Settings(
        tg_bot_token="tg",
        tg_chat_id="-100",
        max_pymax_auth="sms",
        max_phone="+79990000000",
    )
    return PyMaxClient(settings), raw


def test_converts_pymax_message_to_bridge_shape():
    message = FakeModel(
        id=55,
        chat_id=20,
        sender=8,
        text="hello",
        time=123456,
        attaches=[FakeModel(_type="PHOTO", baseUrl="https://i.oneme.ru/i?r=x")],
        link=None,
    )

    bridge_message = _message_from_pymax(message, my_id=7)

    assert bridge_message.chat_id == 20
    assert bridge_message.sender_id == 8
    assert bridge_message.message_id == "55"
    assert bridge_message.text == "hello"
    assert bridge_message.is_self is False
    assert bridge_message.attaches == [{"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/i?r=x"}]


async def test_run_emits_ready_snapshot(adapter):
    client, _ = adapter
    on_ready = AsyncMock()
    client.on_ready(on_ready)

    await client.run()

    snapshot = on_ready.await_args.args[0]
    assert snapshot["profile"]["id"] == 7
    assert snapshot["chats"][1]["title"] == "Team"
    assert snapshot["contacts"][0]["id"] == 8


async def test_run_fetches_configured_chats_missing_from_login_sync(monkeypatch):
    raw = FakeRawClient()
    raw.chats = [raw.chats[0]]
    configured_chat = FakeModel(
        id=-123, type="CHAT", title="Configured group", participants={7: 0, 9: 0}
    )
    raw.get_chats = AsyncMock(return_value=[configured_chat])
    monkeypatch.setattr("app.pymax_client.build_pymax_client", lambda settings, bridge_client=None: raw)
    settings = Settings(
        tg_bot_token="tg",
        tg_chat_id="-100",
        max_pymax_auth="sms",
        max_phone="+79990000000",
        max_chat_ids="-123",
    )
    client = PyMaxClient(settings)
    on_ready = AsyncMock()
    client.on_ready(on_ready)

    await client.run()

    raw.get_chats.assert_awaited_once_with([-123])
    snapshot = on_ready.await_args.args[0]
    assert snapshot["chats"][1]["title"] == "Configured group"


async def test_run_pages_through_full_chat_list_beyond_login_sync_window(adapter):
    """Regression test for the /list bug: PyMax's login/sync only returns a
    limited window of recently-active chats, so a group that isn't among
    the most recent ones is missing from client.chats right after login.
    fetch_chats(marker=...) must be paginated to pull in the rest — even
    when the first page entirely overlaps with what login already knew."""
    client, raw = adapter

    recent_overlap = FakeModel(
        id=20, type="CHAT", title="Team", participants={7: 0, 9: 0},
        last_event_time=2000,
    )
    older_group = FakeModel(
        id=30, type="CHAT", title="Older Group", participants={7: 0, 11: 0},
        last_event_time=1000,
    )

    async def fake_fetch_chats(marker=None):
        if marker is None:
            # First page overlaps entirely with login-sync's own chats —
            # still non-empty, so pagination must keep going.
            return [recent_overlap]
        if marker >= 999 and older_group not in raw.chats:
            raw.chats.append(older_group)
            return [older_group]
        return []

    raw.fetch_chats = AsyncMock(side_effect=fake_fetch_chats)

    on_ready = AsyncMock()
    client.on_ready(on_ready)
    await client.run()

    snapshot = on_ready.await_args.args[0]
    chat_ids = {chat["id"] for chat in snapshot["chats"]}
    assert 30 in chat_ids


async def test_run_pagination_stops_on_empty_page(adapter):
    client, raw = adapter
    raw.fetch_chats = AsyncMock(return_value=[])

    on_ready = AsyncMock()
    client.on_ready(on_ready)
    await client.run()

    raw.fetch_chats.assert_awaited_once_with(marker=None)


async def test_run_pagination_stops_when_no_new_chats(adapter):
    """If a page returns only chats we've already seen, stop instead of
    looping forever (guards against a MAX server bug returning a stuck
    marker)."""
    client, raw = adapter
    already_known = raw.chats[0]  # id=10, already in login-sync
    raw.fetch_chats = AsyncMock(return_value=[already_known])

    on_ready = AsyncMock()
    client.on_ready(on_ready)
    await client.run()

    raw.fetch_chats.assert_awaited_once()


async def test_run_pagination_respects_page_cap(adapter):
    """Even a pathological server that always returns "new" chats and a
    strictly decreasing marker must not page forever."""
    client, raw = adapter
    counter = {"n": 0}

    async def fake_fetch_chats(marker=None):
        counter["n"] += 1
        t = 100000 - counter["n"]
        return [FakeModel(id=1000 + counter["n"], type="CHAT",
                          title=f"G{counter['n']}", participants={}, last_event_time=t)]

    raw.fetch_chats = AsyncMock(side_effect=fake_fetch_chats)

    on_ready = AsyncMock()
    client.on_ready(on_ready)
    await client.run()

    assert counter["n"] == PyMaxClient.MAX_CHAT_LIST_PAGES


async def test_message_handler_receives_bridge_message(adapter):
    client, raw = adapter
    on_message = AsyncMock()
    client.on_message(on_message)
    pymax_message = FakeModel(
        id=77,
        chat_id=20,
        sender=8,
        text="from pymax",
        time=100,
        attaches=[],
        link=None,
    )

    await raw.message_handlers[0](pymax_message, raw)

    msg = on_message.await_args.args[0]
    assert msg.chat_id == 20
    assert msg.text == "from pymax"


async def test_send_message_delegates_to_pymax(adapter):
    client, raw = adapter

    resp = await client.send_message(20, "hello", elements=[{"type": "STRONG"}])

    raw.send_message.assert_awaited_once_with(
        20,
        text="hello",
        attachments=None,
        notify=True,
    )
    assert resp["id"] == 123


async def test_read_message_delegates_to_pymax(adapter):
    client, raw = adapter

    ok = await client.read_message(20, "77")

    # message_id MUST reach pymax as an int, not the str our MaxMessage
    # stores it as — pymax's ReadMessagesPayload accepts str | int, but a
    # str value serializes as a JSON string and MAX's server rejects that
    # with a validation error ("Expected number at ..."), which pymax
    # treats as fatal and tears down the whole websocket connection.
    raw.read_message.assert_awaited_once_with(77, 20)
    assert ok is True


async def test_read_message_converts_string_message_id_to_int(adapter):
    """Regression test for a real production bug: a numeric-looking str
    message_id (what MaxMessage.message_id actually is) must not reach
    pymax as a str."""
    client, raw = adapter

    await client.read_message(418124176, "117250688214045881")

    args, kwargs = raw.read_message.call_args
    assert args[0] == 117250688214045881
    assert isinstance(args[0], int)


async def test_read_message_returns_false_on_failure(adapter):
    client, raw = adapter
    raw.read_message = AsyncMock(side_effect=RuntimeError("nope"))

    ok = await client.read_message(20, "77")

    assert ok is False


async def test_message_handler_tracks_last_message_id_per_chat(adapter):
    """Needed for TG→MAX read receipts: replying in a topic marks the MAX
    chat read up to whatever we last saw from it."""
    client, raw = adapter
    pymax_message = FakeModel(
        id=77, chat_id=20, sender=8, text="hi", time=100, attaches=[], link=None,
    )

    await raw.message_handlers[0](pymax_message, raw)

    assert client.last_message_ids[20] == "77"


async def test_message_handler_tracks_last_message_id_even_when_filtered_out(adapter):
    """A chat outside MAX_CHAT_IDS/MAX_IGNORE_CHAT_IDS still updates the
    tracked id — filtering only affects forwarding to Telegram, not the
    read-receipt bookkeeping."""
    client, raw = adapter
    client.chat_ids = [999]  # only chat 999 gets forwarded
    on_message = AsyncMock()
    client.on_message(on_message)
    pymax_message = FakeModel(
        id=77, chat_id=20, sender=8, text="hi", time=100, attaches=[], link=None,
    )

    await raw.message_handlers[0](pymax_message, raw)

    on_message.assert_not_awaited()
    assert client.last_message_ids[20] == "77"


async def test_cmd_supports_contacts_and_file_download(adapter):
    client, raw = adapter

    contacts = await client.fetch_contacts([8])
    file_url = await client.resolve_file_url(20, "77", 99)

    raw.get_users.assert_awaited_once_with([8])
    raw.get_file_by_id.assert_awaited_once_with(20, 77, 99)
    assert contacts["contacts"][0]["id"] == 8
    assert file_url == "https://i.oneme.ru/file.bin"


async def test_upload_wrappers_return_pymax_files(monkeypatch, adapter):
    class FakePhoto:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeFile:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeVideo:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeVoice:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_pymax = types.SimpleNamespace(
        Photo=FakePhoto, File=FakeFile, Video=FakeVideo, Voice=FakeVoice
    )
    monkeypatch.setitem(sys.modules, "pymax", fake_pymax)
    client, _ = adapter

    photo = await client.upload_photo(b"img", filename="image.jpg")
    file = await client.upload_file(b"doc", filename="doc.txt")
    video = await client.upload_video(b"video", filename="clip.mp4")
    voice = await client.upload_audio(
        b"voice", filename="voice.ogg", duration=4200
    )

    assert isinstance(photo, FakePhoto)
    assert photo.kwargs == {"raw": b"img", "name": "image.jpg"}
    assert isinstance(file, FakeFile)
    assert file.kwargs == {"raw": b"doc", "name": "doc.txt"}
    assert isinstance(video, FakeVideo)
    assert video.kwargs == {"raw": b"video", "name": "clip.mp4"}
    assert isinstance(voice, FakeVoice)
    assert voice.kwargs == {
        "raw": b"voice",
        "name": "voice.ogg",
        "duration": 4200,
    }


# ---------------------------------------------------------------------------
# ApiError "not.ready" matching workaround
#
# Regression coverage for a real bug hit in production: sending a voice
# message failed with "⚠️ MAX: Key: errors.process.attachment.video.not.ready
# [errors.process.attachment.video.not.ready]" — pymax's own built-in
# wait-and-resend retry for this exact situation never fired, because it
# only matches the exact literal "attachment.not.ready", not the more
# specific code the server actually sends.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# read_message message_id type — regression test against the real pymax
# payload model, not just our own mock boundary
# ---------------------------------------------------------------------------

class TestReadMessagePayloadSerialization:
    def test_str_message_id_serializes_as_json_string_not_number(self):
        """Documents exactly why the int() conversion in
        PyMaxClient.read_message is required: pymax's own
        ReadMessagesPayload accepts a str for message_id (its type hint
        is ``str | int``) and happily keeps it as a str — which then
        serializes as a JSON string. MAX's server expects a JSON number
        there and rejects a string with a validation error."""
        from pymax.api.messages.enums import ReadAction
        from pymax.api.messages.payloads import ReadMessagesPayload

        payload = ReadMessagesPayload(
            type=ReadAction.READ_MESSAGE, chat_id=418124176,
            message_id="117250688214045881", mark=1234567890,
        )
        assert payload.to_payload()["messageId"] == "117250688214045881"
        assert isinstance(payload.to_payload()["messageId"], str)

    def test_int_message_id_serializes_as_json_number(self):
        from pymax.api.messages.enums import ReadAction
        from pymax.api.messages.payloads import ReadMessagesPayload

        payload = ReadMessagesPayload(
            type=ReadAction.READ_MESSAGE, chat_id=418124176,
            message_id=117250688214045881, mark=1234567890,
        )
        assert payload.to_payload()["messageId"] == 117250688214045881
        assert isinstance(payload.to_payload()["messageId"], int)


# ---------------------------------------------------------------------------
# Voice-ready vs video-ready event misclassification workaround
#
# Regression coverage for the second half of the real production bug: after
# the ApiError patch above made pymax's built-in "attachment not ready"
# retry actually fire, voice sends still failed — this time timing out
# after 60s with "Timed out waiting for video processing" — because pymax
# resolves the server's upload-ready notification as VIDEO_READY even for
# voice messages, so the wait registered under voice_upload_waiters is
# never resolved.
# ---------------------------------------------------------------------------

class TestVoiceReadyResolutionPatch:
    def test_voice_ready_payload_classified_as_voice_not_video(self):
        """The exact payload shape a voice upload-ready notification has
        in production: both videoId and audioId present, since
        VoiceAttachPayload shares the video-upload id family."""
        from types import SimpleNamespace
        from pymax.dispatch.enums import EventType

        _patch_voice_ready_resolution()
        frame = SimpleNamespace(payload={"videoId": 4279754096696, "audioId": 4279754096696})

        assert _fixed_resolve_attach(frame) == EventType.VOICE_READY

    def test_genuine_video_ready_payload_still_classified_as_video(self):
        """Must not break real (non-voice) video uploads — a genuine
        video-ready payload only ever carries videoId."""
        from types import SimpleNamespace
        from pymax.dispatch.enums import EventType

        _patch_voice_ready_resolution()
        frame = SimpleNamespace(payload={"videoId": 999})

        assert _fixed_resolve_attach(frame) == EventType.VIDEO_READY

    def test_file_ready_payload_still_classified_as_file(self):
        from types import SimpleNamespace
        from pymax.dispatch.enums import EventType

        _patch_voice_ready_resolution()
        frame = SimpleNamespace(payload={"fileId": 12345})

        assert _fixed_resolve_attach(frame) == EventType.FILE_READY

    def test_unrecognized_payload_returns_none(self):
        from types import SimpleNamespace

        _patch_voice_ready_resolution()
        frame = SimpleNamespace(payload={"somethingElse": 1})

        assert _fixed_resolve_attach(frame) is None

    def test_patches_the_live_event_map_in_place(self):
        """The actual mechanism that matters: pymax's dispatcher looks
        EVENT_MAP up fresh on every frame, so mutating it in place must
        make the real dispatch path use our fixed resolver."""
        from pymax.dispatch import mapping
        from pymax.protocol import Opcode

        _patch_voice_ready_resolution()

        assert mapping.EVENT_MAP[Opcode.NOTIF_ATTACH] is _fixed_resolve_attach

    def test_patching_twice_is_a_no_op(self):
        _patch_voice_ready_resolution()
        _patch_voice_ready_resolution()

        from pymax.dispatch import mapping
        from pymax.protocol import Opcode
        assert mapping.EVENT_MAP[Opcode.NOTIF_ATTACH] is _fixed_resolve_attach

    def test_creating_a_pymax_client_applies_this_patch_too(self, adapter):
        from pymax.dispatch import mapping
        from pymax.protocol import Opcode
        assert mapping.EVENT_MAP[Opcode.NOTIF_ATTACH] is _fixed_resolve_attach


class TestApiErrorNotReadyPatch:
    def test_video_not_ready_code_matches_after_patch(self):
        _patch_api_error_not_ready_matching()
        exc = ApiError(
            opcode=64,
            error="errors.process.attachment.video.not.ready",
            message="Key: errors.process.attachment.video.not.ready",
        )
        assert exc.error == "attachment.not.ready"

    def test_photo_not_ready_code_also_matches(self):
        """Not specific to video/voice — any '....not.ready' suffix."""
        _patch_api_error_not_ready_matching()
        exc = ApiError(opcode=64, error="errors.process.attachment.photo.not.ready")
        assert exc.error == "attachment.not.ready"

    def test_exact_original_code_still_matches(self):
        _patch_api_error_not_ready_matching()
        exc = ApiError(opcode=64, error="attachment.not.ready")
        assert exc.error == "attachment.not.ready"

    def test_unrelated_error_codes_do_not_match(self):
        _patch_api_error_not_ready_matching()
        exc = ApiError(opcode=64, error="some.other.error")
        assert exc.error != "attachment.not.ready"
        assert not (exc.error == "attachment.not.ready")

    def test_none_error_is_left_alone(self):
        _patch_api_error_not_ready_matching()
        exc = ApiError(opcode=64, error=None, message="generic failure")
        assert exc.error is None

    def test_exception_message_is_unaffected(self):
        """The patch only changes equality comparisons on .error — the
        human-readable exception text (what ends up in the ⚠️ Telegram
        warning) must stay exactly what the server sent."""
        _patch_api_error_not_ready_matching()
        exc = ApiError(
            opcode=64,
            error="errors.process.attachment.video.not.ready",
            message="Key: errors.process.attachment.video.not.ready",
        )
        assert str(exc) == (
            "Key: errors.process.attachment.video.not.ready "
            "[errors.process.attachment.video.not.ready]"
        )

    def test_patching_twice_is_a_no_op(self):
        """PyMaxClient.__init__ calls this on every instantiation —
        must not double-wrap or break on repeated calls."""
        _patch_api_error_not_ready_matching()
        _patch_api_error_not_ready_matching()
        exc = ApiError(opcode=64, error="errors.process.attachment.video.not.ready")
        assert exc.error == "attachment.not.ready"
        assert str(exc.error) == "errors.process.attachment.video.not.ready"

    def test_creating_a_pymax_client_applies_the_patch(self, adapter):
        """End-to-end: instantiating PyMaxClient (via the adapter fixture)
        must have already applied the patch, without the test calling
        _patch_api_error_not_ready_matching() itself."""
        exc = ApiError(opcode=64, error="errors.process.attachment.video.not.ready")
        assert exc.error == "attachment.not.ready"


class TestAttachmentWaitTimeoutPatch:
    async def test_wait_raises_upload_error_on_timeout(self, monkeypatch):
        """MAX never actually sends the ready notification for these
        uploads in production (confirmed across several debug-log
        captures) — the shortened wait must still surface the same
        UploadError pymax's own (60s) version raises, just faster."""
        monkeypatch.setattr("app.pymax_client._ATTACHMENT_READY_WAIT_SECONDS", 0.01)
        _patch_attachment_wait_timeout()
        from pymax.api.messages.service import MessageService

        waiters: dict = {}
        with pytest.raises(UploadError, match="video_id=42"):
            await MessageService._wait_for_upload_signal(None, waiters, 42)
        assert 42 not in waiters  # cleaned up even on timeout

    async def test_wait_resolves_early_if_signal_arrives(self):
        _patch_attachment_wait_timeout()
        from pymax.api.messages.service import MessageService

        waiters: dict = {}

        async def resolve_soon():
            await asyncio.sleep(0)
            waiters[42].set_result("ready")

        task = asyncio.ensure_future(resolve_soon())
        await MessageService._wait_for_upload_signal(None, waiters, 42)
        await task
        assert 42 not in waiters

    def test_patching_twice_is_a_no_op(self):
        _patch_attachment_wait_timeout()
        _patch_attachment_wait_timeout()
        from pymax.api.messages.service import MessageService
        assert MessageService._wait_for_upload_signal is not None


class TestVoiceUploadUserAgentPatch:
    """MaxApiTeam/PyMax#103: pymax's own upload_voice() runs the
    User-Agent header value through urllib.parse.quote(), producing a
    mangled percent-escaped value MAX's server never finishes processing
    (upload_video/upload_file send no User-Agent at all). Patched
    version must send it unquoted and otherwise behave identically."""

    class _FakeVoice:
        name = "voice.ogg"

        async def size(self):
            return 5

        async def get_duration(self):
            return 1000

        def iter_chunks(self, chunk_size):
            async def _gen():
                yield b"12345"
            return _gen()

    class _FakeResponse:
        def __init__(self, status=200):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        last_headers = None
        last_url = None

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, headers=None, data=None):
            type(self).last_headers = headers
            type(self).last_url = url
            return TestVoiceUploadUserAgentPatch._FakeResponse(200)

    def _fake_upload_service(self):
        from types import SimpleNamespace

        response_payload = {
            "info": [{"url": "https://au.oneme.ru/x", "videoId": 42, "token": "tok123"}]
        }
        app = SimpleNamespace(
            invoke=AsyncMock(return_value=SimpleNamespace(payload=response_payload)),
            config=SimpleNamespace(
                upload_timeout=30,
                proxy=None,
                app_version="26.8.4",
                device=SimpleNamespace(
                    user_agent=SimpleNamespace(
                        os_version="Linux", device_name="Chrome", screen="1080x1920 1.0x",
                    )
                ),
            ),
        )
        return SimpleNamespace(app=app)

    async def test_sends_unquoted_user_agent_header(self, monkeypatch):
        _patch_voice_upload_user_agent()
        monkeypatch.setattr("aiohttp.ClientSession", self._FakeSession)

        service = self._fake_upload_service()
        from pymax.api.uploads.service import UploadService

        result = await UploadService.upload_voice(service, self._FakeVoice())

        sent_ua = self._FakeSession.last_headers["User-Agent"]
        assert sent_ua == "OKMessages/26.8.4 (Linux; Chrome; 1080x1920 1.0x)"
        assert "%20" not in sent_ua and "%28" not in sent_ua
        assert result.token == "tok123"

    async def test_patching_twice_is_a_no_op(self):
        _patch_voice_upload_user_agent()
        _patch_voice_upload_user_agent()
        from pymax.api.uploads.service import UploadService
        assert UploadService.upload_voice is not None

    async def test_upload_http_error_still_raises_upload_error(self, monkeypatch):
        _patch_voice_upload_user_agent()

        class _FailingSession(self._FakeSession):
            def post(self, url, headers=None, data=None):
                return TestVoiceUploadUserAgentPatch._FakeResponse(500)

        monkeypatch.setattr("aiohttp.ClientSession", _FailingSession)
        service = self._fake_upload_service()
        from pymax.api.uploads.service import UploadService

        with pytest.raises(UploadError, match="status 500"):
            await UploadService.upload_voice(service, self._FakeVoice())


class TestSendMessageAttachmentRetry:
    async def test_succeeds_immediately_without_upload_error(self, adapter):
        client, raw = adapter
        resp = await client.send_message(20, "hello")
        raw.send_message.assert_awaited_once()
        assert resp["id"] == 123

    async def test_retries_on_upload_error_then_succeeds(self, adapter):
        client, raw = adapter
        raw.send_message = AsyncMock(
            side_effect=[
                UploadError("Timed out waiting for video processing video_id=1"),
                UploadError("Timed out waiting for video processing video_id=2"),
                FakeModel(id=999, chatId=20),
            ]
        )

        resp = await client.send_message(20, attaches=[object()])

        assert raw.send_message.await_count == 3
        assert resp["id"] == 999

    async def test_gives_up_after_max_attempts(self, adapter):
        client, raw = adapter
        raw.send_message = AsyncMock(
            side_effect=UploadError("Timed out waiting for video processing video_id=1")
        )

        resp = await client.send_message(20, attaches=[object()])

        assert raw.send_message.await_count == _ATTACHMENT_READY_MAX_ATTEMPTS
        assert "_max_error" in resp
        assert "Timed out" in resp["_max_error"]["message"]

    async def test_non_upload_error_fails_immediately_without_retry(self, adapter):
        client, raw = adapter
        raw.send_message = AsyncMock(side_effect=RuntimeError("connection lost"))

        resp = await client.send_message(20, "hello")

        raw.send_message.assert_awaited_once()
        assert "_max_error" in resp
        assert "connection lost" in resp["_max_error"]["message"]
