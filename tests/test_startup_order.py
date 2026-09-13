"""The MAX side must not wait on Telegram.

An experiment on the live bridge: with Telegram unreachable, the bot kept
retrying the connection — and a message sent from MAX in the meantime was
never received, never queued in the outbox, simply missed. The cause was
startup order: connecting to Telegram happened before the MAX listener
ran at all.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import bring_up_telegram


class TestBringUpTelegram:
    async def test_it_does_not_block_while_telegram_is_unreachable(self):
        never = asyncio.Event()           # a connect that never answers

        async def _hangs():
            await never.wait()

        sender = MagicMock()
        sender.connect = _hangs
        start_polling = AsyncMock()

        task = asyncio.create_task(bring_up_telegram(sender, start_polling))
        await asyncio.sleep(0)            # let it get as far as it can

        assert not task.done(), "startup must not wait for Telegram"
        start_polling.assert_not_awaited()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_polling_starts_once_the_bot_is_connected(self):
        sender = MagicMock()
        sender.connect = AsyncMock()
        start_polling = AsyncMock()

        await bring_up_telegram(sender, start_polling)

        sender.connect.assert_awaited_once()
        start_polling.assert_awaited_once()

    async def test_without_reply_enabled_only_the_sender_connects(self):
        sender = MagicMock()
        sender.connect = AsyncMock()

        await bring_up_telegram(sender, None)

        sender.connect.assert_awaited_once()

    async def test_a_refused_connection_is_retried_not_raised(self):
        from telegram.error import NetworkError

        sender = MagicMock()
        sender.connect = AsyncMock(side_effect=[NetworkError("proxy down"), None])
        start_polling = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.tg_sender.asyncio.sleep", AsyncMock())
            await bring_up_telegram(sender, start_polling)

        assert sender.connect.await_count == 2
        start_polling.assert_awaited_once()
