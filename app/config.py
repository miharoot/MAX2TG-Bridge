import json
import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

PyMaxAuthMode = Literal["sms", "qr"]


@dataclass(frozen=True)
class Settings:
    tg_bot_token: str
    tg_chat_id: str                      # default/fallback Telegram supergroup
    chat_routes: dict[str, int] = field(default_factory=dict)  # max_chat_id -> tg_chat_id
    max_pymax_auth: PyMaxAuthMode = "qr"
    max_phone: str | None = None
    max_pymax_work_dir: str = "state/pymax"
    max_pymax_session_name: str = "pymax-sms.db"
    max_2fa_password: str | None = None
    max_chat_ids: str | None = None
    max_ignore_chat_ids: str | None = None
    tg_proxy: str | None = None
    debug: bool = False
    reply_enabled: bool = False
    state_dir: str = "state"
    tg_allowed_user_ids: frozenset[int] | None = None
    max_download_mb: int = 50
    tg_upload_mb: int = 50
    health_port: int | None = None


def _parse_chat_routes(raw: str | None) -> dict[str, int]:
    """Parse MAX_CHAT_ROUTES — a JSON object mapping Max chat IDs (as strings,
    the JSON key type) to target Telegram chat IDs, e.g.:

        MAX_CHAT_ROUTES={"-10000000000005": -1002233445566, "123456": -1009988776655}

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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a valid integer, got: {raw!r}")
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero, got: {value!r}")
    return value


def _normalized_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(name)
    value = (raw or default).strip().lower()
    if value not in allowed:
        expected = ", ".join(sorted(allowed))
        raise SystemExit(f"{name} must be one of: {expected}; got: {raw!r}")
    return value


def load_settings() -> Settings:
    load_dotenv()

    max_pymax_auth = _normalized_choice("MAX_PYMAX_AUTH", "qr", {"sms", "qr"})

    required = ["TG_BOT_TOKEN", "TG_CHAT_ID"]
    if max_pymax_auth == "sms":
        required += ["MAX_PHONE"]

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

    # TG_ALLOWED_USER_IDS (plural, comma-separated) is the current name;
    # TG_ALLOWED_USER_ID (singular) is kept working for existing .env files.
    allowed_raw = (os.environ.get("TG_ALLOWED_USER_IDS")
                   or os.environ.get("TG_ALLOWED_USER_ID") or None)
    allowed_user_ids: frozenset[int] | None = None
    if allowed_raw:
        try:
            allowed_user_ids = frozenset(
                int(value.strip()) for value in allowed_raw.split(",") if value.strip()
            )
        except ValueError:
            raise SystemExit(
                "TG_ALLOWED_USER_IDS must be a comma-separated list of integers, "
                f"got: {allowed_raw!r}"
            )
        if not allowed_user_ids:
            allowed_user_ids = None

    chat_routes = _parse_chat_routes(os.environ.get("MAX_CHAT_ROUTES"))

    state_dir = os.environ.get("STATE_DIR") or "state"
    max_pymax_work_dir = (
        os.environ.get("MAX_PYMAX_WORK_DIR")
        or os.path.join(state_dir, "pymax")
    )
    max_pymax_session_name = (
        os.environ.get("MAX_PYMAX_SESSION_NAME")
        or f"pymax-{max_pymax_auth}.db"
    )

    return Settings(
        tg_bot_token=os.environ["TG_BOT_TOKEN"],
        tg_chat_id=tg_chat_id,
        chat_routes=chat_routes,
        max_pymax_auth=max_pymax_auth,  # type: ignore[arg-type]
        max_phone=os.environ.get("MAX_PHONE") or None,
        max_pymax_work_dir=max_pymax_work_dir,
        max_pymax_session_name=max_pymax_session_name,
        max_2fa_password=os.environ.get("MAX_2FA_PASSWORD") or None,
        max_chat_ids=os.environ.get("MAX_CHAT_IDS") or None,
        max_ignore_chat_ids=os.environ.get("MAX_IGNORE_CHAT_IDS") or None,
        tg_proxy=os.environ.get("TG_PROXY") or None,
        debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
        reply_enabled=os.environ.get("REPLY_ENABLED", "").lower() in ("1", "true", "yes"),
        state_dir=state_dir,
        tg_allowed_user_ids=allowed_user_ids,
        max_download_mb=_int_env("MAX_DOWNLOAD_MB", 50),
        tg_upload_mb=_int_env("TG_UPLOAD_MB", 50),
        health_port=_int_env("HEALTH_PORT", 0) or None,
    )
