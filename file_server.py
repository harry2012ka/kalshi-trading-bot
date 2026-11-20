"""
Health-check server on the PORT the host exposes.
  /health  — JSON status of the trading engine (200 healthy / 503 not)
  /ready   — 200 as soon as the process is up (used as the deploy health check)
"""

import json
import logging
import os

from aiohttp import web

log = logging.getLogger("file_server")


async def health(request):
    status_fn = request.app.get("status_fn")
    if status_fn is None:
        return web.json_response({"healthy": False, "reason": "bot not started"}, status=503)
    try:
        status = status_fn()
    except Exception as e:
        return web.json_response({"healthy": False, "reason": str(e)}, status=503)
    return web.Response(text=json.dumps(status, indent=2), content_type="application/json",
                        status=200 if status.get("healthy") else 503)


def create_app(status_fn=None) -> web.Application:
    app = web.Application()
    app["status_fn"] = status_fn
    app.router.add_get("/health", health)
    app.router.add_get("/ready", lambda request: web.Response(text="ok"))
    return app


async def start_file_server(status_fn=None):
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(create_app(status_fn))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Health server running on port {port}")
    return runner
