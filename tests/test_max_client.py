"""Tests for app/max_client.py — OpCode enum and _parse_message."""

import asyncio

import pytest
from unittest.mock import AsyncMock

from app.max_client import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    MaxClient,
    MaxMessage,
    OpCode,
    _is_allowed_download_url,
    _redact_sensitive,
    _redact_url,
)


# ---------------------------------------------------------------------------
# OpCode enum
# ---------------------------------------------------------------------------

class TestOpCode:
    """Validate that all expected opcodes exist with their correct integer values."""

    def test_heartbeat_ping(self):
        assert OpCode.HEARTBEAT_PING == 1

    def test_handshake(self):
        assert OpCode.HANDSHAKE == 6

    def test_auth_snapshot(self):
        assert OpCode.AUTH_SNAPSHOT == 19

    def test_logout(self):
        assert OpCode.LOGOUT == 20

    def test_sticker_store(self):
        assert OpCode.STICKER_STORE == 27

    def test_asset_get(self):
        assert OpCode.ASSET_GET == 28

    def test_favorite_sticker(self):
        assert OpCode.FAVORITE_STICKER == 29

    def test_contact_get(self):
        assert OpCode.CONTACT_GET == 32

    def test_contact_presence(self):
        assert OpCode.CONTACT_PRESENCE == 35

    def test_chat_get(self):
        assert OpCode.CHAT_GET == 48

    def test_send_message(self):
        assert OpCode.SEND_MESSAGE == 64

    def test_edit_message(self):
        assert OpCode.EDIT_MESSAGE == 67

    def test_dispatch(self):
        assert OpCode.DISPATCH == 128

    def test_all_values_are_ints(self):
        for member in OpCode:
            assert isinstance(member.value, int), f"{member.name} is not an int"

    def test_no_duplicate_values(self):
        values = [m.value for m in OpCode]
        assert len(values) == len(set(values)), "Duplicate opcode values found"

    def test_can_be_used_as_int(self):
        # IntEnum should compare equal to a plain int
        assert OpCode.HANDSHAKE == 6
        assert 6 == OpCode.HANDSHAKE


# ---------------------------------------------------------------------------
# MaxMessage dataclass defaults
# ---------------------------------------------------------------------------

class TestMaxMessageDefaults:
    def test_default_text_is_empty_string(self):
        msg = MaxMessage()
        assert msg.text == ""

    def test_default_is_self_is_false(self):
        msg = MaxMessage()
        assert msg.is_self is False

    def test_default_attaches_is_empty_list(self):
        msg = MaxMessage()
        assert msg.attaches == []

    def test_default_link_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.link == {}

    def test_default_raw_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.raw == {}

    def test_attaches_are_independent_instances(self):
        # mutable default via field(default_factory=...) must not be shared
        m1 = MaxMessage()
        m2 = MaxMessage()
        m1.attaches.append("x")
        assert m2.attaches == []


# ---------------------------------------------------------------------------
# MaxClient._parse_message
# ---------------------------------------------------------------------------

def _make_client() -> MaxClient:
    return MaxClient(token="tok", device_id="dev")


class TestParseMessage:
    """Tests for _parse_message — the only complex pure-ish method."""

    def test_returns_none_when_no_message_key(self):
        client = _make_client()
        assert client._parse_message({}) is None

    def test_returns_none_when_message_is_not_dict(self):
        client = _make_client()
        assert client._parse_message({"message": "oops"}) is None
        assert client._parse_message({"message": 42}) is None
        assert client._parse_message({"message": None}) is None

    def test_basic_text_message(self):
        client = _make_client()
        payload = {
            "chatId": 100,
            "message": {
                "sender": 7,
                "text": "Hello",
                "time": 1700000000000,
                "id": "abc123",
            },
        }
        msg = client._parse_message(payload)
        assert msg is not None
        assert msg.chat_id == 100
        assert msg.sender_id == 7
        assert msg.text == "Hello"
        assert msg.timestamp == 1700000000000
        assert msg.message_id == "abc123"

    def test_message_id_is_always_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"id": 99999}}
        msg = client._parse_message(payload)
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "99999"

    def test_missing_text_defaults_to_empty_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 1}}
        msg = client._parse_message(payload)
        assert msg.text == ""

    def test_attaches_populated(self):
        client = _make_client()
        attaches = [{"_type": "PHOTO", "url": "http://example.com/img.jpg"}]
        payload = {"chatId": 1, "message": {"attaches": attaches}}
        msg = client._parse_message(payload)
        assert msg.attaches == attaches

    def test_attaches_none_becomes_empty_list(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"attaches": None}}
        msg = client._parse_message(payload)
        assert msg.attaches == []

    def test_link_populated(self):
        client = _make_client()
        link = {"type": "FORWARD", "message": {"text": "original"}}
        payload = {"chatId": 1, "message": {"link": link}}
        msg = client._parse_message(payload)
        assert msg.link == link

    def test_link_none_becomes_empty_dict(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"link": None}}
        msg = client._parse_message(payload)
        assert msg.link == {}

    def test_raw_is_full_payload(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"text": "hi"}, "extra": "data"}
        msg = client._parse_message(payload)
        assert msg.raw is payload

    def test_is_self_false_when_my_id_not_set(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_false_when_sender_differs(self):
        client = _make_client()
        client._my_id = 1
        payload = {"chatId": 1, "message": {"sender": 99}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_true_when_sender_matches_my_id(self):
        client = _make_client()
        client._my_id = 42
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is True

    def test_chat_id_none_when_absent(self):
        client = _make_client()
        payload = {"message": {"text": "no chat id"}}
        msg = client._parse_message(payload)
        assert msg.chat_id is None

    def test_empty_message_dict_returns_none(self):
        # Empty dict is falsy in Python, so _parse_message treats it as absent
        client = _make_client()
        payload = {"chatId": 5, "message": {}}
        msg = client._parse_message(payload)
        assert msg is None


# ---------------------------------------------------------------------------
# MaxClient constructor / basic state
# ---------------------------------------------------------------------------

class TestMaxClientInit:
    def test_token_stored(self):
        c = MaxClient(token="my_token", device_id="dev1")
        assert c.token == "my_token"

    def test_device_id_stored(self):
        c = MaxClient(token="tok", device_id="mydev")
        assert c.device_id == "mydev"

    def test_debug_default_false(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.debug is False

    def test_debug_explicit_true(self):
        c = MaxClient(token="tok", device_id="dev", debug=True)
        assert c.debug is True

    def test_initial_seq_is_zero(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._seq == 0

    def test_initial_my_id_is_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._my_id is None

    def test_ws_url_constant(self):
        assert MaxClient.WS_URL == "wss://ws-api.oneme.ru/websocket"

    def test_heartbeat_sec_constant(self):
        assert MaxClient.HEARTBEAT_SEC == 30

    def test_reconnect_sec_constant(self):
        assert MaxClient.RECONNECT_SEC == 5

    def test_on_disconnect_cb_initial_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._on_disconnect_cb is None

    def test_on_disconnect_decorator_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        @c.on_disconnect
        async def my_handler():
            pass

        assert c._on_disconnect_cb is my_handler

    def test_on_disconnect_returns_function(self):
        c = MaxClient(token="tok", device_id="dev")

        async def my_handler():
            pass

        result = c.on_disconnect(my_handler)
        assert result is my_handler


# ---------------------------------------------------------------------------
# chat filtering: chat_ids / ignore_chat_ids
# ---------------------------------------------------------------------------

class TestChatFiltering:
    def test_chat_ids_are_instance_local(self):
        """chat_ids must NOT be a shared mutable class attribute — otherwise
        one instance's allow-list would leak into every other instance."""
        c1 = MaxClient(token="tok", device_id="dev", chat_ids="-1")
        c2 = MaxClient(token="tok", device_id="dev")

        assert c1.chat_ids == [-1]
        assert c2.chat_ids == []

    def test_ignore_chat_ids_are_parsed(self):
        c = MaxClient(token="tok", device_id="dev", ignore_chat_ids="-789, -101112")
        assert c.ignore_chat_ids == [-789, -101112]

    def test_invalid_chat_ids_raise_clear_error(self):
        with pytest.raises(SystemExit) as exc:
            MaxClient(token="tok", device_id="dev", chat_ids="-1, nope")
        assert "MAX_CHAT_IDS" in str(exc.value)

    def test_invalid_ignore_chat_ids_raise_clear_error(self):
        with pytest.raises(SystemExit) as exc:
            MaxClient(token="tok", device_id="dev", ignore_chat_ids="-789, nope")
        assert "MAX_IGNORE_CHAT_IDS" in str(exc.value)

    def test_should_dispatch_all_chats_when_no_filters(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._should_dispatch_message(MaxMessage(chat_id=-1)) is True

    def test_should_dispatch_only_allowed_chat_when_allow_list_set(self):
        c = MaxClient(token="tok", device_id="dev", chat_ids="-1,-2")
        assert c._should_dispatch_message(MaxMessage(chat_id=-1)) is True
        assert c._should_dispatch_message(MaxMessage(chat_id=-3)) is False

    def test_ignore_chat_ids_take_precedence_over_allow_list(self):
        c = MaxClient(token="tok", device_id="dev", chat_ids="-1,-2", ignore_chat_ids="-2")
        assert c._should_dispatch_message(MaxMessage(chat_id=-1)) is True
        assert c._should_dispatch_message(MaxMessage(chat_id=-2)) is False

    def test_ignore_chat_ids_filter_when_allow_list_unset(self):
        c = MaxClient(token="tok", device_id="dev", ignore_chat_ids="-2")
        assert c._should_dispatch_message(MaxMessage(chat_id=-1)) is True
        assert c._should_dispatch_message(MaxMessage(chat_id=-2)) is False

    def test_should_not_dispatch_none_message(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._should_dispatch_message(None) is False


# ---------------------------------------------------------------------------
# message dedupe: in-flight + delivered (survives handler failures)
# ---------------------------------------------------------------------------

class TestMessageDedupe:
    def test_can_start_message_accepts_first_message(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")
        assert c._can_start_message(msg, now=100.0) is True

    def test_can_start_message_rejects_inflight_duplicate_message(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")
        assert c._can_start_message(msg, now=100.0) is True
        assert c._can_start_message(msg, now=101.0) is False

    def test_can_start_message_scopes_duplicates_by_chat(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._can_start_message(MaxMessage(chat_id=-1, message_id="m1"), now=100.0) is True
        assert c._can_start_message(MaxMessage(chat_id=-2, message_id="m1"), now=101.0) is True

    def test_can_start_message_allows_missing_message_id(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="")
        assert c._can_start_message(msg, now=100.0) is True
        assert c._can_start_message(msg, now=101.0) is True

    def test_finish_message_success_rejects_later_duplicate(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")
        assert c._can_start_message(msg, now=100.0) is True
        c._finish_message(msg, delivered=True, now=100.0)
        assert c._can_start_message(msg, now=101.0) is False

    def test_finish_message_failure_allows_retry(self):
        """A message whose handler crashed must NOT be remembered as
        delivered — otherwise a resend from MAX (e.g. after reconnect)
        would be silently dropped forever, losing the message."""
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")
        assert c._can_start_message(msg, now=100.0) is True
        c._finish_message(msg, delivered=False)
        assert c._can_start_message(msg, now=101.0) is True

    def test_can_start_message_expires_old_delivered_entries(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")
        assert c._can_start_message(msg, now=100.0) is True
        c._finish_message(msg, delivered=True, now=100.0)
        assert c._can_start_message(msg, now=100.0 + c.MESSAGE_DEDUPE_TTL_SEC + 1) is True

    async def test_run_message_callback_marks_delivered_on_success(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")

        async def ok(_msg):
            pass

        c._on_message_cb = ok
        assert c._can_start_message(msg, now=100.0) is True
        await c._run_message_callback(msg)
        assert c._can_start_message(msg, now=101.0) is False

    async def test_failed_message_callback_allows_retry(self):
        c = MaxClient(token="tok", device_id="dev")
        msg = MaxMessage(chat_id=-1, message_id="m1")

        async def fail(_msg):
            raise RuntimeError("boom")

        c._on_message_cb = fail
        assert c._can_start_message(msg, now=100.0) is True
        with pytest.raises(RuntimeError):
            await c._run_message_callback(msg)
        assert c._can_start_message(msg, now=101.0) is True


# ---------------------------------------------------------------------------
# bridge-echo detection (manual self-sends vs. our own bridge sends)
# ---------------------------------------------------------------------------

class TestBridgeEcho:
    def test_cid_is_parsed(self):
        c = MaxClient(token="tok", device_id="dev")
        payload = {"chatId": 1, "message": {"cid": 123456789}}
        msg = c._parse_message(payload)
        assert msg.cid == 123456789

    def test_bridge_echo_detected_by_outbound_cid(self):
        c = MaxClient(token="tok", device_id="dev")
        c._mark_outbound_cid(chat_id=-1, cid=123, now=100.0)
        assert c._is_bridge_echo(
            MaxMessage(chat_id=-1, cid=123, is_self=True), now=101.0,
        ) is True

    def test_manual_self_message_is_not_bridge_echo(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._is_bridge_echo(
            MaxMessage(chat_id=-1, cid=123, is_self=True), now=101.0,
        ) is False

    def test_non_self_message_is_not_bridge_echo_even_with_outbound_cid(self):
        c = MaxClient(token="tok", device_id="dev")
        c._mark_outbound_cid(chat_id=-1, cid=123, now=100.0)
        assert c._is_bridge_echo(
            MaxMessage(chat_id=-1, cid=123, is_self=False), now=101.0,
        ) is False


# ---------------------------------------------------------------------------
# connection state
# ---------------------------------------------------------------------------

class TestIsConnected:
    def test_false_before_authorization(self):
        c = MaxClient(token="tok", device_id="dev")
        c._ws = type("Ws", (), {"closed": False})()
        assert c.is_connected is False

    def test_false_when_ws_closed(self):
        c = MaxClient(token="tok", device_id="dev")
        c._authorized = True
        c._ws = type("Ws", (), {"closed": True})()
        assert c.is_connected is False

    def test_true_when_authorized_and_ws_open(self):
        c = MaxClient(token="tok", device_id="dev")
        c._authorized = True
        c._ws = type("Ws", (), {"closed": False})()
        assert c.is_connected is True


# ---------------------------------------------------------------------------
# debug/media settings
# ---------------------------------------------------------------------------

class TestDebugAndMediaSettings:
    def test_debug_dump_json_default_false(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.debug_dump_json is False

    def test_max_download_bytes_default(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.max_download_bytes == DEFAULT_MAX_DOWNLOAD_BYTES


# ---------------------------------------------------------------------------
# WS RPC helpers
# ---------------------------------------------------------------------------

class TestFetchChat:
    async def test_fetch_chat_uses_chatIds_payload(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"chats": []})
        await c.fetch_chat(130382286)
        c.cmd.assert_awaited_once_with(OpCode.CHAT_GET, {"chatIds": [130382286]})


class TestVideoDownloadUrl:
    async def test_resolves_best_allowed_mp4_quality(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={
            "MP4_720": "https://vd123.okcdn.ru/video.mp4",
            "MP4_360": "https://vd123.okcdn.ru/video-small.mp4",
        })
        url = await c.download_video_url("190714046", chat_id=-100, message_id="message-1")
        assert url == "https://vd123.okcdn.ru/video.mp4"
        c.cmd.assert_awaited_once_with(
            OpCode.VIDEO_DOWNLOAD_URL,
            {"videoId": 190714046, "chatId": -100, "messageId": "message-1"},
        )

    async def test_rejects_external_video_url(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"MP4_1080": "https://example.com/video.mp4"})
        assert await c.download_video_url(123, chat_id=-100, message_id="message-1") is None


# ---------------------------------------------------------------------------
# safety helpers: SSRF protection + secret redaction
# ---------------------------------------------------------------------------

class TestSafetyHelpers:
    def test_redacts_sensitive_dict_keys(self):
        data = {
            "token": "secret-token",
            "deviceId": "device",
            "nested": {"phone": "+79990000000", "text": "keep"},
        }
        redacted = _redact_sensitive(data)
        assert redacted["token"] == "<redacted>"
        assert redacted["deviceId"] == "<redacted>"
        assert redacted["nested"]["phone"] == "<redacted>"
        assert redacted["nested"]["text"] == "keep"

    def test_redacts_sensitive_url_query_values(self):
        assert _redact_url("https://i.oneme.ru/i?r=abc&token=secret") == (
            "https://i.oneme.ru/i?r=abc&token=%3Credacted%3E"
        )

    def test_redacts_max_invite_path_tokens(self):
        assert _redact_url("https://max.ru/join/abc123") == (
            "https://max.ru/join/<redacted>"
        )

    def test_allows_expected_max_cdn_hosts(self):
        assert _is_allowed_download_url("https://i.oneme.ru/i?r=abc")
        assert _is_allowed_download_url("https://cdn.oneme.ru/file")
        assert _is_allowed_download_url("https://web.max.ru/image")
        assert _is_allowed_download_url("https://iv.okcdn.ru/video.mp4")

    def test_blocks_non_https_and_external_hosts(self):
        assert not _is_allowed_download_url("http://i.oneme.ru/i?r=abc")
        assert not _is_allowed_download_url("https://example.com/file")
        assert not _is_allowed_download_url("https://oneme.ru.evil.test/file")
        assert not _is_allowed_download_url("https://okcdn.ru.evil.test/file")


# ---------------------------------------------------------------------------
# read / reaction events (opcodes NOTIF_MARK=130, NOTIF_MSG_REACTIONS_CHANGED=155)
# ---------------------------------------------------------------------------

class TestOpCodeReadReaction:
    def test_notif_mark_value(self):
        assert OpCode.NOTIF_MARK == 130

    def test_notif_msg_reactions_changed_value(self):
        assert OpCode.NOTIF_MSG_REACTIONS_CHANGED == 155


class TestParseReadEvent:
    def test_parses_basic_fields(self):
        c = MaxClient(token="tok", device_id="dev")
        event = c._parse_read_event({
            "chatId": -100, "userId": 42, "mark": 1723000000000,
        })
        assert event.chat_id == -100
        assert event.user_id == 42
        assert event.mark == 1723000000000
        assert event.set_as_unread is False

    def test_parses_set_as_unread(self):
        c = MaxClient(token="tok", device_id="dev")
        event = c._parse_read_event({"chatId": -100, "setAsUnread": True})
        assert event.set_as_unread is True

    def test_missing_chat_id_returns_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._parse_read_event({"userId": 1}) is None


class TestParseReactionEvent:
    def test_parses_basic_fields(self):
        c = MaxClient(token="tok", device_id="dev")
        event = c._parse_reaction_event({
            "chatId": -100, "messageId": "abc123",
            "counters": [{"reaction": "👍", "count": 2}],
            "totalCount": 2,
        })
        assert event.chat_id == -100
        assert event.message_id == "abc123"
        assert event.counters == [{"reaction": "👍", "count": 2}]
        assert event.total_count == 2

    def test_missing_chat_id_returns_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._parse_reaction_event({"messageId": "abc"}) is None

    def test_missing_message_id_returns_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._parse_reaction_event({"chatId": -100}) is None


class TestOnReadOnReactionDecorators:
    def test_on_read_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        async def handler(event):
            pass

        assert c.on_read(handler) is handler
        assert c._on_read_cb is handler

    def test_on_reaction_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        async def handler(event):
            pass

        assert c.on_reaction(handler) is handler
        assert c._on_reaction_cb is handler


class TestDispatchRoutesReadAndReactionEvents:
    async def test_notif_mark_invokes_on_read_callback(self):
        c = MaxClient(token="tok", device_id="dev")
        received = []

        async def handler(event):
            received.append(event)

        c.on_read(handler)
        await c._handle({
            "opcode": OpCode.NOTIF_MARK, "cmd": 0, "seq": 1,
            "payload": {"chatId": -100, "userId": 2, "mark": 123},
        })
        await asyncio.sleep(0)  # let the fire-and-forget task run

        assert len(received) == 1
        assert received[0].chat_id == -100

    async def test_notif_reactions_changed_invokes_on_reaction_callback(self):
        c = MaxClient(token="tok", device_id="dev")
        received = []

        async def handler(event):
            received.append(event)

        c.on_reaction(handler)
        await c._handle({
            "opcode": OpCode.NOTIF_MSG_REACTIONS_CHANGED, "cmd": 0, "seq": 1,
            "payload": {"chatId": -100, "messageId": "m1",
                       "counters": [{"reaction": "👀", "count": 1}]},
        })
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0].message_id == "m1"

    async def test_no_callback_registered_does_not_raise(self):
        c = MaxClient(token="tok", device_id="dev")
        await c._handle({
            "opcode": OpCode.NOTIF_MARK, "cmd": 0, "seq": 1,
            "payload": {"chatId": -100},
        })


class TestMyIdProperty:
    def test_none_before_auth(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.my_id is None

    def test_reflects_internal_state(self):
        c = MaxClient(token="tok", device_id="dev")
        c._my_id = 12345
        assert c.my_id == 12345
