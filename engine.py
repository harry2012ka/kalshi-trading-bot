"""
Trading engine — orchestrates everything.
  - Connects to the exchanges, retrying until they answer (never dies on a blip)
  - Runs the market scan loop
  - Syncs positions, orders and balance from Kalshi so risk limits use real numbers
  - Cancels market-making quotes that have gone stale
  - Executes trades through the risk manager
  - Exposes a status snapshot for the /health endpoint and the watchdog
"""

import asyncio
import logging
import time
from typing import Optional

from config import BotConfig
from kalshi_client import KalshiClient, KalshiAPIError, count as fp_count, dollars, event_of
from matcher import MarketMatcher
from strategies import ArbStrategy, EdgeStrategy, MakerStrategy, FavoriteMakerStrategy, TradeSignal
from risk import RiskManager
from database import TradeDB

log = logging.getLogger("bot.engine")


class FatalConfigError(Exception):
    """A problem a restart can't fix (bad key file, missing settings)."""


class TradingEngine:
    def __init__(self, config: BotConfig):
        self.cfg = config
        self.running = False

        self.kalshi: Optional[KalshiClient] = None
        self.poly = None  # PolymarketClient, only if enabled and configured

        self.matcher = MarketMatcher(min_score=0.4)
        self.risk = RiskManager(
            max_position_usd=config.max_position_usd,
            max_open_positions=config.max_open_positions,
            daily_loss_limit_usd=config.daily_loss_limit_usd,
            max_total_exposure_usd=config.max_total_exposure_usd,
            max_resting_orders=config.max_resting_orders,
            ramp_fraction=config.ramp_fraction,
            ramp_floor_usd=config.ramp_floor_usd,
        )
        self.db = TradeDB(config.db_path)

        self.arb = ArbStrategy(min_edge_pct=config.arb_min_edge_pct,
                               max_position_usd=config.max_position_usd)
        self.edge = EdgeStrategy(min_deviation_pct=config.edge_min_deviation_pct,
                                 kelly_fraction=config.kelly_fraction,
                                 max_position_usd=config.max_position_usd)
        self.maker = MakerStrategy(max_position_usd=config.maker_size_usd)
        self.favorite = FavoriteMakerStrategy(
            min_price=config.fav_min_price, max_price=config.fav_max_price,
            min_minutes_to_close=config.fav_min_minutes_to_close,
            max_days_to_close=config.fav_max_days_to_close,
        )
        self._held_tickers: set[str] = set()

        # Health / bookkeeping
        self.connected = False
        self.started_at = time.time()
        self.last_success_at: Optional[float] = None
        self.last_error: str = ""
        self.errors_in_a_row = 0
        self.scan_count = 0
        self.orders_placed = 0
        self._kalshi_prices: dict = {}           # ticker -> KalshiMarket from the latest scan
        self._our_orders: dict[str, dict] = {}   # order_id -> {placed_at, strategy, ticker}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self):
        self.running = True
        log.info("Trading engine starting...")
        await self._connect_with_retry()
        await self._run_scan_loop()

    async def _connect_with_retry(self):
        """Keep trying to reach the exchanges. Only a broken config stops the bot."""
        if "kalshi" in self.cfg.platforms:
            try:
                self.kalshi = KalshiClient(
                    api_key_id=self.cfg.kalshi_api_key_id,
                    private_key_path=self.cfg.kalshi_private_key_path,
                    private_key_bytes=self.cfg.kalshi_private_key_bytes,
                    demo=self.cfg.kalshi_demo_mode,
                )
            except Exception as e:
                raise FatalConfigError(f"Could not load the Kalshi private key: {e}") from e

            delay = 5
            while self.running:
                try:
                    bal = await self.kalshi.get_balance()
                    log.info(f"Kalshi connected | Balance: ${bal:.2f} | "
                             f"{'DEMO (fake money)' if self.cfg.kalshi_demo_mode else 'LIVE (real money)'}")
                    break
                except KalshiAPIError as e:
                    self.last_error = str(e)
                    hint = " — check KALSHI_API_KEY_ID and the key match the demo/live setting" if e.status in (401, 403) else ""
                    log.error(f"Kalshi not reachable yet: {e}{hint}. Retrying in {delay}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 600)

        if "polymarket" in self.cfg.platforms:
            if not (self.cfg.polymarket_private_key and self.cfg.polymarket_funder_address):
                log.warning("Polymarket is listed in PLATFORMS but its keys aren't set — skipping Polymarket")
            else:
                try:
                    from polymarket_client import PolymarketClient
                    self.poly = PolymarketClient(
                        private_key=self.cfg.polymarket_private_key,
                        funder_address=self.cfg.polymarket_funder_address,
                    )
                    await self.poly.connect()
                    bal = await self.poly.get_balance()
                    log.info(f"Polymarket connected | Balance: ${bal:.2f}")
                except Exception as e:
                    log.error(f"Polymarket failed to connect ({e}) — continuing with Kalshi only")
                    self.poly = None

        if not self.kalshi and not self.poly:
            raise FatalConfigError("No exchange is enabled and configured. Set PLATFORMS=kalshi and the Kalshi keys.")
        self.connected = True

    async def _run_scan_loop(self):
        log.info(f"Scan loop started | interval={self.cfg.scan_interval_sec}s | mode={self.cfg.mode}")
        while self.running:
            try:
                await self._scan_cycle()
                self.scan_count += 1
                self.errors_in_a_row = 0
                if self.scan_count % self.cfg.status_every_scans == 0:
                    self._log_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.errors_in_a_row += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.error(f"Scan error #{self.errors_in_a_row} in a row: {e}", exc_info=True)
            interval = self.cfg.scan_interval_sec
            if self.cfg.mode == "favorite":
                interval = max(interval, 20.0)  # resting orders don't need second-by-second scans
            await asyncio.sleep(interval)

    # ── One scan ──────────────────────────────────────────────────────────────

    async def _scan_cycle(self):
        kalshi_markets, poly_markets = [], []
        fetched_ok = False

        if self.kalshi and self.cfg.mode == "favorite":
            now = time.time()
            kalshi_markets = await self.kalshi.get_markets_closing_between(
                now + self.cfg.fav_min_minutes_to_close * 60,
                now + self.cfg.fav_max_days_to_close * 86400,
            )
            self._kalshi_prices = {m.ticker: m for m in kalshi_markets}
            fetched_ok = True
        elif self.kalshi:
            kalshi_markets = await self.kalshi.get_liquid_markets()  # raises after retries
            self._kalshi_prices = {m.ticker: m for m in kalshi_markets}
            fetched_ok = True
        if self.poly:
            try:
                poly_markets = await self.poly.get_markets(limit=100)
                fetched_ok = True
            except Exception as e:
                log.warning(f"Polymarket market fetch failed: {e}")

        # Real numbers from the exchange before deciding anything
        if self.kalshi and (self.scan_count % self.cfg.sync_every_scans == 0 or not self.risk.synced):
            await self._sync_from_exchange()

        if self.kalshi:
            await self._cancel_stale_quotes()

        if fetched_ok:
            self.last_success_at = time.time()

        bankroll = self.cfg.max_total_exposure_usd

        if self.cfg.mode == "favorite" and kalshi_markets:
            now = time.time()
            placed_before = self.orders_placed
            candidates = 0
            for market in kalshi_markets:
                if market.ticker in self._held_tickers or self._has_live_quote(market.ticker, "favorite"):
                    continue
                # one bet per game: never hold or quote two markets of the same event (e.g. both teams)
                if self._event_busy(market.event_ticker or event_of(market.ticker)):
                    continue
                if self.risk.budget_for_next_trade() < 0.01:
                    break
                # cheap filters first, so the fee lookup only runs for real candidates
                signal = self.favorite.evaluate(market, now, max(self.risk.budget_for_next_trade(), 1.0))
                if not signal:
                    continue
                fee_type = await self.kalshi.get_series_fee_type(market.series_ticker)
                if "maker" in fee_type:
                    continue  # resting orders pay a fee here; the edge is too thin
                candidates += 1
                await self._execute_signal(signal)
            if self.scan_count % 10 == 0:
                log.info(f"FAV scan: {len(kalshi_markets)} markets closing in window | "
                         f"{candidates} candidates | {self.orders_placed - placed_before} orders this scan | "
                         f"{len(self._our_orders)} resting | {len(self._held_tickers)} held | "
                         f"budget ${self.risk.budget_for_next_trade():.2f}")
            return

        if self.cfg.mode in ("arb", "all") and kalshi_markets and poly_markets:
            for pair in self.matcher.find_pairs(kalshi_markets, poly_markets)[:20]:
                signal = self.arb.evaluate(pair, bankroll)
                if signal:
                    await self._execute_signal(signal)

        if self.cfg.mode in ("edge", "all"):
            if kalshi_markets:
                await self.edge.refresh_signals(kalshi_markets)
            for market in kalshi_markets[:50]:
                signal = self.edge.evaluate_kalshi(market, bankroll)
                if signal:
                    await self._execute_signal(signal)
            for market in poly_markets[:50]:
                signal = self.edge.evaluate_poly(market, bankroll)
                if signal:
                    await self._execute_signal(signal)

        if self.cfg.mode in ("maker", "all"):
            for market in kalshi_markets[:10]:
                if self._has_live_quote(market.ticker, "maker"):
                    continue  # one set of quotes per market at a time
                for q in self.maker.get_kalshi_quotes(market) or ():
                    await self._execute_signal(q)
            for market in poly_markets[:10]:
                for q in self.maker.get_poly_quotes(market) or ():
                    await self._execute_signal(q)

    # ── Exchange sync & order hygiene ────────────────────────────────────────

    async def _sync_from_exchange(self):
        cash = await self.kalshi.get_balance()
        raw_positions = await self.kalshi.get_positions()
        resting = await self.kalshi.get_resting_orders()

        positions = [{
            "ticker": p.get("ticker", ""),
            "contracts": fp_count(p, "position"),
            "exposure_usd": dollars(p, "market_exposure"),
        } for p in raw_positions]
        self._held_tickers = {p["ticker"] for p in positions if p["contracts"] != 0}

        if resting is None:
            # Legacy endpoint retired: count only the quotes we know we placed
            resting = [{"order_id": oid} for oid in self._our_orders]
        else:
            resting_ids = {o.get("order_id") for o in resting}
            for oid in list(self._our_orders):
                if oid not in resting_ids:
                    self._our_orders.pop(oid)  # filled or cancelled

        def mark(ticker, side):
            m = self._kalshi_prices.get(ticker)
            if not m:
                return None
            bid = m.yes_bid if side == "yes" else m.no_bid
            return bid if bid > 0 else None

        self.risk.sync_from_exchange(cash, positions, len(resting), mark)

    def _event_busy(self, event: str) -> bool:
        if any(event_of(t) == event for t in self._held_tickers):
            return True
        return any(event_of(o["ticker"]) == event for o in self._our_orders.values())

    def _has_live_quote(self, ticker: str, strategy: str = "maker") -> bool:
        return any(o["ticker"] == ticker and o["strategy"] == strategy for o in self._our_orders.values())

    async def _cancel_stale_quotes(self):
        now = time.time()
        stale = [oid for oid, o in self._our_orders.items()
                 if o["strategy"] in ("maker", "favorite") and now - o["placed_at"] > self.cfg.maker_refresh_sec]
        for oid in stale:
            if await self.kalshi.cancel_order(oid):
                self._our_orders.pop(oid, None)

    # ── Execution ─────────────────────────────────────────────────────────────

    def _fit_to_budget(self, signal: TradeSignal) -> bool:
        """Shrink the Kalshi leg to what we can afford right now. False if not even one contract."""
        if not signal.kalshi_ticker or not signal.kalshi_price:
            return True
        budget = self.risk.budget_for_next_trade()
        affordable = int(budget // signal.kalshi_price + 1e-9) if signal.kalshi_price > 0 else 0
        if affordable < 1:
            return False
        if (signal.kalshi_count or 0) > affordable or not signal.kalshi_count:
            signal.kalshi_count = affordable
        return True

    async def _execute_signal(self, signal: TradeSignal):
        if signal.strategy != "arb" and not self._fit_to_budget(signal):
            log.debug(f"Skip (can't afford 1 contract at ${signal.kalshi_price}): {signal.description}")
            return
        poly_usd = (signal.poly_size_usd or 0) if (signal.poly_token_id and self.poly) else 0
        size_usd = signal.kalshi_cost_usd + poly_usd
        can, reason = self.risk.can_trade(signal, size_usd)
        if not can:
            log.debug(f"Signal blocked: {reason} | {signal.description}")
            return

        signal_id = self.db.log_signal(signal, fired=True)
        log.info(f"Executing: {signal.description} | ${size_usd:.2f}")

        kalshi_ok = True
        if signal.kalshi_ticker and self.kalshi:
            try:
                order = await self.kalshi.place_order(
                    ticker=signal.kalshi_ticker,
                    side=signal.kalshi_side,
                    action=signal.kalshi_action,
                    count=signal.kalshi_count,
                    order_type="limit",
                    price=signal.kalshi_price,
                    post_only=signal.post_only,
                )
                self.orders_placed += 1
                self.risk.record_order(signal, signal.kalshi_cost_usd)
                if order.order_id and order.status == "resting":
                    self._our_orders[order.order_id] = {
                        "placed_at": time.time(), "strategy": signal.strategy, "ticker": signal.kalshi_ticker,
                    }
                self.db.log_order(
                    platform="kalshi", order_id=order.order_id, ticker_or_token=signal.kalshi_ticker,
                    side=signal.kalshi_side, action=signal.kalshi_action, price=signal.kalshi_price,
                    size_usd=signal.kalshi_cost_usd, count=signal.kalshi_count, status=order.status,
                    strategy=signal.strategy, signal_id=signal_id,
                )
            except Exception as e:
                msg = str(e)
                if "post only cross" in msg:
                    log.info(f"Quote skipped (price moved): {signal.description}")
                elif "insufficient_balance" in msg:
                    log.warning(f"Kalshi says not enough balance for: {signal.description}")
                    await self._sync_from_exchange()
                else:
                    log.error(f"Kalshi order failed: {e}")
                kalshi_ok = False

        if signal.poly_token_id and self.poly:
            if signal.strategy == "arb" and not kalshi_ok:
                log.warning("ARB: Kalshi leg failed — skipping Poly leg to avoid naked exposure")
                return
            try:
                if signal.poly_side == "BUY" and signal.strategy != "maker":
                    order = await self.poly.place_market_order(
                        signal.poly_token_id, signal.poly_side, signal.poly_size_usd)
                else:
                    order = await self.poly.place_limit_order(
                        signal.poly_token_id, signal.poly_side, signal.poly_price, signal.poly_size_usd)
                if order:
                    self.orders_placed += 1
                    self.risk.record_order(signal, signal.poly_size_usd or 0)
                    self.db.log_order(
                        platform="polymarket", order_id=order.order_id, ticker_or_token=signal.poly_token_id,
                        side=signal.poly_side, action="buy" if signal.poly_side == "BUY" else "sell",
                        price=signal.poly_price or 0, size_usd=signal.poly_size_usd, count=0,
                        status=order.status, strategy=signal.strategy, signal_id=signal_id,
                    )
            except Exception as e:
                log.error(f"Polymarket order failed: {e}")
                if signal.strategy == "arb" and kalshi_ok:
                    log.critical("ARB: Poly leg failed after Kalshi filled — one-sided position, review manually.")

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        now = time.time()
        since = None if self.last_success_at is None else round(now - self.last_success_at)
        healthy = self.connected and since is not None and since < self.cfg.stall_after_sec
        return {
            "healthy": healthy,
            "connected": self.connected,
            "exchange": "kalshi-demo" if self.cfg.kalshi_demo_mode else "kalshi-live",
            "polymarket": bool(self.poly),
            "mode": self.cfg.mode,
            "uptime_hours": round((now - self.started_at) / 3600, 2),
            "scans": self.scan_count,
            "seconds_since_last_good_scan": since,
            "errors_in_a_row": self.errors_in_a_row,
            "last_error": self.last_error[:300],
            "orders_placed_since_start": self.orders_placed,
            "our_resting_quotes": len(self._our_orders),
            **self.risk.status_summary(),
        }

    def _log_status(self):
        s = self.status()
        log.info(
            f"STATUS | {s['exchange']} | up {s['uptime_hours']}h | scans={s['scans']} | "
            f"value=${s['account_value']:.2f} | cash=${s['cash']:.2f} | per-trade=${s['per_trade_cap']:.2f} | "
            f"today=${s['daily_pnl']:+.2f} | "
            f"open={s['open_positions']}/{s['max_positions']} | exposure=${s['total_exposure']:.2f} | "
            f"resting={s['resting_orders']} | orders={s['orders_placed_since_start']}"
        )

    async def shutdown(self):
        """Graceful shutdown: cancel open orders first."""
        log.warning("Shutdown initiated — cancelling open orders...")
        self.running = False
        tasks = []
        if self.kalshi:
            tasks.append(self.kalshi.cancel_all_orders())
        if self.poly:
            tasks.append(self.poly.cancel_all_orders())
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.kalshi:
            await self.kalshi.close()
        if self.poly:
            await self.poly.close()
        self.db.close()
        log.info("Shutdown complete")
