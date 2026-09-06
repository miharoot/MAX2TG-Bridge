import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    max_token: str
    max_device_id: str
    tg_bot_token: str
    tg_chat_id: str                      # default/fallback Telegram supergroup
    chat_routes: dict[str, int] = field(default_factory=dict)  # max_chat_id -> tg_chat_id
    max_chat_ids: str | None = None
    tg_proxy: str | None = None
    debug: bool = False
    reply_enabled: bool = False
    state_dir: str = "state"
    tg_allowed_user_id: int | None = None


def _parse_chat_routes(raw: str | None) -> dict[str, int]:
    """Parse MAX_CHAT_ROUTES — a JSON object mapping Max chat IDs (as strings,
    the JSON key type) to target Telegram chat IDs, e.g.:

        MAX_CHAT_ROUTES={"-75107924425434": -1002233445566, "123456": -1009988776655}

    Chats not listed here fall back to TG_CHAT_ID once a message arrives, or
    can be bound later at runtime with /bind inside the desired group.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"MAX_CHAT_ROUTES is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("MAX_CHAT_ROUTES must be a JSON object of {max_chat_id: tg_chat_id}")
    result: dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"MAX_CHAT_ROUTES: invalid tg_chat_id for key {key!r}: {value!r}")
    return result


def load_settings() -> Settings:
    load_dotenv()

    required = ["MAX_TOKEN", "MAX_DEVICE_ID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )

    tg_chat_id = os.environ["TG_CHAT_ID"]
    try:
        int(tg_chat_id)
    except ValueError:
        raise SystemExit(
            f"TG_CHAT_ID must be a valid integer, got: {tg_chat_id!r}"
        )

    allowed_raw = os.environ.get("TG_ALLOWED_USER_ID") or None
    allowed_user_id: int | None = None
    if allowed_raw:
        try:
            allowed_user_id = int(allowed_raw)
        except ValueError:
            raise SystemExit(
                f"TG_ALLOWED_USER_ID must be a valid integer, got: {allowed_raw!r}"
            )

    chat_routes = _parse_chat_routes(os.environ.get("MAX_CHAT_ROUTES"))

    return Settings(
        max_token=os.environ["MAX_TOKEN"],
        max_device_id=os.environ["MAX_DEVICE_ID"],
        tg_bot_token=os.environ["TG_BOT_TOKEN"],
        tg_chat_id=tg_chat_id,
        chat_routes=chat_routes,
        max_chat_ids=os.environ.get("MAX_CHAT_IDS") or None,
        tg_proxy=os.environ.get("TG_PROXY") or None,
        debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
        reply_enabled=os.environ.get("REPLY_ENABLED", "").lower() in ("1", "true", "yes"),
        state_dir=os.environ.get("STATE_DIR") or "state",
        tg_allowed_user_id=allowed_user_id,
    )
