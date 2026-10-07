"""Background sweep that retries queued outbox items (see app/outbox.py)
until they're actually delivered — or a week has passed (OUTBOX_MAX_AGE).

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
MIN_BACKOFF = 120    # first retry after a failure waits two minutes: a
                      # target that just refused rarely recovers sooner,
                      # and each attempt can mean re-downloading and
                      # re-uploading the whole attachment.
MAX_BACKOFF = 600    # and never more than ten minutes apart, however many
                      # times it's failed: an outage that lasts hours
                      # shouldn't leave a message sitting an hour past the
                      # moment delivery became possible again. A refusal
                      # from MAX itself slows down further (_refusal_wait),
                      # and nothing is retried past OUTBOX_MAX_AGE.


def _backoff_seconds(attempts: int) -> float:
    """Immediate before the first failure, then 2 min, 4 min, 8 min,
    capped at MAX_BACKOFF (10 min)."""
    if attempts <= 0:
        return 0
    return min(MIN_BACKOFF * (2 ** (attempts - 1)), MAX_BACKOFF)


# A message MAX itself keeps refusing (see outbox.RefusedByMax) is not
# worth trying every ten minutes for days: each attempt also posts the
# refusal into the topic again. For the first hour of the same refusal the
# ordinary backoff stays as it is (2, 4, 8 min, then every 10 min); after
# that the attempts get rarer — (refused for at least, then try every):
_HOUR = 60 * 60
REFUSAL_SCHEDULE = (
    (1 * _HOUR, 20 * 60),      # after 1 h — every 20 min
    (3 * _HOUR, _HOUR),        # after 3 h — every hour
    (6 * _HOUR, 6 * _HOUR),    # after 6 h — every 6 hours
    (24 * _HOUR, 24 * _HOUR),  # after a day — once a day
)
# A network failure in between resets the item to the ordinary backoff.

# Anything still undelivered this long after it was queued is dropped,
# whatever the reason — refusal or outage. Past a week a message is more
# likely to confuse than to help, and the queue must not grow forever.
OUTBOX_MAX_AGE = 7 * 24 * _HOUR


def _refusal_wait(refused_for: float) -> float | None:
    """How long to wait between attempts for an item MAX has been refusing
    for ``refused_for`` seconds, or None while the ordinary backoff applies
    (the first hour).

    Keyed to how long the refusal has lasted, not to how many attempts
    there were, so a restart or a missed sweep can't reset it.
    """
    wait = None
    for after, every in REFUSAL_SCHEDULE:
        if refused_for >= after:
            wait = every
    return wait


def _wait_before_retry(item: "outbox.OutboxItem", now: float) -> float:
    """The pause owed before this item's next attempt."""
    if item.refused_since is not None:
        wait = _refusal_wait(now - item.refused_since)
        if wait is not None:
            return wait
    return _backoff_seconds(item.attempts)


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
                    now = time.time()
                    remaining = (_wait_before_retry(item, now)
                                 - (now - item.last_attempt_at))
                    if remaining > 0:
                        continue
                if not client.outbox.try_start(item.id):
                    # Already being delivered — either the original live
                    # send hasn't finished yet, or a previous sweep tick
                    # is still waiting on it. Don't start a duplicate.
                    continue
                try:
                    if await _expire_if_too_old(client, sender, item):
                        continue
                    await _retry_one(client, sender, max_upload_bytes,
                                      item, redeliver_tg_to_max_text, redeliver_tg_to_max_media)
                finally:
                    client.outbox.finish(item.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Outbox retry sweep failed; will try again next tick")


async def _expire_if_too_old(client, sender, item: "outbox.OutboxItem",
                             now: float | None = None) -> bool:
    """Drop an item undelivered for OUTBOX_MAX_AGE; True if it was dropped.

    A message from Telegram is reported into its topic, so the drop is
    visible where it was written. A message from MAX has nowhere better
    to go than the log — Telegram being unreachable may be the very reason
    it is still here.
    """
    now = time.time() if now is None else now
    age = now - item.created_at
    if age < OUTBOX_MAX_AGE:
        return False
    log.warning(
        "Outbox: item id=%s (%s) undelivered for %.1f days, dropping it "
        "(attempts=%d, last error: %s)",
        item.id, item.direction, age / 86400, item.attempts, item.last_error,
    )
    if item.direction in (outbox.TG_TO_MAX_TEXT, outbox.TG_TO_MAX_MEDIA):
        payload = item.payload or {}
        # Quoted rather than replied to: the original may be long deleted,
        # and a reply to a missing message would make the notice itself fail.
        what = (payload.get("text") or payload.get("caption") or "").strip()
        quoted = f"«{what[:100]}{'…' if len(what) > 100 else ''}»" if what else "с вложением"
        try:
            await sender.bot.send_message(
                chat_id=payload.get("tg_chat_id"),
                message_thread_id=payload.get("thread_id"),
                text=f"⚠️ Сообщение {quoted} так и не удалось доставить в MAX за 7 дней — "
                     "больше не пытаюсь. Последняя ошибка: "
                     f"{item.last_error or 'неизвестна'}",
            )
        except Exception:
            log.exception("Outbox: could not report the dropped item id=%s", item.id)
    await client.outbox.remove(item.id)
    return True


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
    except outbox.RefusedByMax as exc:
        # Already reported into the topic. Kept queued — MAX may have had
        # a bad moment — but recorded as a refusal, which is what slows
        # the next attempts down (see _refusal_wait).
        refused_for = await client.outbox.mark_refused(item.id, exc.code, str(exc))
        log.warning(
            "Outbox: item id=%s (%s) refused by MAX for %.0f min (%s)",
            item.id, item.direction, refused_for / 60, exc.code,
        )
        return
    except outbox.PermanentDeliveryFailure as exc:
        # The target refused the message itself, so another attempt can
        # only get the same answer. The reason has already been posted
        # into the topic, so dropping it here is visible, not silent.
        log.warning(
            "Outbox: item id=%s (%s) permanently refused, dropping it: %s",
            item.id, item.direction, exc,
        )
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
