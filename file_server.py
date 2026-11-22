"""
Health-check server on the PORT the host exposes.
  /health  — 200 healthy / 503 not. The public answer is only
             {"healthy", "seconds_since_last_good_scan"}. Full detail (cash, P&L,
             positions, last error) only when HEALTH_TOKEN is set and sent as
             ?token=... or "Authorization: Bearer ...".
  /ready   — 200 as soon as the process is up (used as the deploy health check)
"""

import hmac
import json
import logging
import os

try:
    from aiohttp import web
except ImportError:  # offline tests stub aiohttp without .web; the pure helpers below still work
    web = None

log = logging.getLogger("file_server")

PUBLIC_HEALTH_KEYS = ("healthy", "seconds_since_last_good_scan")


# ── Pure helpers (tested offline) ────────────────────────────────────────────

def token_ok(supplied: str, expected: str = None) -> bool:
    """Constant-time token check. No HEALTH_TOKEN set means nobody gets the detail."""
    expected = os.getenv("HEALTH_TOKEN", "") if expected is None else expected
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def supplied_token(query, headers) -> str:
    tok = query.get("token", "") or ""
    auth = headers.get("Authorization", "") or ""
    if not tok and auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    return tok


def health_payload(status: dict, authorized: bool) -> dict:
    if authorized:
        return status
    return {k: status.get(k) for k in PUBLIC_HEALTH_KEYS}


# ── Routes ───────────────────────────────────────────────────────────────────

async def health(request):
    status_fn = request.app.get("status_fn")
    if status_fn is None:
        status = {"healthy": False, "reason": "bot not started"}
    else:
        try:
            status = status_fn()
        except Exception as e:
            status = {"healthy": False, "reason": str(e)}
    authorized = token_ok(supplied_token(request.query, request.headers))
    return web.Response(text=json.dumps(health_payload(status, authorized), indent=2),
                        content_type="application/json",
                        status=200 if status.get("healthy") else 503)


def create_app(status_fn=None):
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
