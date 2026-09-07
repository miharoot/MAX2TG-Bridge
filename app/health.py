"""Minimal HTTP health endpoint for Docker/orchestrator healthchecks.

Exposes GET /health returning 200 with {"status": "ok"} when the MAX
WebSocket is currently authorized and connected, or 503 otherwise. Runs on
its own tiny aiohttp server so it doesn't depend on Telegram polling being
enabled.
"""

import logging

from aiohttp import web

from app.max_client import MaxClient

log = logging.getLogger(__name__)


def build_health_app(client: MaxClient) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        if client.is_connected:
            return web.json_response({"status": "ok"})
        return web.json_response({"status": "disconnected"}, status=503)

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def start_health_server(client: MaxClient, port: int) -> web.AppRunner:
    app = build_health_app(client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health endpoint listening on :%d/health", port)
    return runner
