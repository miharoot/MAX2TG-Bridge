"""Service events from MAX: "Анастасия добавила mihar" used to reach the
topic as "[нетекстовое сообщение]" — the message carries no text, only a
CONTROL attachment."""

from unittest.mock import AsyncMock, MagicMock

from app.max_listener import _control_event_line
from app.pymax_client import MaxMessage


def _msg(*attaches, text=""):
    return MaxMessage(chat_id=-42, sender_id=1, text=text, timestamp=1,
                      message_id="1", is_self=False, cid=None,
                      attaches=list(attaches), link=None, raw={})


def _resolver(names=None):
    names = names or {427441720: "Иван Петров"}
    resolver = MagicMock()
    resolver.resolve_user = AsyncMock(
        side_effect=lambda uid: names.get(uid, str(uid)))
    return resolver


class TestControlEventLine:
    async def test_an_added_member_is_named(self):
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "add", "userIds": [427441720]}),
            _resolver())

        assert line == "➕ добавил(а) в чат: Иван Петров"

    async def test_a_removed_member_is_named(self):
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "remove", "userIds": [427441720]}),
            _resolver())

        assert "Иван Петров" in line and "убрал" in line

    async def test_leaving_needs_no_names(self):
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "leave"}), _resolver())

        assert line == "🚪 вышел(а) из чата"

    async def test_a_rename_carries_the_new_title(self):
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "title", "title": "Рабочий чат"}),
            _resolver())

        assert "Рабочий чат" in line

    async def test_an_unknown_event_is_shown_as_itself(self):
        """Swallowing it would hide the one clue needed to render the next
        one properly."""
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "somethingnew"}), _resolver())

        assert "somethingnew" in line

    async def test_a_name_is_escaped(self):
        line = await _control_event_line(
            _msg({"_type": "CONTROL", "event": "add", "userIds": [1]}),
            _resolver({1: "<b>дерзкий</b>"}))

        assert "<b>" not in line

    async def test_an_ordinary_message_has_no_control_line(self):
        assert await _control_event_line(_msg(text="привет"), _resolver()) is None
