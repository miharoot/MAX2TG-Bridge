"""Background sweep that retries queued outbox items (see app/outbox.py)
until they're actually delivered.

Started as a background asyncio task alongside client.run() in
app/main.py, and runs for the lifetime of the process. Never lets a
single bad item or a transient error kill the loop — everything is
caught, logged, and retried on the next tick.
"""

import asyncio
import logging
import time

from app import outbox
from app.pymax_client import MaxMessage
from app.tg_sender import TelegramSender

log = logging.getLogger(__name__)

SWEEP_INTERVAL = 20  # seconds between checks for pending outbox items
MAX_BACKOFF = 600    # cap retry spacing at 10 minutes per item, however
                      # many times it's failed — never give up entirely,
                      # per the "keep retrying, don't drop it" requirement,
                      # but don't hammer a permanently-broken target either.


def _backoff_seconds(attempts: int) -> float:
    """1st retry: immediate. 2nd: 1 min. 3rd: 2 min. 4th: 4 min...
    capped at MAX_BACKOFF."""
    if attempts <= 0:
        return 0
    return min(60 * (2 ** (attempts - 1)), MAX_BACKOFF)


async def run_outbox_retry_loop(client, sender: TelegramSender, max_upload_bytes: int) -> None:
    """Runs forever until cancelled (see app/main.py's shutdown handling)."""
    # Imported here, not at module load time, to avoid a circular import
    # (tg_handler imports from pymax_client/tg_sender; this module would
    # otherwise need to import tg_handler at the same time main.py is
    # still assembling everything).
    from app.tg_handler import redeliver_tg_to_max_media, redeliver_tg_to_max_text

    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL)
            items = await client.outbox.pending()
            if items:
                log.info("Outbox: %d item(s) pending redelivery", len(items))
            for item in items:
                if item.last_attempt_at is not None:
                    remaining = (_backoff_seconds(item.attempts)
                                 - (time.time() - item.last_attempt_at))
                    if remaining > 0:
                        continue
                if not client.outbox.try_start(item.id):
                    # Already being delivered — either the original live
                    # send hasn't finished yet, or a previous sweep tick
                    # is still waiting on it. Don't start a duplicate.
                    continue
                try:
                    await _retry_one(client, sender, max_upload_bytes,
                                      item, redeliver_tg_to_max_text, redeliver_tg_to_max_media)
                finally:
                    client.outbox.finish(item.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Outbox retry sweep failed; will try again next tick")


async def _retry_one(client, sender, max_upload_bytes, item: "outbox.OutboxItem",
                      redeliver_tg_to_max_text, redeliver_tg_to_max_media) -> None:
    log.info("Outbox: retrying %s item id=%s (attempt %d)",
              item.direction, item.id, item.attempts + 1)
    try:
        if item.direction == outbox.MAX_TO_TG:
            msg = MaxMessage(**item.payload)
            await client.redeliver_max_message(msg)
            ok = True
        elif item.direction == outbox.TG_TO_MAX_TEXT:
            ok = await redeliver_tg_to_max_text(client, sender.bot, item.payload)
        elif item.direction == outbox.TG_TO_MAX_MEDIA:
            ok = await redeliver_tg_to_max_media(client, sender.bot, item.payload, max_upload_bytes)
        else:
            log.error("Outbox: unknown direction %r for item id=%s, dropping",
                     item.direction, item.id)
            await client.outbox.remove(item.id)
            return
    except Exception as exc:
        log.exception("Outbox retry failed for item id=%s (direction=%s)",
                      item.id, item.direction)
        await client.outbox.mark_failed(item.id, str(exc))
        return

    if ok:
        log.info("Outbox: item id=%s (%s) delivered on retry", item.id, item.direction)
        await client.outbox.remove(item.id)
    else:
        await client.outbox.mark_failed(item.id, "redelivery returned failure")
