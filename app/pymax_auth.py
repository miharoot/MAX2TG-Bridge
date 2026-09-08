import io
import logging
from typing import TYPE_CHECKING

import qrcode

from app.config import Settings

from pymax import Client, ExtraConfig, WebClient

if TYPE_CHECKING:
    from app.pymax_client import PyMaxClient

log = logging.getLogger(__name__)


class EnvPasswordProvider:
    def __init__(self, password: str):
        self._password = password

    async def get_password(self, hint: str | None = None) -> str:
        return self._password


class LogQrHandler:
    """Shows the login QR code as a real PNG broadcast to Telegram (via
    ``bridge_client.notify_qr``, see app/pymax_client.py) — much more
    reliably scannable than terminal ASCII art — and also logs it
    through the app's own logger as a fallback for when Telegram isn't
    reachable yet.

    The ASCII fallback deliberately avoids pymax's own
    ``ConsoleQrHandler``, which writes cp437 half-block chars straight
    to stdout: under systemd without a TTY, journald services often
    start in the ``C``/``POSIX`` locale, so raw non-ASCII writes can
    throw ``UnicodeEncodeError`` or render as garbage. This uses plain
    ``#``/space ASCII (safe under any encoding), doubled both ways to
    stay roughly square.
    """

    def __init__(self, bridge_client: "PyMaxClient | None" = None):
        self._bridge_client = bridge_client

    async def show_qr(self, qr_url: str) -> None:
        qr = qrcode.QRCode(border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)

        log.warning("PyMax QR authorization URL: %s", qr_url)
        for row in qr.modules:
            # 2 chars wide, 1 line tall per module: a monospace glyph is
            # roughly twice as tall as it is wide, so this (not doubling
            # the line too) is what keeps modules square instead of
            # rendering as tall rectangles that break scanning.
            line = "".join("##" if cell else "  " for cell in row)
            log.warning(line)

        if self._bridge_client is None:
            return
        try:
            img = qr.make_image()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            await self._bridge_client.notify_qr(qr_url, buf.getvalue())
        except Exception:
            log.exception("Failed to render/send QR PNG; ASCII log above is the fallback")


def build_pymax_client(settings: Settings, bridge_client: "PyMaxClient | None" = None):
    """Build a PyMax client for the configured primary auth flow.

    Imports stay local so configuration errors remain easy to diagnose.
    ``bridge_client`` (optional) lets the QR handler broadcast the code
    via ``bridge_client.notify_qr`` instead of only logging it.
    """
    try:
        from pymax import Client, ExtraConfig, WebClient
    except ImportError as exc:
        raise RuntimeError(
            "PyMax requires maxapi-python to be installed."
        ) from exc

    extra_config = ExtraConfig(
        proxy=settings.tg_proxy,
        log_level="DEBUG" if settings.debug else "INFO",
    )

    if settings.max_pymax_auth == "qr":
        return WebClient(
            work_dir=settings.max_pymax_work_dir,
            session_name=settings.max_pymax_session_name,
            extra_config=extra_config,
            qr_provider=LogQrHandler(bridge_client),
        )

    if not settings.max_phone:
        raise RuntimeError("MAX_PYMAX_AUTH=sms requires MAX_PHONE.")

    password_provider = (
        EnvPasswordProvider(settings.max_2fa_password)
        if settings.max_2fa_password
        else None
    )
    return Client(
        phone=settings.max_phone,
        work_dir=settings.max_pymax_work_dir,
        session_name=settings.max_pymax_session_name,
        extra_config=extra_config,
        password_provider=password_provider,
    )
