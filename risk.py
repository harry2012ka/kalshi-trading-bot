"""
Risk manager — protects your capital.

The source of truth is the exchange, not the bot's memory. Every few scans the
engine calls sync_from_exchange() with Kalshi's real positions, resting orders
and cash balance. From that the risk manager knows:

  - how many positions are actually open (so the cap frees up when markets settle)
  - how much money is tied up (total exposure cap)
  - today's profit or loss, measured as change in account value since UTC midnight
    (daily loss limit — the bot stops opening trades if it's hit)

Plus a kill switch and duplicate-signal protection.
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("bot.risk")


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(
        self,
        max_position_usd: float = 25.0,
        max_open_positions: int = 10,
        daily_loss_limit_usd: float = 100.0,
        max_total_exposure_usd: float = 250.0,
        max_resting_orders: int = 20,
        ramp_fraction: float = 0.2,
        ramp_floor_usd: float = 1.0,
    ):
        self.max_position_usd = max_position_usd
        self.max_open_positions = max_open_positions
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.max_total_exposure_usd = max_total_exposure_usd
        self.max_resting_orders = max_resting_orders
        # Per-trade size grows with the account: ramp_fraction x account value,
        # never below ramp_floor_usd and never above max_position_usd.
        self.ramp_fraction = ramp_fraction
        self.ramp_floor_usd = ramp_floor_usd
        self.cash_usd = 0.0

        # Synced from the exchange
        self.synced = False
        self.open_positions = 0
        self.resting_orders = 0
        self.total_exposure_usd = 0.0
        self.account_value_usd = 0.0

        # Daily P&L tracking (account value at start of the UTC day)
        self._day = _utc_day()
        self._day_start_value: float | None = None

        # Orders placed since the last sync (so we don't overshoot between syncs)
        self._pending_new = 0
        self._pending_exposure = 0.0

        self._killed = False
        self._recent: dict[str, float] = {}  # signal fingerprint -> time fired
        self.dedup_sec = 60.0

    # ── Kill switch ───────────────────────────────────────────────────────────

    def kill(self, reason: str = "manual"):
        if not self._killed:
            log.critical(f"KILL SWITCH ACTIVATED ({reason}) — no new orders will be placed")
        self._killed = True

    def is_killed(self) -> bool:
        return self._killed

    def reset_kill(self):
        self._killed = False
        log.warning("Kill switch reset — bot resuming")

    # ── Exchange sync ─────────────────────────────────────────────────────────

    def sync_from_exchange(self, cash_usd: float, positions: list[dict],
                           resting_orders: int, mark_price_for):
        """
        cash_usd: available cash on the exchange
        positions: list of dicts with keys ticker, contracts (signed: + YES, - NO), exposure_usd
        resting_orders: number of open (unfilled) orders
        mark_price_for(ticker, side) -> current bid in dollars, or None if unknown
        """
        open_count, exposure, marked_value = 0, 0.0, 0.0
        for p in positions:
            contracts = p["contracts"]
            if contracts == 0:
                continue
            open_count += 1
            exposure += abs(p["exposure_usd"])
            side = "yes" if contracts > 0 else "no"
            bid = mark_price_for(p["ticker"], side)
            # Value at what we could sell for now; fall back to cost if we have no price.
            marked_value += abs(contracts) * bid if bid is not None else abs(p["exposure_usd"])

        self.open_positions = open_count
        self.resting_orders = resting_orders
        self.total_exposure_usd = round(exposure, 2)
        self.account_value_usd = round(cash_usd + marked_value, 2)
        self.cash_usd = round(cash_usd, 2)
        self._pending_new = 0
        self._pending_exposure = 0.0
        self.synced = True

        today = _utc_day()
        if today != self._day or self._day_start_value is None:
            if today != self._day:
                log.info(f"New UTC day — daily P&L reset (yesterday: ${self.daily_pnl():+.2f})")
            self._day = today
            self._day_start_value = self.account_value_usd

        if self.daily_pnl() <= -self.daily_loss_limit_usd and not self._killed:
            log.critical(
                f"Daily loss limit hit: ${self.daily_pnl():.2f} "
                f"(limit -${self.daily_loss_limit_usd:.2f}). Pausing new trades until tomorrow (UTC)."
            )

    def daily_pnl(self) -> float:
        if self._day_start_value is None:
            return 0.0
        return round(self.account_value_usd - self._day_start_value, 2)

    # ── Sizing ────────────────────────────────────────────────────────────────

    def per_trade_cap(self) -> float:
        """Today's max per trade: grows with the account, capped at max_position_usd."""
        ramped = max(self.ramp_floor_usd, self.account_value_usd * self.ramp_fraction)
        return round(min(self.max_position_usd, ramped), 2)

    def spendable_cash(self) -> float:
        """Cash not already committed to orders placed since the last sync."""
        return max(0.0, round(self.cash_usd - self._pending_exposure, 2))

    def budget_for_next_trade(self) -> float:
        return round(min(self.per_trade_cap(), self.spendable_cash()), 2)

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    def can_trade(self, signal, size_usd: float) -> tuple[bool, str]:
        if self._killed:
            return False, "kill switch active"
        if not self.synced:
            return False, "waiting for first sync with exchange"
        if self.daily_pnl() <= -self.daily_loss_limit_usd:
            return False, f"daily loss limit hit (${self.daily_pnl():.2f})"
        if size_usd <= 0:
            return False, "zero size"
        if size_usd > self.per_trade_cap() + 1e-9:
            return False, f"size ${size_usd:.2f} over per-trade cap ${self.per_trade_cap():.2f}"
        if size_usd > self.spendable_cash() + 1e-9:
            return False, f"not enough cash (${self.spendable_cash():.2f} free, need ${size_usd:.2f})"
        if self.open_positions + self._pending_new >= self.max_open_positions:
            return False, f"max open positions reached ({self.open_positions + self._pending_new})"
        if self.resting_orders + self._pending_new >= self.max_resting_orders:
            return False, f"max resting orders reached ({self.resting_orders + self._pending_new})"
        if self.total_exposure_usd + self._pending_exposure + size_usd > self.max_total_exposure_usd:
            return False, (f"total exposure cap: ${self.total_exposure_usd + self._pending_exposure:.2f}"
                           f" + ${size_usd:.2f} > ${self.max_total_exposure_usd:.2f}")
        fp = self._fingerprint(signal)
        fired = self._recent.get(fp)
        if fired and time.time() - fired < self.dedup_sec:
            return False, "duplicate signal (dedup)"
        return True, "ok"

    def record_order(self, signal, size_usd: float):
        """Call right after an order is accepted."""
        self._recent[self._fingerprint(signal)] = time.time()
        self._pending_new += 1
        self._pending_exposure += size_usd
        # keep the dedup map small
        cutoff = time.time() - self.dedup_sec
        self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}

    @staticmethod
    def _fingerprint(signal) -> str:
        return "|".join([
            signal.strategy,
            str(signal.kalshi_ticker or ""),
            str(signal.poly_token_id or ""),
            str(signal.kalshi_side or ""),
            str(signal.poly_side or ""),
        ])

    def status_summary(self) -> dict:
        return {
            "killed": self._killed,
            "synced": self.synced,
            "daily_pnl": self.daily_pnl(),
            "daily_loss_limit": self.daily_loss_limit_usd,
            "account_value": self.account_value_usd,
            "cash": self.cash_usd,
            "per_trade_cap": self.per_trade_cap(),
            "open_positions": self.open_positions,
            "max_positions": self.max_open_positions,
            "resting_orders": self.resting_orders,
            "total_exposure": self.total_exposure_usd,
            "max_total_exposure": self.max_total_exposure_usd,
        }
