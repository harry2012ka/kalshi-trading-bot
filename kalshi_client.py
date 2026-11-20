"""
Kalshi API client
- RSA-PSS authentication (2025+ format)
- Fixed-point prices as dollar strings (March 2026 migration)
- Demo and production support
- WebSocket streaming
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("bot.kalshi")

KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PROD_WS  = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_DEMO_WS  = "wss://demo-api.kalshi.co/trade-api/ws/v2"


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_bid: float   # dollar value 0.0 – 1.0
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_mid: float
    volume_24h: float
    open_interest: float
    status: str
    close_ts: float = 0.0      # unix seconds when trading closes (0 if unknown)
    series_ticker: str = ""
    event_ticker: str = ""     # the game/event this market belongs to (both sides share it)


def event_of(ticker: str) -> str:
    """KXFOO-26NOV20ABC-XYZ -> KXFOO-26NOV20ABC (markets of the same game share this)."""
    return ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ticker


def _iso_ts(v) -> float:
    if not v:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


@dataclass
class KalshiOrder:
    order_id: str
    ticker: str
    side: str    # "yes" or "no"
    action: str  # "buy" or "sell"
    count: int
    price: float
    status: str


class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _Retryable(Exception):
    pass


def dollars(d: dict, key: str, default: float = 0.0) -> float:
    """Read a money field that may come as `key_dollars` ("0.5600") or legacy integer cents."""
    v = d.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = d.get(key)
    if v not in (None, ""):
        return float(v) / 100.0
    return default


def count(d: dict, key: str, default: float = 0.0) -> float:
    """Read a contract-count field that may come as `key_fp` ("10.00") or a legacy integer."""
    v = d.get(f"{key}_fp")
    if v not in (None, ""):
        return float(v)
    v = d.get(key)
    if v not in (None, ""):
        return float(v)
    return default


class KalshiClient:
    def __init__(
        self,
        api_key_id: str,
        private_key_path: str = None,
        demo: bool = True,
        private_key_bytes: bytes = None,
    ):
        self.api_key_id = api_key_id
        self.demo = demo
        self.base_url = KALSHI_DEMO_URL if demo else KALSHI_PROD_URL
        self.ws_url = KALSHI_DEMO_WS if demo else KALSHI_PROD_WS
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

        if private_key_bytes:
            pem_data = private_key_bytes
        else:
            with open(private_key_path, "rb") as f:
                pem_data = f.read()

        self._private_key = serialization.load_pem_private_key(
            pem_data, password=None, backend=default_backend()
        )
        log.info(f"Kalshi client initialized ({'DEMO' if demo else 'LIVE'})")

    def _sign(self, method: str, path: str) -> dict:
        """Generate RSA-PSS signed headers for Kalshi API."""
        ts = str(int(time.time() * 1000))
        # Strip query params before signing
        clean_path = path.split("?")[0]
        # Both demo and live require the full path including the /trade-api/v2 prefix
        sign_path = "/trade-api/v2" + clean_path
        msg = ts + method.upper() + sign_path
        sig = self._private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "Content-Type": "application/json",
        }

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def _request(self, method: str, path: str, params: dict = None, body: dict = None,
                       retries: int = 3) -> dict:
        """One signed request with retries on rate limits, server errors and network blips.
        4xx errors other than 429 are not retried (they mean the request itself is wrong)."""
        delay = 1.0
        for attempt in range(retries + 1):
            session = await self._session_get()
            headers = self._sign(method, path)  # fresh timestamp every attempt
            try:
                async with session.request(method, self.base_url + path, headers=headers,
                                           params=params, json=body) as r:
                    if r.status == 429 or r.status >= 500:
                        raise _Retryable(f"{method} {path} -> HTTP {r.status}")
                    if r.status >= 400:
                        text = await r.text()
                        raise KalshiAPIError(r.status, f"{method} {path} -> HTTP {r.status}: {text[:300]}")
                    return await r.json()
            except (_Retryable, aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == retries:
                    raise KalshiAPIError(0, f"{method} {path} failed after {retries + 1} tries: {e}") from e
                log.warning(f"Kalshi {method} {path} retry {attempt + 1}/{retries} in {delay:.0f}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def get(self, path: str, params: dict = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict) -> dict:
        # Orders are not retried automatically: a timeout could mean it went through,
        # and retrying would double the position.
        return await self._request("POST", path, body=body, retries=0)

    async def delete(self, path: str, params: dict = None) -> dict:
        return await self._request("DELETE", path, params=params)

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Returns balance in dollars."""
        data = await self.get("/portfolio/balance")
        return dollars(data, "balance")

    # Liquid series to scan. The generic /markets endpoint returns unpriced sports
    # parlays by default — fetching by series_ticker is required to get real markets.
    LIQUID_SERIES = [
        "KXGDP", "KXCPI", "KXFED", "KXUNRATE", "KXNONFARM", "KXPCE",
        "KXBTC", "KXETH", "KXINX", "KXNDX",
        "KXSENATE", "KXHOUSE", "KXPRESIDENT",
        "KXOIL", "KXGOLD",
    ]

    async def get_markets(
        self,
        limit: int = 100,
        status: str = "open",
        series_ticker: str = None,
    ) -> list[KalshiMarket]:
        params: dict = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self.get("/markets", params=params)
        return self._parse_markets(data.get("markets", []))

    async def get_markets_closing_between(self, min_close_ts: float, max_close_ts: float,
                                          max_pages: int = 10) -> list[KalshiMarket]:
        """Every open market closing in the given window (paginated). Skips combos and unpriced markets."""
        out: list[KalshiMarket] = []
        cursor = None
        # Combos (KXMVE parlays) flood this list and crowd real markets out of the page limit,
        # so ask Kalshi to leave them out. Fall back if the filter is ever rejected.
        use_mve_filter = getattr(self, "_mve_filter_ok", True)
        for _ in range(max_pages):
            params = {"status": "open", "limit": 1000,
                      "min_close_ts": int(min_close_ts), "max_close_ts": int(max_close_ts)}
            if use_mve_filter:
                params["mve_filter"] = "exclude"
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self.get("/markets", params=params)
            except KalshiAPIError as e:
                if use_mve_filter and e.status == 400:
                    log.warning("mve_filter rejected by Kalshi; scanning without it")
                    self._mve_filter_ok = use_mve_filter = False
                    params.pop("mve_filter", None)
                    data = await self.get("/markets", params=params)
                else:
                    raise
            out.extend(m for m in self._parse_markets(data.get("markets", []))
                       if not m.ticker.startswith("KXMVE") and m.yes_bid > 0 and m.yes_ask > 0)
            cursor = data.get("cursor")
            if not cursor:
                break
        out.sort(key=lambda x: x.volume_24h, reverse=True)
        return out

    async def get_liquid_markets(self, limit_per_series: int = 20) -> list[KalshiMarket]:
        """
        Fetch priced, liquid markets across all known active series in parallel.
        Much more reliable than the generic /markets endpoint which returns unpriced parlays.
        """
        tasks = [
            self.get_markets(limit=limit_per_series, series_ticker=s)
            for s in self.LIQUID_SERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_markets: list[KalshiMarket] = []
        failures = [r for r in results if isinstance(r, Exception)]
        if failures and len(failures) == len(results):
            raise KalshiAPIError(0, f"every market request failed, e.g. {failures[0]}")
        for r in results:
            if isinstance(r, list):
                all_markets.extend(m for m in r if m.yes_bid > 0 and m.yes_ask > 0)
        all_markets.sort(key=lambda x: x.volume_24h, reverse=True)
        return all_markets

    def _parse_markets(self, raw: list) -> list[KalshiMarket]:
        markets = []
        for m in raw:
            try:
                yes_bid = dollars(m, "yes_bid")
                yes_ask = dollars(m, "yes_ask")
                markets.append(KalshiMarket(
                    ticker=m["ticker"],
                    title=m.get("title", ""),
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=dollars(m, "no_bid"),
                    no_ask=dollars(m, "no_ask"),
                    yes_mid=(yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0,
                    volume_24h=count(m, "volume_24h"),
                    open_interest=count(m, "open_interest"),
                    status=m.get("status", ""),
                    close_ts=_iso_ts(m.get("close_time") or m.get("expected_expiration_time")),
                    series_ticker=(m.get("series_ticker") or (m.get("event_ticker") or m["ticker"]).split("-")[0]),
                    event_ticker=(m.get("event_ticker") or event_of(m["ticker"])),
                ))
            except Exception as e:
                log.debug(f"Skipping malformed market {m.get('ticker')}: {e}")
        markets.sort(key=lambda x: x.volume_24h, reverse=True)
        return markets

    async def get_series_fee_type(self, series_ticker: str) -> str:
        """Fee type for a series, e.g. 'quadratic' or 'quadratic_with_maker_fees'. Cached; '' if unknown."""
        cache = self.__dict__.setdefault("_fee_cache", {})
        if series_ticker in cache:
            return cache[series_ticker]
        fee_type = ""
        try:
            data = await self.get(f"/series/{series_ticker}")
            fee_type = (data.get("series") or {}).get("fee_type", "") or ""
        except Exception as e:
            log.debug(f"fee type lookup failed for {series_ticker}: {e}")
        cache[series_ticker] = fee_type
        return fee_type

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        data = await self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook", {})

    # ── Order management ──────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,       # "yes" or "no" — the contract we want
        action: str,     # "buy" or "sell"
        count: int,      # number of contracts
        order_type: str, # only "limit" is supported by the V2 endpoint
        price: float = None,  # dollars for the chosen side, 0.01 - 0.99
        post_only: bool = False,
    ) -> KalshiOrder:
        """
        Place an order via Create Order (V2): POST /portfolio/events/orders.
        The old POST /portfolio/orders now answers 410 Gone.

        V2 quotes everything on the YES leg: side is "bid" (buy YES) or "ask" (sell YES).
        Buying NO at $p is the same as selling YES at $(1-p), so we translate.
        """
        if count < 1:
            raise ValueError(f"order count must be >= 1, got {count}")
        if order_type != "limit":
            raise ValueError("only limit orders are supported")
        if price is None or not (0.0 < price < 1.0):
            raise ValueError(f"limit price must be between 0 and 1 dollars, got {price}")

        buys_yes = (side == "yes") == (action == "buy")
        yes_price = price if side == "yes" else round(1.0 - price, 4)
        body = {
            "ticker": ticker,
            "side": "bid" if buys_yes else "ask",
            "count": f"{int(count)}.00",
            "price": f"{yes_price:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "maker",
            "client_order_id": str(uuid.uuid4()),
        }
        if post_only:
            body["post_only"] = True

        resp = await self.post("/portfolio/events/orders", body)
        order = resp.get("order", resp)  # V2 returns fields at the top level
        filled = count_fp = 0.0
        try:
            filled = float(order.get("fill_count") or 0)
            count_fp = float(order.get("remaining_count") or 0)
        except (TypeError, ValueError):
            pass
        status = "resting" if count_fp > 0 else ("executed" if filled > 0 else order.get("status", ""))
        log.info(f"Kalshi order placed: {ticker} {action} {count}x {side} @ ${price}"
                 f" (YES {body['side']} @ {body['price']}){' post-only' if post_only else ''} -> {status}")
        return KalshiOrder(
            order_id=order.get("order_id", ""),
            ticker=ticker,
            side=side,
            action=action,
            count=int(count),
            price=price,
            status=status,
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self.delete(f"/portfolio/events/orders/{order_id}")
            log.info(f"Kalshi order cancelled: {order_id}")
            return True
        except Exception as e:
            log.error(f"Failed to cancel Kalshi order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders by fetching and cancelling each one."""
        try:
            orders = await self.get_resting_orders()
            if orders is None:
                await self.delete("/portfolio/events/orders")  # V2 cancel-all
                log.info("Kalshi: cancelled all open orders (V2)")
                return -1
            if not orders:
                return 0
            results = await asyncio.gather(
                *[self.cancel_order(o["order_id"]) for o in orders],
                return_exceptions=True,
            )
            cancelled = sum(1 for r in results if r is True)
            log.info(f"Kalshi: cancelled {cancelled}/{len(orders)} open orders")
            return cancelled
        except Exception as e:
            log.error(f"Kalshi cancel_all failed: {e}")
            return 0

    async def get_resting_orders(self):
        """Open orders, or None if Kalshi has retired this legacy endpoint (410)."""
        try:
            data = await self.get("/portfolio/orders", params={"status": "resting"})
        except KalshiAPIError as e:
            if e.status == 410:
                log.warning("GET /portfolio/orders is gone (410) — falling back to the bot's own order tracking")
                return None
            raise
        return data.get("orders", [])

    async def get_positions(self) -> list[dict]:
        """Open market positions. Paginates so nothing is missed."""
        out, cursor = [], None
        for _ in range(20):
            params = {"limit": 200, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            data = await self.get("/portfolio/positions", params=params)
            out.extend(data.get("market_positions", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    # ── WebSocket streaming ───────────────────────────────────────────────────

    async def stream_markets(self, tickers: list[str], callback):
        """
        Stream real-time orderbook updates via WebSocket.
        callback(ticker, yes_bid, yes_ask) is called on each update.
        """
        headers = self._sign("GET", "/trade-api/ws/v2")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.ws_url, headers=headers) as ws:
                self._ws = ws
                # Subscribe to orderbook channels
                sub_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": tickers,
                    }
                }
                await ws.send_json(sub_msg)
                log.info(f"Kalshi WS subscribed to {len(tickers)} markets")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            msg_type = data.get("type")
                            if msg_type in ("orderbook_snapshot", "orderbook_delta"):
                                ticker = data.get("market_ticker", "")
                                yes_bids = data.get("yes", {}).get("bids", [])
                                yes_asks = data.get("yes", {}).get("asks", [])
                                if yes_bids and yes_asks:
                                    best_bid = float(yes_bids[0][0])
                                    best_ask = float(yes_asks[0][0])
                                    await callback(ticker, best_bid, best_ask)
                        except Exception as e:
                            log.debug(f"Kalshi WS parse error: {e}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log.warning("Kalshi WebSocket closed/error — will reconnect")
                        break

    async def close(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
