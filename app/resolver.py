"""Resolve numeric Max IDs to human-readable names via WebSocket RPC."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pymax_client import PyMaxClient

log = logging.getLogger(__name__)

# MAX's "Избранное" is an ordinary DIALOG whose only participant is you —
# a chat with yourself. It carries no title of its own, so without this it
# shows up nameless everywhere the peer's name would go.
SAVED_MESSAGES_TITLE = "Избранное"


class ContactResolver:
    def __init__(self, client: PyMaxClient | None = None):
        self.chats: dict[Any, str] = {}
        self.chat_types: dict[Any, str] = {}
        self.chats_raw: dict[Any, dict] = {}      # full chat snapshot per id
        self.users: dict[Any, str] = {}
        self.contacts_raw: dict[Any, dict] = {}   # full contact dicts per id
        self._client = client
        self._fetch_failed: set = set()
        self._chat_fetch_failed: set = set()
        self._my_id: Any = None

    @property
    def my_id(self) -> Any:
        return self._my_id

    def chat_name(self, chat_id: Any) -> str:
        return self.chats.get(chat_id, str(chat_id))

    def is_dm(self, chat_id: Any) -> bool:
        chat_type = self.chat_types.get(chat_id)
        if chat_type:
            return chat_type == "DIALOG"
        # Unknown chat (not yet in snapshot/fetched): MAX's own convention is
        # that DM chat IDs are positive and group/channel chat IDs are
        # negative. Falling back on this avoids briefly misfiling a brand-new
        # DM as a group (which would use the wrong topic-title heuristic).
        try:
            return int(chat_id) > 0
        except (TypeError, ValueError):
            return False

    def is_saved_messages(self, chat_id: Any) -> bool:
        """Whether this is MAX's own saved-messages chat ("Избранное").

        It is a dialog with exactly one participant — you. Every other
        chat has someone else in it, so nothing else answers to this.
        """
        if self._my_id is None:
            return False
        chat = self.chats_raw.get(chat_id) or {}
        participants = chat.get("participants") or {}
        if not participants:
            return False
        ids = set()
        for uid in participants:
            try:
                ids.add(int(uid))
            except (TypeError, ValueError):
                return False
        return ids == {int(self._my_id)}

    def user_name(self, user_id: Any) -> str:
        return self.users.get(user_id, str(user_id))

    async def resolve_user(self, user_id: Any) -> str:
        if user_id in self.users:
            return self.users[user_id]
        if user_id in self._fetch_failed:
            return str(user_id)

        await self._ws_fetch_contacts([user_id])

        if user_id in self.users:
            return self.users[user_id]
        self._fetch_failed.add(user_id)
        return str(user_id)

    async def resolve_users_batch(self, user_ids: list) -> None:
        """Pre-fetch a batch of unknown user IDs in one WS call."""
        unknown = [uid for uid in user_ids if uid not in self.users and uid not in self._fetch_failed]
        if unknown:
            await self._ws_fetch_contacts(unknown)

    async def resolve_chat(self, chat_id: Any, *, refresh: bool = False) -> str:
        """Best-effort fetch of chat metadata for chats missing from snapshot.

        This is what fixes brand-new MAX chats (e.g. right after the bot's
        account is added to a group) getting a forum topic named after
        whoever added it instead of the actual group name: the snapshot
        taken at startup doesn't know about chats created afterwards, so
        without this live lookup the fallback used to be the sender's name.

        ``refresh=True`` bypasses the cache/failure memory and re-fetches —
        used when a topic already exists and its title should be kept in
        sync with the current MAX chat title (e.g. the group was renamed).
        """
        if chat_id in self.chats and not refresh:
            return self.chat_name(chat_id)
        if chat_id in self._chat_fetch_failed and not refresh:
            return self.chat_name(chat_id)

        await self._ws_fetch_chat(chat_id)

        if chat_id in self.chats:
            return self.chat_name(chat_id)
        self._chat_fetch_failed.add(chat_id)
        return self.chat_name(chat_id)

    # ── populate from AUTH_SNAPSHOT ────────────────────────────────

    def load_snapshot(self, snapshot: dict) -> list:
        profile = snapshot.get("profile", {})
        self._my_id = profile.get("id")
        names = profile.get("names", [])
        if names and self._my_id:
            n = names[0]
            first = n.get("firstName") or ""
            last = n.get("lastName") or ""
            self.users[self._my_id] = f"{first} {last}".strip() or n.get("name", "")

        all_participant_ids: set[int] = set()

        for chat in snapshot.get("chats", []):
            cid = chat.get("id")
            ctype = chat.get("type")
            title = chat.get("title")

            if cid is None:
                continue

            self.chats_raw[cid] = chat

            if ctype:
                self.chat_types[cid] = ctype

            if title:
                self.chats[cid] = title

            participants = chat.get("participants", {})
            for uid_str in participants:
                try:
                    all_participant_ids.add(int(uid_str))
                except (ValueError, TypeError):
                    pass

            if not title and ctype == "DIALOG" and self._my_id:
                peer_id = None
                for uid in participants:
                    try:
                        uid_int = int(uid)
                    except (ValueError, TypeError):
                        continue
                    if uid_int != self._my_id:
                        peer_id = uid_int
                        break
                if peer_id:
                    self.chats[cid] = f"DM:{peer_id}"
                elif participants:
                    # Nobody but us in it: MAX's saved-messages chat.
                    self.chats[cid] = SAVED_MESSAGES_TITLE

        log.info(
            "Snapshot parsed: %d chats, my_id=%s, %d participant IDs to resolve",
            len(self.chats), self._my_id, len(all_participant_ids),
        )
        return list(all_participant_ids)

    # ── WebSocket contact fetch ────────────────────────────────────

    async def _ws_fetch_contacts(self, user_ids: list) -> None:
        if not self._client:
            return
        try:
            resp = await self._client.fetch_contacts(user_ids)
            self._parse_contacts_response(resp)
        except asyncio.CancelledError:
            log.warning("Contact fetch cancelled, falling back to numeric user IDs")
        except Exception:
            log.exception("Failed to fetch contacts via WS")

    def _parse_contacts_response(self, resp: dict) -> None:
        """Parse the response from opcode 32 (CONTACT_GET)."""
        if not resp:
            return

        contacts = resp.get("contacts") or resp.get("users") or []
        if isinstance(contacts, dict):
            contacts = contacts.values()

        for c in contacts:
            if not isinstance(c, dict):
                continue
            uid = c.get("id") or c.get("userId")
            if uid is not None:
                self.contacts_raw[uid] = c
            name = self._extract_name_from_contact(c)
            if uid is not None and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)

        # Maybe the response IS the contact (single user)
        if not contacts and resp.get("id"):
            uid = resp.get("id")
            name = self._extract_name_from_contact(resp)
            if uid and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)

        # Walk the entire response for any name-bearing objects
        self._deep_extract(resp, depth=0)

    async def _ws_fetch_chat(self, chat_id: Any) -> None:
        if not self._client:
            return
        try:
            resp = await self._client.fetch_chat(chat_id)
            self._parse_chat_response(resp, requested_chat_id=chat_id)
        except asyncio.CancelledError:
            log.warning("Chat fetch cancelled, falling back to cached chat metadata")
        except Exception:
            log.exception("Failed to fetch chat via WS")

    def _parse_chat_response(
        self,
        resp: dict,
        requested_chat_id: Any | None = None,
    ) -> None:
        """Parse a CHAT_GET-like response and update known chat metadata."""
        if not isinstance(resp, dict) or not resp or "_max_error" in resp:
            return

        chats = []
        if isinstance(resp.get("chat"), dict):
            chats.append(resp["chat"])

        resp_chats = resp.get("chats")
        if isinstance(resp_chats, dict):
            chats.extend(c for c in resp_chats.values() if isinstance(c, dict))
        elif isinstance(resp_chats, list):
            chats.extend(c for c in resp_chats if isinstance(c, dict))

        if not chats and (
            resp.get("id") == requested_chat_id
            or resp.get("chatId") == requested_chat_id
            or resp.get("title")
            or resp.get("participants")
        ):
            chats.append(resp)

        for chat in chats:
            cid = chat.get("id") or chat.get("chatId") or requested_chat_id
            if cid is None:
                continue
            ctype = chat.get("type")
            title = chat.get("title")

            self.chats_raw[cid] = chat
            if ctype:
                self.chat_types[cid] = ctype
            if title:
                self.chats[cid] = title
            log.info("Fetched chat %s → title=%r type=%s", cid, title, ctype)

    def _deep_extract(self, obj: Any, depth: int) -> None:
        if depth > 5:
            return
        if isinstance(obj, dict):
            uid = obj.get("id") or obj.get("userId")
            name = self._extract_name_from_contact(obj)
            if uid is not None and name and uid not in self.users:
                self.users[uid] = name
                if uid not in self.contacts_raw:
                    self.contacts_raw[uid] = obj
                log.info("Deep-resolved contact %s → %s", uid, name)
            for v in obj.values():
                self._deep_extract(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._deep_extract(item, depth + 1)

    @staticmethod
    def _extract_name_from_contact(c: dict) -> str:
        # Max stores names in a "names" array: [{firstName, lastName, name, type}]
        names_list = c.get("names")
        if isinstance(names_list, list) and names_list:
            n = names_list[0]
            first = n.get("firstName") or ""
            last = n.get("lastName") or ""
            if first or last:
                return f"{first} {last}".strip()
            if n.get("name"):
                return str(n["name"])

        first = c.get("firstName") or c.get("first_name") or ""
        last = c.get("lastName") or c.get("last_name") or ""
        if first or last:
            return f"{first} {last}".strip()

        return str(c.get("friendly") or c.get("displayName") or c.get("name") or "")
