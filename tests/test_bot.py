"""
Offline tests for the trading bot. No network, no real exchange, no money.

Run:  python -m unittest discover -s tests -v

A fake Kalshi exchange stands in for the real API so we can check the things that
broke before: the bot must keep trading after its 10th order, honour the daily
loss limit, size trades correctly, cancel stale quotes, and survive outages.
"""

import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# aiohttp isn't needed for these tests; stub it if it isn't installed locally.
try:
    import aiohttp  # noqa: F401
except ImportError:
    stub = types.ModuleType("aiohttp")

    class _ClientError(Exception):
        pass

    stub.ClientError = _ClientError
    stub.ClientSession = object
    stub.ClientTimeout = lambda **kw: None
    stub.ClientWebSocketResponse = object
    stub.WSMsgType = types.SimpleNamespace(TEXT=1, CLOSED=2, ERROR=3)
    sys.modules["aiohttp"] = stub

from kalshi_client import KalshiMarket, KalshiOrder, KalshiAPIError, dollars, count  # noqa: E402
from strategies import contracts_for, MakerStrategy, FavoriteMakerStrategy, TradeSignal  # noqa: E402
from risk import RiskManager  # noqa: E402
from config import BotConfig  # noqa: E402
import engine as engine_mod  # noqa: E402


def market(ticker, bid, ask, vol=10_000, closes_in_min=24 * 60, series=None):
    return KalshiMarket(ticker=ticker, title=f"Market {ticker}", yes_bid=bid, yes_ask=ask,
                        no_bid=round(1 - ask, 2), no_ask=round(1 - bid, 2),
                        yes_mid=(bid + ask) / 2, volume_24h=vol, open_interest=0, status="open",
                        close_ts=time.time() + closes_in_min * 60, series_ticker=series or ticker.split("-")[0])


class FakeKalshi:
    """Minimal stand-in for KalshiClient. Orders rest until the test fills or cancels them."""

    def __init__(self, markets, cash=1000.0):
        self.markets = markets
        self.cash = cash
        self.positions = []        # dicts in Kalshi's newer format
        self.resting = {}          # order_id -> order dict
        self.placed = []
        self.cancelled = []
        self.fail_balance_times = 0
        self.fail_markets = False
        self.fee_types = {}
        self._n = 0

    async def get_balance(self):
        if self.fail_balance_times > 0:
            self.fail_balance_times -= 1
            raise KalshiAPIError(503, "exchange down")
        return self.cash

    async def get_liquid_markets(self):
        if self.fail_markets:
            raise KalshiAPIError(0, "every market request failed")
        return list(self.markets)

    async def get_markets_closing_between(self, lo, hi):
        if self.fail_markets:
            raise KalshiAPIError(0, "every market request failed")
        return [m for m in self.markets if lo <= m.close_ts <= hi]

    async def get_positions(self):
        return list(self.positions)

    async def get_series_fee_type(self, series):
        return self.fee_types.get(series, "quadratic")

    async def get_resting_orders(self):
        return [{"order_id": oid, **o} for oid, o in self.resting.items()]

    async def place_order(self, ticker, side, action, count, order_type, price=None, post_only=False):
        self._n += 1
        oid = f"o{self._n}"
        self.placed.append(dict(ticker=ticker, side=side, count=count, price=price, post_only=post_only))
        self.resting[oid] = {"ticker": ticker, "side": side}
        return KalshiOrder(oid, ticker, side, action, count, price, "resting")

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return self.resting.pop(order_id, None) is not None

    def fill_all(self):
        self.resting.clear()

    async def cancel_all_orders(self):
        self.resting.clear()

    async def close(self):
        pass


def make_engine(fake, **overrides):
    cfg = BotConfig()
    cfg.platforms = ["kalshi"]
    cfg.mode = "maker"
    cfg.db_path = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg.sync_every_scans = 1
    for k, v in overrides.items():
        setattr(cfg, k, v)
    eng = engine_mod.TradingEngine(cfg)
    eng.kalshi = fake
    eng.connected = True
    eng.running = True
    return eng


def run(coro):
    return asyncio.run(coro)


class TestMoneyParsing(unittest.TestCase):
    def test_dollar_fields_new_and_legacy(self):
        self.assertEqual(dollars({"balance_dollars": "12.3400"}, "balance"), 12.34)
        self.assertEqual(dollars({"balance": 1234}, "balance"), 12.34)
        self.assertEqual(dollars({}, "balance"), 0.0)

    def test_count_fields_new_and_legacy(self):
        self.assertEqual(count({"position_fp": "-3.00"}, "position"), -3.0)
        self.assertEqual(count({"position": 5}, "position"), 5.0)


class TestSizing(unittest.TestCase):
    def test_contracts_cost_what_they_should(self):
        self.assertEqual(contracts_for(25, 0.72), 34)       # 34 x $0.72 = $24.48
        self.assertEqual(contracts_for(25, 0.10), 250)
        self.assertEqual(contracts_for(0.50, 0.72), 0)      # can't afford one
        self.assertEqual(contracts_for(25, 0), 0)

    def test_signal_cost_is_dollars_not_cents(self):
        s = TradeSignal(strategy="edge", description="x", kalshi_count=34, kalshi_price=0.72)
        self.assertEqual(s.kalshi_cost_usd, 24.48)

    def test_maker_quotes_are_whole_cents_and_post_only(self):
        q = MakerStrategy(max_position_usd=10).get_kalshi_quotes(market("A", 0.40, 0.50))
        bid, ask = q
        self.assertEqual(bid.kalshi_price, 0.44)
        self.assertEqual(ask.kalshi_price, 0.54)          # NO side = 1 - 0.46
        self.assertTrue(bid.post_only and ask.post_only)
        self.assertLessEqual(bid.kalshi_cost_usd, 10)
        self.assertLessEqual(ask.kalshi_cost_usd, 10)


class TestRisk(unittest.TestCase):
    def sig(self, t="T"):
        return TradeSignal(strategy="edge", description="x", kalshi_ticker=t, kalshi_side="yes")

    def test_blocks_until_first_sync(self):
        r = RiskManager()
        self.assertFalse(r.can_trade(self.sig(), 5)[0])

    def test_position_slots_free_up_after_settlement(self):
        r = RiskManager(max_open_positions=2)
        held = [{"ticker": "A", "contracts": 5, "exposure_usd": 2},
                {"ticker": "B", "contracts": -5, "exposure_usd": 2}]
        r.sync_from_exchange(100, held, 0, lambda t, s: None)
        self.assertIn("max open positions", r.can_trade(self.sig("C"), 5)[1])
        r.sync_from_exchange(104, [], 0, lambda t, s: None)  # both settled
        self.assertTrue(r.can_trade(self.sig("C"), 5)[0])

    def test_daily_loss_limit_trips_and_resets_next_day(self):
        r = RiskManager(daily_loss_limit_usd=50)
        with mock.patch("risk._utc_day", return_value="2026-11-19"):
            r.sync_from_exchange(1000, [], 0, lambda t, s: None)
            r.sync_from_exchange(949, [], 0, lambda t, s: None)
            ok, why = r.can_trade(self.sig(), 5)
            self.assertFalse(ok)
            self.assertIn("daily loss limit", why)
        with mock.patch("risk._utc_day", return_value="2026-11-20"):
            r.sync_from_exchange(949, [], 0, lambda t, s: None)
            self.assertTrue(r.can_trade(self.sig(), 5)[0])

    def test_open_positions_are_marked_to_market(self):
        r = RiskManager(daily_loss_limit_usd=50)
        with mock.patch("risk._utc_day", return_value="2026-11-19"):
            r.sync_from_exchange(1000, [], 0, lambda t, s: None)
            # spent $60 on 100 YES @ 0.60, now bid 0.30 -> worth $30, real loss $30
            pos = [{"ticker": "A", "contracts": 100, "exposure_usd": 60}]
            r.sync_from_exchange(940, pos, 0, lambda t, s: 0.30)
            self.assertEqual(r.daily_pnl(), -30.0)

    def test_total_exposure_cap(self):
        r = RiskManager(max_total_exposure_usd=30)
        r.sync_from_exchange(100, [{"ticker": "A", "contracts": 10, "exposure_usd": 25}], 0, lambda t, s: None)
        self.assertIn("total exposure", r.can_trade(self.sig("B"), 10)[1])

    def test_dedup_window(self):
        r = RiskManager()
        r.sync_from_exchange(100, [], 0, lambda t, s: None)
        r.record_order(self.sig(), 5)
        self.assertIn("duplicate", r.can_trade(self.sig(), 5)[1])


class TestEngine(unittest.TestCase):
    def test_keeps_trading_past_ten_orders(self):
        """The old bot stopped forever after its 10th order. This one must not."""
        fake = FakeKalshi([market(f"M{i}", 0.40, 0.50) for i in range(3)])
        eng = make_engine(fake, max_resting_orders=50, max_open_positions=50)

        async def go():
            for _ in range(12):
                await eng._scan_cycle()
                eng.scan_count += 1
                fake.fill_all()               # quotes fill...
                eng.risk._recent.clear()      # ...and the dedup window passes
        run(go())
        self.assertGreater(len(fake.placed), 10)

    def test_one_quote_set_per_market_and_stale_quotes_cancelled(self):
        fake = FakeKalshi([market("A", 0.40, 0.50)])
        eng = make_engine(fake, maker_refresh_sec=60)

        async def go():
            await eng._scan_cycle()
            eng.risk._recent.clear()
            await eng._scan_cycle()           # quote still live -> no duplicate quotes
            self.assertEqual(len(fake.placed), 2)
            later = time.time() + 120
            with mock.patch("engine.time.time", return_value=later):
                await eng._scan_cycle()       # stale -> cancelled, then re-quoted
        run(go())
        self.assertEqual(len(fake.cancelled), 2)
        self.assertEqual(len(fake.placed), 4)
        self.assertTrue(all(p["post_only"] for p in fake.placed))

    def test_orders_use_dollar_prices_with_correct_size(self):
        fake = FakeKalshi([market("A", 0.40, 0.50)])
        eng = make_engine(fake, maker_size_usd=10)
        run(eng._scan_cycle())
        for p in fake.placed:
            self.assertLessEqual(p["count"] * p["price"], 10.0 + 1e-9)
            self.assertGreaterEqual(p["count"], 1)

    def test_connect_survives_exchange_outage(self):
        fake = FakeKalshi([])
        fake.fail_balance_times = 3
        eng = make_engine(fake)
        eng.connected = False

        async def no_sleep(_):
            return None

        with mock.patch.object(engine_mod, "KalshiClient", return_value=fake), \
             mock.patch("engine.asyncio.sleep", no_sleep):
            run(eng._connect_with_retry())
        self.assertTrue(eng.connected)
        self.assertEqual(fake.fail_balance_times, 0)

    def test_bad_key_is_reported_as_config_error(self):
        eng = make_engine(FakeKalshi([]))
        with mock.patch.object(engine_mod, "KalshiClient", side_effect=ValueError("bad PEM")):
            with self.assertRaises(engine_mod.FatalConfigError):
                run(eng._connect_with_retry())

    def test_health_goes_red_when_scans_stop(self):
        fake = FakeKalshi([market("A", 0.40, 0.50)])
        eng = make_engine(fake, stall_after_sec=600)
        run(eng._scan_cycle())
        self.assertTrue(eng.status()["healthy"])
        fake.fail_markets = True
        with self.assertRaises(KalshiAPIError):
            run(eng._scan_cycle())
        with mock.patch("engine.time.time", return_value=time.time() + 700):
            self.assertFalse(eng.status()["healthy"])

    def test_scan_loop_keeps_going_after_errors(self):
        fake = FakeKalshi([market("A", 0.40, 0.50)])
        fake.fail_markets = True
        eng = make_engine(fake, scan_interval_sec=0)
        calls = {"n": 0}
        real_cycle = eng._scan_cycle

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 4:
                fake.fail_markets = False
            if calls["n"] == 6:
                eng.running = False
            await real_cycle()

        eng._scan_cycle = flaky
        run(eng._run_scan_loop())
        self.assertEqual(calls["n"], 6)
        self.assertEqual(eng.errors_in_a_row, 0)
        self.assertGreater(len(fake.placed), 0)


class TestKalshiOrderBody(unittest.TestCase):
    def test_order_uses_v2_endpoint_yes_leg_pricing(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from kalshi_client import KalshiClient
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        c = KalshiClient("kid", private_key_bytes=pem, demo=True)
        sent = {}

        async def fake_post(path, body):
            sent.update(body)
            return {"order_id": "x", "fill_count": "0.00", "remaining_count": body["count"], "ts_ms": 1}

        c.post = fake_post
        sent_path = {}

        async def fake_post_path(path, body):
            sent_path["p"] = path
            return await fake_post(path, body)

        c.post = fake_post_path
        o = run(c.place_order("T", "no", "buy", 7, "limit", price=0.54, post_only=True))
        # buying NO at $0.54 == selling YES at $0.46 on the V2 endpoint
        self.assertEqual(sent_path["p"], "/portfolio/events/orders")
        self.assertEqual(sent["side"], "ask")
        self.assertEqual(sent["price"], "0.4600")
        self.assertEqual(sent["count"], "7.00")
        self.assertEqual(sent["time_in_force"], "good_till_canceled")
        self.assertTrue(sent["post_only"])
        self.assertEqual(o.status, "resting")

        sent.clear()
        run(c.place_order("T", "yes", "buy", 3, "limit", price=0.44))
        self.assertEqual(sent["side"], "bid")
        self.assertEqual(sent["price"], "0.4400")
        self.assertNotIn("post_only", sent)

        with self.assertRaises(ValueError):
            run(c.place_order("T", "yes", "buy", 0, "limit", price=0.5))


class TestRampAndCash(unittest.TestCase):
    def sig(self, t="T"):
        return TradeSignal(strategy="edge", description="x", kalshi_ticker=t, kalshi_side="yes")

    def test_per_trade_cap_grows_with_account_up_to_ceiling(self):
        r = RiskManager(max_position_usd=10, ramp_fraction=0.2, ramp_floor_usd=1)
        for value, cap in [(0.49, 1.0), (5, 1.0), (20, 4.0), (50, 10.0), (500, 10.0)]:
            r.sync_from_exchange(value, [], 0, lambda t, s: None)
            self.assertEqual(r.per_trade_cap(), cap, f"value ${value}")

    def test_cant_spend_more_cash_than_exists(self):
        r = RiskManager(max_position_usd=10)
        r.sync_from_exchange(0.49, [], 0, lambda t, s: None)
        self.assertEqual(r.budget_for_next_trade(), 0.49)
        self.assertIn("not enough cash", r.can_trade(self.sig(), 0.82)[1])
        self.assertTrue(r.can_trade(self.sig(), 0.40)[0])
        r.record_order(self.sig(), 0.40)
        self.assertEqual(r.budget_for_next_trade(), 0.09)   # pending order counts against cash

    def test_bot_trades_with_49_cents(self):
        """With $0.49 it should place a one-contract order on a cheap market and skip expensive ones."""
        fake = FakeKalshi([market("CHEAP", 0.20, 0.30), market("PRICEY", 0.80, 0.90)], cash=0.49)
        eng = make_engine(fake, max_resting_orders=50)
        run(eng._scan_cycle())
        self.assertTrue(fake.placed, "should place at least one order")
        spent = sum(p["count"] * p["price"] for p in fake.placed)
        self.assertLessEqual(spent, 0.49 + 1e-9)
        self.assertTrue(all(p["price"] <= 0.49 for p in fake.placed))


class TestFavoriteStrategy(unittest.TestCase):
    def ev(self, m, budget=1.0):
        return FavoriteMakerStrategy().evaluate(m, time.time(), budget)

    def test_buys_yes_favorite_at_the_bid_post_only(self):
        s = self.ev(market("A", 0.80, 0.82))
        self.assertEqual((s.kalshi_side, s.kalshi_price, s.kalshi_count), ("yes", 0.80, 1))
        self.assertTrue(s.post_only)

    def test_buys_no_side_when_no_is_the_favorite(self):
        s = self.ev(market("A", 0.15, 0.17))      # NO bid = 0.83
        self.assertEqual((s.kalshi_side, s.kalshi_price), ("no", 0.83))

    def test_skips_longshot_tossup_and_near_certain(self):
        self.assertIsNone(self.ev(market("A", 0.45, 0.47)))   # neither side is 60-95c
        self.assertIsNone(self.ev(market("A", 0.97, 0.98)))   # too expensive, edge gone

    def test_skips_wide_spread_low_volume_combos_and_bad_timing(self):
        self.assertIsNone(self.ev(market("A", 0.70, 0.80)))                       # wide spread
        self.assertIsNone(self.ev(market("A", 0.80, 0.82, vol=10)))               # illiquid
        self.assertIsNone(self.ev(market("KXMVEPARLAY-1", 0.80, 0.82)))           # combo
        self.assertIsNone(self.ev(market("A", 0.80, 0.82, closes_in_min=5)))      # about to close
        self.assertIsNone(self.ev(market("A", 0.80, 0.82, closes_in_min=60*24*30)))  # a month out

    def test_cant_afford(self):
        self.assertIsNone(self.ev(market("A", 0.80, 0.82), budget=0.49))


class TestFavoriteMode(unittest.TestCase):
    def test_49_cents_buys_nothing_it_cant_afford(self):
        fake = FakeKalshi([market("A", 0.80, 0.82)], cash=0.49)
        eng = make_engine(fake, mode="favorite")
        run(eng._scan_cycle())
        self.assertEqual(fake.placed, [])

    def test_places_one_order_per_market_and_skips_held_and_maker_fee_series(self):
        fake = FakeKalshi([market("A-1", 0.80, 0.82), market("B-1", 0.70, 0.72),
                           market("FEE-1", 0.75, 0.77)], cash=5)
        fake.fee_types["FEE"] = "quadratic_with_maker_fees"
        fake.positions = [{"ticker": "B-1", "position_fp": "3.00", "market_exposure_dollars": "2.10"}]
        eng = make_engine(fake, mode="favorite", max_resting_orders=50)

        async def go():
            await eng._scan_cycle()
            eng.risk._recent.clear()
            await eng._scan_cycle()       # quote for A still resting -> no duplicate
        run(go())
        self.assertEqual([p["ticker"] for p in fake.placed], ["A-1"])
        self.assertTrue(fake.placed[0]["post_only"])



class TestSafetyFilters(unittest.TestCase):

    def test_one_bet_per_game(self):
        """Both teams of one game are favorites on different sides — only one order may go out."""
        a = market("KXNBAGAME-26NOV20LALBOS-LAL", 0.80, 0.82)   # YES favorite
        b = market("KXNBAGAME-26NOV20LALBOS-BOS", 0.17, 0.19)   # NO favorite on the other team
        for m in (a, b):
            m.event_ticker = "KXNBAGAME-26NOV20LALBOS"
        fake = FakeKalshi([a, b])
        eng = make_engine(fake, mode="favorite", max_resting_orders=50)
        run(eng._scan_cycle())
        run(eng._scan_cycle())
        self.assertEqual(len(fake.placed), 1, fake.placed)

if __name__ == "__main__":
    unittest.main()


