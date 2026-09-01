import os
from unittest.mock import patch

import pytest

from app.config import Settings, load_settings


def load(env):
    with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
        return load_settings()


BASE = {"TG_BOT_TOKEN": "token", "TG_CHAT_ID": "-100"}


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

def test_settings_are_frozen():
    settings = Settings(tg_bot_token="token", tg_chat_id="-100")
    with pytest.raises((AttributeError, TypeError)):
        settings.debug = True


def test_defaults():
    settings = Settings(tg_bot_token="token", tg_chat_id="-100")
    assert settings.debug is False
    assert settings.reply_enabled is False
    assert settings.max_chat_ids is None
    assert settings.max_ignore_chat_ids is None
    assert settings.tg_allowed_user_ids is None
    assert settings.debug_dump_json is False
    assert settings.max_download_mb == 50
    assert settings.tg_upload_mb == 50
    assert settings.health_port is None
    assert settings.chat_routes == {}


# ---------------------------------------------------------------------------
# load_settings — required fields
# ---------------------------------------------------------------------------

def test_required_fields_populated():
    settings = load(BASE)
    assert settings.tg_bot_token == "token"
    assert settings.tg_chat_id == "-100"


def test_missing_tg_bot_token_raises():
    with pytest.raises(SystemExit, match="TG_BOT_TOKEN"):
        load({"TG_CHAT_ID": "-100"})


def test_invalid_chat_id():
    with pytest.raises(SystemExit, match="TG_CHAT_ID"):
        load({**BASE, "TG_CHAT_ID": "invalid"})


# ---------------------------------------------------------------------------
# load_settings — PyMax auth
# ---------------------------------------------------------------------------

def test_qr_is_default_and_needs_no_extra_credentials():
    settings = load(BASE)
    assert settings.max_pymax_auth == "qr"
    assert settings.max_pymax_session_name == "pymax-qr.db"
    assert settings.max_phone is None


def test_sms_requires_phone():
    with pytest.raises(SystemExit, match="MAX_PHONE"):
        load({**BASE, "MAX_PYMAX_AUTH": "sms"})


def test_sms_configuration():
    settings = load({**BASE, "MAX_PYMAX_AUTH": "sms", "MAX_PHONE": "+79990000000"})
    assert settings.max_phone == "+79990000000"
    assert settings.max_pymax_session_name == "pymax-sms.db"


def test_rejects_unknown_auth_mode():
    with pytest.raises(SystemExit, match="MAX_PYMAX_AUTH"):
        load({**BASE, "MAX_PYMAX_AUTH": "invalid"})


def test_pymax_session_settings_can_be_overridden():
    settings = load({
        **BASE,
        "MAX_PYMAX_WORK_DIR": "cache/max",
        "MAX_PYMAX_SESSION_NAME": "main.db",
        "MAX_2FA_PASSWORD": "secret",
    })
    assert settings.max_pymax_work_dir == "cache/max"
    assert settings.max_pymax_session_name == "main.db"
    assert settings.max_2fa_password == "secret"


def test_pymax_work_dir_defaults_under_state_dir():
    settings = load({**BASE, "STATE_DIR": "runtime-state"})
    assert settings.max_pymax_work_dir == "runtime-state/pymax"


# ---------------------------------------------------------------------------
# load_settings — MAX_CHAT_IDS / MAX_IGNORE_CHAT_IDS
# ---------------------------------------------------------------------------

def test_chat_and_user_filters():
    settings = load({**BASE, "MAX_CHAT_IDS": "-1,-2", "TG_ALLOWED_USER_IDS": "10,20"})
    assert settings.max_chat_ids == "-1,-2"
    assert settings.tg_allowed_user_ids == frozenset({10, 20})


def test_max_chat_ids_none_when_not_set():
    settings = load(BASE)
    assert settings.max_chat_ids is None


def test_max_chat_ids_none_when_empty_string():
    settings = load({**BASE, "MAX_CHAT_IDS": ""})
    assert settings.max_chat_ids is None


def test_ignore_chat_ids_none_when_not_set():
    settings = load(BASE)
    assert settings.max_ignore_chat_ids is None


def test_ignore_chat_ids_populated_when_set():
    settings = load({**BASE, "MAX_IGNORE_CHAT_IDS": "-789,-101112"})
    assert settings.max_ignore_chat_ids == "-789,-101112"


def test_ignore_chat_ids_none_when_empty_string():
    settings = load({**BASE, "MAX_IGNORE_CHAT_IDS": ""})
    assert settings.max_ignore_chat_ids is None


# ---------------------------------------------------------------------------
# load_settings — TG_ALLOWED_USER_IDS
# ---------------------------------------------------------------------------

def test_allowed_user_ids_none_when_not_set():
    settings = load(BASE)
    assert settings.tg_allowed_user_ids is None


def test_allowed_user_ids_plural_list():
    settings = load({**BASE, "TG_ALLOWED_USER_IDS": "100, 200"})
    assert settings.tg_allowed_user_ids == frozenset({100, 200})


def test_allowed_user_id_legacy_singular_still_works():
    settings = load({**BASE, "TG_ALLOWED_USER_ID": "100"})
    assert settings.tg_allowed_user_ids == frozenset({100})


def test_allowed_user_ids_plural_takes_precedence_over_singular():
    settings = load({**BASE, "TG_ALLOWED_USER_IDS": "100", "TG_ALLOWED_USER_ID": "200"})
    assert settings.tg_allowed_user_ids == frozenset({100})


def test_allowed_user_ids_invalid_value_raises():
    with pytest.raises(SystemExit, match="TG_ALLOWED_USER_IDS"):
        load({**BASE, "TG_ALLOWED_USER_IDS": "abc"})


# ---------------------------------------------------------------------------
# load_settings — debug / reply flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("True", True), ("yes", True),
    ("", False), ("0", False), ("no", False),
])
def test_debug_flag_parsing(value, expected):
    settings = load({**BASE, "DEBUG": value})
    assert settings.debug is expected


def test_reply_enabled_default_false():
    settings = load(BASE)
    assert settings.reply_enabled is False


def test_reply_enabled_true_via_1():
    settings = load({**BASE, "REPLY_ENABLED": "1"})
    assert settings.reply_enabled is True


def test_reply_enabled_true_via_yes():
    settings = load({**BASE, "REPLY_ENABLED": "yes"})
    assert settings.reply_enabled is True


def test_reply_enabled_false_via_false():
    settings = load({**BASE, "REPLY_ENABLED": "false"})
    assert settings.reply_enabled is False


def test_debug_dump_json_true_via_true():
    settings = load({**BASE, "DEBUG_DUMP_JSON": "true"})
    assert settings.debug_dump_json is True


def test_returns_settings_instance():
    settings = load(BASE)
    assert isinstance(settings, Settings)


# ---------------------------------------------------------------------------
# load_settings — media limits / health port
# ---------------------------------------------------------------------------

def test_media_limits():
    settings = load({**BASE, "MAX_DOWNLOAD_MB": "25", "TG_UPLOAD_MB": "30"})
    assert settings.max_download_mb == 25
    assert settings.tg_upload_mb == 30


def test_media_limits_reject_non_integer():
    with pytest.raises(SystemExit, match="MAX_DOWNLOAD_MB"):
        load({**BASE, "MAX_DOWNLOAD_MB": "large"})


def test_media_limits_reject_zero():
    with pytest.raises(SystemExit, match="TG_UPLOAD_MB"):
        load({**BASE, "TG_UPLOAD_MB": "0"})


def test_health_port_none_by_default():
    settings = load(BASE)
    assert settings.health_port is None


def test_health_port_can_be_configured():
    settings = load({**BASE, "HEALTH_PORT": "8080"})
    assert settings.health_port == 8080


# ---------------------------------------------------------------------------
# load_settings — MAX_CHAT_ROUTES
# ---------------------------------------------------------------------------

def test_chat_routes_empty_dict_when_not_set():
    settings = load(BASE)
    assert settings.chat_routes == {}


def test_chat_routes_empty_dict_when_empty_string():
    settings = load({**BASE, "MAX_CHAT_ROUTES": ""})
    assert settings.chat_routes == {}


def test_chat_routes_parses_valid_json_mapping():
    settings = load({**BASE, "MAX_CHAT_ROUTES":
                      '{"-75107924425434": -1002233445566, "123456": -1009988776655}'})
    assert settings.chat_routes == {
        "-75107924425434": -1002233445566,
        "123456": -1009988776655,
    }


def test_chat_routes_invalid_json_raises():
    with pytest.raises(SystemExit, match="MAX_CHAT_ROUTES"):
        load({**BASE, "MAX_CHAT_ROUTES": "{not json"})


def test_chat_routes_non_object_json_raises():
    with pytest.raises(SystemExit, match="MAX_CHAT_ROUTES"):
        load({**BASE, "MAX_CHAT_ROUTES": "[1, 2, 3]"})


def test_chat_routes_non_integer_value_raises():
    with pytest.raises(SystemExit, match="MAX_CHAT_ROUTES"):
        load({**BASE, "MAX_CHAT_ROUTES": '{"42": "not-a-number"}'})
