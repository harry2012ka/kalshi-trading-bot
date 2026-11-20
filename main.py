"""
Prediction Market Trading Bot
Supports: Kalshi (default) and optionally Polymarket
Strategies: Cross-platform arbitrage, Edge hunting, Market making

Built to run unattended:
  - exchange hiccups are retried, never fatal
  - /health reports what the bot is doing
  - a watchdog restarts the process if trading stalls (Railway brings it back)
"""

import asyncio
import logging
import os
import signal
import sys
import time

from config import BotConfig
from engine import TradingEngine, FatalConfigError
from logger import setup_logging
from file_server import start_file_server


async def watchdog(engine: TradingEngine, log: logging.Logger):
    """If no good scan happens for stall_after_sec, exit so Railway restarts a fresh process."""
    grace = engine.cfg.stall_after_sec
    while True:
        await asyncio.sleep(30)
        if not engine.running:
            return
        ref = engine.last_success_at or engine.started_at
        if engine.connected and time.time() - ref > grace:
            log.critical(f"Watchdog: no successful scan for {time.time() - ref:.0f}s "
                         f"(last error: {engine.last_error or 'none'}). Restarting process.")
            logging.shutdown()
            os._exit(1)


async def main():
    config = BotConfig.from_env()
    setup_logging(config.log_level)
    log = logging.getLogger("bot.main")

    try:
        config.validate()
    except ValueError as e:
        log.critical(str(e))
        log.critical("Fix the settings above in Railway → Variables, then redeploy.")
        # Stay up (so Railway doesn't crash-loop) but report unhealthy on /health
        await start_file_server(lambda: {"healthy": False, "reason": str(e)})
        await asyncio.Event().wait()

    log.info("Starting Prediction Market Bot")
    log.info(f"Mode: {config.mode} | Platforms: {config.platforms} | "
             f"{'DEMO — fake money' if config.kalshi_demo_mode else 'LIVE — REAL MONEY'}")
    log.info(f"Per trade ${config.max_position_usd} | total exposure cap ${config.max_total_exposure_usd} | "
             f"daily loss limit ${config.daily_loss_limit_usd}")

    engine = TradingEngine(config)
    await start_file_server(engine.status)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _shutdown():
        if stopping.is_set():
            return
        stopping.set()
        log.warning("Shutdown signal received — cancelling open orders...")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    bot = asyncio.create_task(engine.run())
    dog = asyncio.create_task(watchdog(engine, log))
    stop = asyncio.create_task(stopping.wait())

    done, _ = await asyncio.wait({bot, stop}, return_when=asyncio.FIRST_COMPLETED)

    if bot in done and bot.exception():
        err = bot.exception()
        if isinstance(err, FatalConfigError):
            engine.last_error = str(err)
            log.critical(f"{err} — fix it in Railway → Variables and redeploy.")
            dog.cancel()
            await asyncio.Event().wait()  # stay up, /health shows the problem
        log.critical(f"Engine crashed: {err!r} — exiting so Railway restarts it")
        sys.exit(1)

    await engine.shutdown()
    dog.cancel()


if __name__ == "__main__":
    asyncio.run(main())
