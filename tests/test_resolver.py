"""Tests for app/resolver.py — ContactResolver."""

import pytest
from app.resolver import ContactResolver


# ---------------------------------------------------------------------------
# _extract_name_from_contact
# ---------------------------------------------------------------------------

class TestExtractNameFromContact:
    """Tests for the static helper that derives a display name from a contact dict."""

    def test_names_array_first_last(self):
        c = {"names": [{"firstName": "Ivan", "lastName": "Petrov"}]}
        assert ContactResolver._extract_name_from_contact(c) == "Ivan Petrov"

    def test_names_array_first_only(self):
        c = {"names": [{"firstName": "Ivan", "lastName": ""}]}
        assert ContactResolver._extract_name_from_contact(c) == "Ivan"

    def test_names_array_null_last_name(self):
        c = {"names": [{"firstName": "Ivan", "lastName": None}]}
        assert ContactResolver._extract_name_from_contact(c) == "Ivan"

    def test_names_array_last_only(self):
        c = {"names": [{"firstName": "", "lastName": "Petrov"}]}
        assert ContactResolver._extract_name_from_contact(c) == "Petrov"

    def test_names_array_falls_back_to_name_field(self):
        c = {"names": [{"name": "SomeAlias"}]}
        assert ContactResolver._extract_name_from_contact(c) == "SomeAlias"

    def test_names_array_empty_entry_falls_through(self):
        # names list present but all fields empty → falls through to top-level fields
        c = {"names": [{}], "firstName": "Anna"}
        assert ContactResolver._extract_name_from_contact(c) == "Anna"

    def test_names_list_empty_falls_through(self):
        c = {"names": [], "firstName": "Anna"}
        assert ContactResolver._extract_name_from_contact(c) == "Anna"

    def test_top_level_first_last_camelcase(self):
        c = {"firstName": "Maria", "lastName": "Ivanova"}
        assert ContactResolver._extract_name_from_contact(c) == "Maria Ivanova"

    def test_top_level_snake_case(self):
        c = {"first_name": "Maria", "last_name": "Ivanova"}
        assert ContactResolver._extract_name_from_contact(c) == "Maria Ivanova"

    def test_top_level_first_only(self):
        c = {"firstName": "Solo"}
        assert ContactResolver._extract_name_from_contact(c) == "Solo"

    def test_friendly_fallback(self):
        c = {"friendly": "FriendlyName"}
        assert ContactResolver._extract_name_from_contact(c) == "FriendlyName"

    def test_display_name_fallback(self):
        c = {"displayName": "Display"}
        assert ContactResolver._extract_name_from_contact(c) == "Display"

    def test_name_fallback(self):
        c = {"name": "JustName"}
        assert ContactResolver._extract_name_from_contact(c) == "JustName"

    def test_completely_empty(self):
        assert ContactResolver._extract_name_from_contact({}) == ""

    def test_names_array_not_a_list(self):
        # If names happens to be a non-list value, should skip it gracefully
        c = {"names": "not-a-list", "firstName": "Oops"}
        assert ContactResolver._extract_name_from_contact(c) == "Oops"

    def test_priority_names_array_over_top_level(self):
        # names array should take priority over top-level firstName
        c = {"names": [{"firstName": "FromArray"}], "firstName": "TopLevel"}
        assert ContactResolver._extract_name_from_contact(c) == "FromArray"


# ---------------------------------------------------------------------------
# load_snapshot
# ---------------------------------------------------------------------------

class TestLoadSnapshot:
    """Tests for ContactResolver.load_snapshot."""

    def _make_resolver(self):
        return ContactResolver(client=None)

    def test_returns_participant_ids_as_list_of_ints(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1, "names": [{"firstName": "Me", "lastName": ""}]},
            "chats": [
                {"id": 100, "type": "DIALOG", "participants": {"1": {}, "2": {}, "3": {}}}
            ],
        }
        result = resolver.load_snapshot(snapshot)
        assert sorted(result) == [1, 2, 3]

    def test_my_id_stored(self):
        resolver = self._make_resolver()
        snapshot = {"profile": {"id": 42}, "chats": []}
        resolver.load_snapshot(snapshot)
        assert resolver._my_id == 42

    def test_own_name_populated(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 7, "names": [{"firstName": "Bot", "lastName": "User"}]},
            "chats": [],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.users[7] == "Bot User"

    def test_chat_title_stored(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"id": 50, "type": "GROUP", "title": "Dev Team", "participants": {}}],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.chats[50] == "Dev Team"

    def test_chat_type_stored(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"id": 50, "type": "GROUP", "title": "Team", "participants": {}}],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.chat_types[50] == "GROUP"

    def test_dm_without_title_gets_peer_placeholder(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"id": 99, "type": "DIALOG", "participants": {"1": {}, "55": {}}}],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.chats[99] == "DM:55"

    def test_dm_with_explicit_title_keeps_title(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"id": 99, "type": "DIALOG", "title": "Friend", "participants": {"1": {}, "55": {}}}],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.chats[99] == "Friend"

    def test_chat_without_id_skipped(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"type": "GROUP", "title": "Ghost", "participants": {}}],
        }
        resolver.load_snapshot(snapshot)
        assert resolver.chats == {}

    def test_invalid_participant_id_skipped(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [{"id": 10, "type": "GROUP", "participants": {"abc": {}, "2": {}}}],
        }
        result = resolver.load_snapshot(snapshot)
        assert 2 in result
        # "abc" should be silently ignored
        assert all(isinstance(x, int) for x in result)

    def test_empty_snapshot(self):
        resolver = self._make_resolver()
        result = resolver.load_snapshot({})
        assert result == []

    def test_multiple_chats_aggregate_participants(self):
        resolver = self._make_resolver()
        snapshot = {
            "profile": {"id": 1},
            "chats": [
                {"id": 10, "type": "GROUP", "participants": {"2": {}, "3": {}}},
                {"id": 20, "type": "GROUP", "participants": {"3": {}, "4": {}}},
            ],
        }
        result = resolver.load_snapshot(snapshot)
        assert sorted(result) == [2, 3, 4]


# ---------------------------------------------------------------------------
# get_name helpers on resolver (chat_name, user_name, is_dm)
# ---------------------------------------------------------------------------

class TestGetName:
    """Tests for chat_name, user_name, is_dm fallback behaviour."""

    def test_chat_name_known(self):
        resolver = ContactResolver()
        resolver.chats[10] = "Team Chat"
        assert resolver.chat_name(10) == "Team Chat"

    def test_chat_name_unknown_returns_str_id(self):
        resolver = ContactResolver()
        assert resolver.chat_name(999) == "999"

    def test_user_name_known(self):
        resolver = ContactResolver()
        resolver.users[5] = "Alice"
        assert resolver.user_name(5) == "Alice"

    def test_user_name_unknown_returns_str_id(self):
        resolver = ContactResolver()
        assert resolver.user_name(77) == "77"

    def test_is_dm_true(self):
        resolver = ContactResolver()
        resolver.chat_types[1] = "DIALOG"
        assert resolver.is_dm(1) is True

    def test_is_dm_false_for_group(self):
        resolver = ContactResolver()
        resolver.chat_types[2] = "GROUP"
        assert resolver.is_dm(2) is False

    def test_is_dm_true_for_unknown_positive_chat_id(self):
        """MAX convention: unknown positive chat_id looks like a DM peer id."""
        resolver = ContactResolver()
        assert resolver.is_dm(999) is True

    def test_is_dm_false_for_unknown_negative_chat_id(self):
        resolver = ContactResolver()
        assert resolver.is_dm(-999) is False


# ---------------------------------------------------------------------------
# _deep_extract
# ---------------------------------------------------------------------------

class TestDeepExtract:
    """Tests for ContactResolver._deep_extract."""

    def test_extracts_nested_contact(self):
        resolver = ContactResolver()
        obj = {
            "wrapper": {
                "id": 42,
                "firstName": "Deep",
                "lastName": "User",
            }
        }
        resolver._deep_extract(obj, depth=0)
        assert resolver.users[42] == "Deep User"

    def test_extracts_contact_in_list(self):
        resolver = ContactResolver()
        obj = [
            {"id": 10, "firstName": "List", "lastName": "User"},
        ]
        resolver._deep_extract(obj, depth=0)
        assert resolver.users[10] == "List User"

    def test_does_not_overwrite_existing_user(self):
        resolver = ContactResolver()
        resolver.users[5] = "Original"
        obj = {"id": 5, "firstName": "Override", "lastName": "Attempt"}
        resolver._deep_extract(obj, depth=0)
        assert resolver.users[5] == "Original"

    def test_respects_depth_limit(self):
        resolver = ContactResolver()
        # Build a deeply nested dict with a contact at depth 6 (beyond limit of 5)
        inner = {"id": 99, "firstName": "TooDeep", "lastName": ""}
        obj = inner
        for _ in range(6):
            obj = {"child": obj}
        resolver._deep_extract(obj, depth=0)
        # The contact at excessive depth should NOT be resolved
        assert 99 not in resolver.users

    def test_skips_dict_without_id(self):
        resolver = ContactResolver()
        obj = {"firstName": "NoId", "lastName": "Person"}
        resolver._deep_extract(obj, depth=0)
        assert resolver.users == {}

    def test_skips_dict_without_name(self):
        resolver = ContactResolver()
        obj = {"id": 123}
        resolver._deep_extract(obj, depth=0)
        assert 123 not in resolver.users

    def test_handles_none_values_gracefully(self):
        resolver = ContactResolver()
        obj = {"key": None, "id": 1, "firstName": "Valid"}
        # Should not raise even with None nested values
        resolver._deep_extract(obj, depth=0)
        assert resolver.users[1] == "Valid"

    def test_handles_primitive_values_in_list(self):
        resolver = ContactResolver()
        obj = [1, "string", None, {"id": 7, "firstName": "Good"}]
        resolver._deep_extract(obj, depth=0)
        assert resolver.users[7] == "Good"


# ---------------------------------------------------------------------------
# resolve_user (async, with mocked client)
# ---------------------------------------------------------------------------

class TestResolveUser:
    """Tests for the async resolve_user method."""

    @pytest.mark.asyncio
    async def test_returns_cached_name(self):
        resolver = ContactResolver()
        resolver.users[10] = "Cached"
        result = await resolver.resolve_user(10)
        assert result == "Cached"

    @pytest.mark.asyncio
    async def test_returns_str_id_when_fetch_failed_before(self):
        resolver = ContactResolver()
        resolver._fetch_failed.add(42)
        result = await resolver.resolve_user(42)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_marks_fetch_failed_when_not_found(self):
        from unittest.mock import AsyncMock, patch

        resolver = ContactResolver()
        # _ws_fetch_contacts does nothing (no client), so user won't be found
        with patch.object(resolver, "_ws_fetch_contacts", new=AsyncMock()):
            result = await resolver.resolve_user(99)
        assert result == "99"
        assert 99 in resolver._fetch_failed

    @pytest.mark.asyncio
    async def test_populates_user_after_successful_fetch(self):

        resolver = ContactResolver()

        async def fake_fetch(ids):
            resolver.users[ids[0]] = "Fetched User"

        resolver._ws_fetch_contacts = fake_fetch
        result = await resolver.resolve_user(77)
        assert result == "Fetched User"
        assert 77 not in resolver._fetch_failed


# ---------------------------------------------------------------------------
# resolve_chat — live MAX lookup for chats missing from the snapshot
# ---------------------------------------------------------------------------

class TestResolveChat:
    """Tests for resolve_chat: this is what fixes new-group topics being
    named after whoever added the bridge instead of the group itself."""

    @pytest.mark.asyncio
    async def test_returns_cached_title_without_fetching(self):
        from unittest.mock import AsyncMock

        resolver = ContactResolver()
        resolver.chats[-100] = "Already known"
        resolver._ws_fetch_chat = AsyncMock()

        result = await resolver.resolve_chat(-100)

        assert result == "Already known"
        resolver._ws_fetch_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetches_and_returns_title_for_unknown_chat(self):
        resolver = ContactResolver()

        async def fake_fetch(chat_id):
            resolver.chats[chat_id] = "Дружная команда"
            resolver.chat_types[chat_id] = "CHAT"

        resolver._ws_fetch_chat = fake_fetch
        result = await resolver.resolve_chat(-68192506787240)

        assert result == "Дружная команда"

    @pytest.mark.asyncio
    async def test_caches_failure_and_skips_refetch(self):
        from unittest.mock import AsyncMock

        resolver = ContactResolver()
        resolver._ws_fetch_chat = AsyncMock()  # does nothing → stays unresolved

        first = await resolver.resolve_chat(-999)
        resolver._ws_fetch_chat.assert_awaited_once()
        resolver._ws_fetch_chat.reset_mock()

        second = await resolver.resolve_chat(-999)
        resolver._ws_fetch_chat.assert_not_awaited()
        assert first == second == "-999"

    @pytest.mark.asyncio
    async def test_refresh_bypasses_cached_failure(self):
        resolver = ContactResolver()
        resolver._chat_fetch_failed.add(-999)

        async def fake_fetch(chat_id):
            resolver.chats[chat_id] = "Now known"

        resolver._ws_fetch_chat = fake_fetch
        result = await resolver.resolve_chat(-999, refresh=True)

        assert result == "Now known"

    @pytest.mark.asyncio
    async def test_refresh_bypasses_cache_even_if_already_known(self):
        """Used when a topic already exists and the MAX chat may have been
        renamed since — refresh=True re-queries even a 'known' chat."""
        from unittest.mock import AsyncMock

        resolver = ContactResolver()
        resolver.chats[-100] = "Old Name"
        resolver._ws_fetch_chat = AsyncMock(
            side_effect=lambda cid: resolver.chats.__setitem__(cid, "New Name")
        )

        result = await resolver.resolve_chat(-100, refresh=True)

        assert result == "New Name"
        resolver._ws_fetch_chat.assert_awaited_once()


# ---------------------------------------------------------------------------
# _parse_chat_response — CHAT_GET (opcode 48) response shapes
# ---------------------------------------------------------------------------

class TestParseChatResponse:
    def test_parses_single_chat_object(self):
        resolver = ContactResolver()
        resolver._parse_chat_response(
            {"chat": {"id": -100, "type": "CHAT", "title": "Дружная команда"}}
        )
        assert resolver.chats[-100] == "Дружная команда"
        assert resolver.chat_types[-100] == "CHAT"

    def test_parses_chats_list(self):
        resolver = ContactResolver()
        resolver._parse_chat_response(
            {"chats": [{"id": -100, "type": "CHAT", "title": "A"},
                      {"id": -101, "type": "DIALOG", "title": "B"}]}
        )
        assert resolver.chats[-100] == "A"
        assert resolver.chats[-101] == "B"
        assert resolver.chat_types[-101] == "DIALOG"

    def test_parses_chats_dict(self):
        resolver = ContactResolver()
        resolver._parse_chat_response(
            {"chats": {"-100": {"id": -100, "type": "CHAT", "title": "A"}}}
        )
        assert resolver.chats[-100] == "A"

    def test_parses_flat_response_using_requested_chat_id(self):
        resolver = ContactResolver()
        resolver._parse_chat_response(
            {"title": "Дружная команда", "type": "CHAT"},
            requested_chat_id=-100,
        )
        assert resolver.chats[-100] == "Дружная команда"

    def test_ignores_error_response(self):
        resolver = ContactResolver()
        resolver._parse_chat_response({"_max_error": {"code": "not.found"}})
        assert resolver.chats == {}

    def test_ignores_empty_response(self):
        resolver = ContactResolver()
        resolver._parse_chat_response({})
        assert resolver.chats == {}

    def test_ignores_non_dict_response(self):
        resolver = ContactResolver()
        resolver._parse_chat_response(None)
        assert resolver.chats == {}
