import logging

import qrcode

from app.config import Settings

from pymax import Client, ExtraConfig, WebClient

log = logging.getLogger(__name__)


class EnvPasswordProvider:
    def __init__(self, password: str):
        self._password = password

    async def get_password(self, hint: str | None = None) -> str:
        return self._password


class LogQrHandler:
    """Shows the login QR code through the app's own logger instead of
    pymax's ``ConsoleQrHandler`` (which writes cp437 half-block chars
    straight to stdout).

    That matters when running under systemd without a TTY: journald
    services often start in the ``C``/``POSIX`` locale, so raw non-ASCII
    writes to stdout can throw ``UnicodeEncodeError`` (or just render as
    garbage in some log viewers/fonts), and writing outside the logger
    means the QR can get interleaved with other async log lines. This
    renders with plain ``#``/space ASCII (safe under any encoding) and
    doubles each module both ways to keep it roughly square and
    scannable, then logs it one line at a time.
    """

    async def show_qr(self, qr_url: str) -> None:
        qr = qrcode.QRCode(border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)

        log.warning("PyMax QR authorization URL: %s", qr_url)
        for row in qr.modules:
            line = "".join("##" if cell else "  " for cell in row)
            # print each module row twice: monospace glyphs are roughly
            # twice as tall as wide, so this keeps modules square-ish
            log.warning(line)
            log.warning(line)


def build_pymax_client(settings: Settings):
    """Build a PyMax client for the configured primary auth flow.

    Imports stay local so configuration errors remain easy to diagnose.
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
            qr_provider=LogQrHandler(),
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
