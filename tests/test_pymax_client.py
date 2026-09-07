import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.pymax_client import PyMaxClient, _message_from_pymax


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
    monkeypatch.setattr("app.pymax_client.build_pymax_client", lambda settings: raw)
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
    monkeypatch.setattr("app.pymax_client.build_pymax_client", lambda settings: raw)
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
