# Kalshi Trading Bot

An automated trading system for Kalshi, the US-regulated prediction market. It runs 24/7 in production with real money, stays inside strict risk limits, and keeps itself running without anyone watching.

This public repo contains the full trading system and an **example strategy**. The strategy used in production lives in a private repo, with extra market filters and tuned settings.

## The idea

Prediction markets show a **favorite–longshot bias**: contracts priced as likely winners pay off slightly more often than their price implies. A study of 300,000+ Kalshi contracts (Bürgi, Deng & Whelan, *Makers and Takers*) found the bias holds, and that traders who post resting orders do far better than traders who cross the spread.

So the bot buys the **favorite** side of a market using **post-only limit orders** at the best bid. It never crosses the spread, which on most Kalshi markets means **zero fees**.

## What the system does

**Trading engine** (`engine.py`)
- Scans every open market closing in a time window, every few seconds.
- Places, tracks, and re-quotes stale orders so it doesn't get picked off when prices move.
- Places one bet per event, never both sides of the same game.

**Risk management** (`risk.py`), all synced from the exchange, not tracked from memory
- Bet size grows with the account: a percentage of account value, with a floor and a hard cap.
- Caps on total exposure, open positions, and resting orders.
- Daily loss limit against account value at UTC midnight. The bot stops for the day if it's hit.
- Every order is sized to the cash actually available. Duplicate-order protection.

**Exchange client** (`kalshi_client.py`)
- RSA-signed requests to the Kalshi v2 API, including the newer order endpoints.
- Retries with backoff on rate limits and server errors. Order placement is never retried blindly.

**Reliability** (`main.py`, `file_server.py`, `Dockerfile`)
- Runs in Docker; deploys automatically on every push to `main`.
- `/health` and `/ready` endpoints. A deploy only goes live after the health check passes.
- A watchdog restarts the process if the scan loop stalls. Open orders are cancelled on shutdown.

**Tests** (`tests/`): 30 offline tests against a fake exchange. No network, no money.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Kalshi API key id + private key
python -m unittest discover -s tests -v
python main.py
```

Set `KALSHI_DEMO_MODE=true` to trade Kalshi's demo exchange with fake money.

## Honest notes

- The edge is small, and research suggests it may be shrinking as markets get more efficient. This is a disciplined, low-stakes system, not a money printer.
- Not financial advice.
