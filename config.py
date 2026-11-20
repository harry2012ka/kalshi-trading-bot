"""
Configuration — all settings loaded from environment variables.
Copy .env.example to .env and fill in your credentials.
"""

import base64
import os
from dataclasses import dataclass, field
from typing import Literal, Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class BotConfig:
    # ── Platform toggles ──────────────────────────────────────────────────────
    platforms: list[str] = field(default_factory=lambda: ["kalshi"])

    # ── Strategy mode ─────────────────────────────────────────────────────────
    # "arb"        → cross-platform arbitrage only
    # "maker"      → market making on both platforms
    # "edge"       → edge hunting (mispriced markets)
    # "all"        → arb + maker + edge combined
    # "favorite"   → buy favorites (60-95c) with resting orders, hold to settlement (default)
    mode: str = "favorite"   # evidence-based default; see FavoriteMakerStrategy

    # ── Risk controls ─────────────────────────────────────────────────────────
    max_position_usd: float = 10.0       # ceiling per trade; the ramp grows toward this
    ramp_fraction: float = 0.2           # per-trade size = 20% of account value...
    ramp_floor_usd: float = 1.0          # ...but at least $1 (or whatever cash allows)
    max_open_positions: int = 10         # max simultaneous open positions
    daily_loss_limit_usd: float = 100.0  # bot pauses if daily loss hits this
    arb_min_edge_pct: float = 2.0        # min arb gap % to fire (after fees)
    edge_min_deviation_pct: float = 5.0  # min model vs market gap to trade
    kelly_fraction: float = 0.25         # fractional Kelly sizing (0.25 = quarter Kelly)
    max_total_exposure_usd: float = 50.0   # cap on all money tied up in positions at once
    max_resting_orders: int = 20         # cap on unfilled orders sitting on the book
    fav_min_price: float = 0.60          # favorite strategy: cheapest contract it will buy
    fav_max_price: float = 0.95          # ...and the most expensive
    fav_min_minutes_to_close: float = 15 # skip markets about to close
    fav_max_days_to_close: float = 14    # skip markets that tie money up for weeks
    maker_size_usd: float = 10.0         # $ per side for each market-making quote
    maker_refresh_sec: float = 120.0     # cancel and re-quote maker orders older than this

    # ── Kalshi credentials ────────────────────────────────────────────────────
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "keys/kalshi_private.pem"
    kalshi_private_key_bytes: Optional[bytes] = None  # set when using B64 env var
    kalshi_demo_mode: bool = True        # START IN DEMO — set False for live

    # ── Polymarket credentials ────────────────────────────────────────────────
    polymarket_private_key: str = ""     # your Polygon wallet private key (0x...)
    polymarket_funder_address: str = ""  # your Polymarket proxy/funder address

    # ── Infrastructure ────────────────────────────────────────────────────────
    scan_interval_sec: float = 5.0       # how often to scan for new opportunities
    heartbeat_interval_sec: float = 20.0 # WebSocket heartbeat (Polymarket requires this)
    db_path: str = "data/trades.db"
    log_level: str = "INFO"
    sync_every_scans: int = 6            # re-read positions/balance from Kalshi every N scans
    status_every_scans: int = 60         # log a STATUS line every N scans (~5 min)
    stall_after_sec: float = 600.0       # watchdog restarts the bot if no good scan for this long

    @classmethod
    def from_env(cls) -> "BotConfig":
        cfg = cls()
        cfg.kalshi_api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        cfg.kalshi_demo_mode = os.getenv("KALSHI_DEMO_MODE", "true").lower() == "true"

        # Support key as base64 env var (for Railway/cloud) or local file path
        key_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "")
        if key_b64:
            # Strip ALL whitespace before decoding — Railway's UI injects spaces/newlines
            cfg.kalshi_private_key_bytes = base64.b64decode("".join(key_b64.split()))
        else:
            cfg.kalshi_private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "keys/kalshi_private.pem")

        cfg.polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        cfg.polymarket_funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

        cfg.mode = os.getenv("BOT_MODE", "favorite")
        cfg.fav_min_price = float(os.getenv("FAV_MIN_PRICE", "0.60"))
        cfg.fav_max_price = float(os.getenv("FAV_MAX_PRICE", "0.95"))
        cfg.fav_min_minutes_to_close = float(os.getenv("FAV_MIN_MINUTES_TO_CLOSE", "15"))
        cfg.fav_max_days_to_close = float(os.getenv("FAV_MAX_DAYS_TO_CLOSE", "14"))
        cfg.max_position_usd = float(os.getenv("MAX_POSITION_USD", "10"))
        cfg.ramp_fraction = float(os.getenv("RAMP_FRACTION", "0.2"))
        cfg.ramp_floor_usd = float(os.getenv("RAMP_FLOOR_USD", "1"))
        cfg.daily_loss_limit_usd = float(os.getenv("DAILY_LOSS_LIMIT_USD", "100"))
        cfg.arb_min_edge_pct = float(os.getenv("ARB_MIN_EDGE_PCT", "2.0"))
        cfg.edge_min_deviation_pct = float(os.getenv("EDGE_MIN_DEVIATION_PCT", "5.0"))
        cfg.kelly_fraction = float(os.getenv("KELLY_FRACTION", "0.25"))
        cfg.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
        cfg.max_total_exposure_usd = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "50"))
        cfg.max_resting_orders = int(os.getenv("MAX_RESTING_ORDERS", "20"))
        cfg.maker_size_usd = float(os.getenv("MAKER_SIZE_USD", "10"))
        cfg.maker_refresh_sec = float(os.getenv("MAKER_REFRESH_SEC", "120"))
        cfg.scan_interval_sec = float(os.getenv("SCAN_INTERVAL_SEC", "5"))
        cfg.stall_after_sec = float(os.getenv("STALL_AFTER_SEC", "600"))
        cfg.log_level = os.getenv("LOG_LEVEL", "INFO")

        # Determine which platforms are enabled
        platforms_env = os.getenv("PLATFORMS", "kalshi")
        cfg.platforms = [p.strip().lower() for p in platforms_env.split(",") if p.strip()]

        return cfg

    def validate(self):
        errors = []
        if "kalshi" in self.platforms:
            if not self.kalshi_api_key_id:
                errors.append("KALSHI_API_KEY_ID is not set")
            if not self.kalshi_private_key_bytes and not os.path.exists(self.kalshi_private_key_path):
                errors.append("No Kalshi private key: set KALSHI_PRIVATE_KEY_B64 "
                              f"or put the key file at {self.kalshi_private_key_path}")
        if self.mode not in ("arb", "edge", "maker", "all", "favorite"):
            errors.append(f"BOT_MODE must be favorite, arb, edge, maker or all (got {self.mode!r})")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  - {e}" for e in errors))
