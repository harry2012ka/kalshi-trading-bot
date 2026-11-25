"""
Performance and calibration report. READ-ONLY: never places, cancels or changes an order,
never touches the network.

Input: a JSON-lines file of bets, one object per line:

    {"key": "TICKER yes", "price": 0.78, "stake": 0.78, "ts": 1700000000,
     "won": true, "pnl": 0.22, "fee": 0.0}

  price  average price paid per contract (= the market's implied probability we win)
  won    true / false once settled, null while open
  pnl    realized P&L net of fees, null while open

For the bets it reports: settled count, win rate (95% Wilson interval), average price paid,
win rate minus price, a z-score of wins against what the prices implied, Brier score,
realized P&L, fees, return on stake, max drawdown, and a calibration table per 5c price
bucket. If the favorite-longshot bias is real for this bot, the win rate in each bucket
sits ABOVE the price paid. |z| < 2 means the sample can't tell yet.

    python -m report bets.jsonl
    python -m report bets.jsonl --json

The private production version reads the bot's own fill and settlement records instead of
a bets file; the statistics are the same. See scripts/synthetic_report.py for a demo.
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Optional

BUCKET = 0.05  # 5-cent calibration buckets

@dataclass
class Bet:
    key: str
    price: float                   # average price paid per contract = implied probability of winning
    stake: float                   # dollars at risk
    ts: float = 0.0                # when the bet was opened (used to order the P&L curve)
    won: Optional[bool] = None     # None while open, or closed early without a settlement
    pnl: Optional[float] = None    # net of fees; None while open
    fee: float = 0.0

    @property
    def settled(self) -> bool:
        return self.pnl is not None


@dataclass
class Tracker:
    name: str
    money: str                     # "real" or "paper"
    bets: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# ── Reader ─────────────────────────────────────────────────────────────────

def load_bets(path: str, name: str = "bets", money: str = "real") -> Tracker:
    """Read a bets.jsonl file. Malformed lines are skipped, a missing file gives no bets."""
    t = Tracker(name, money)
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    price = float(r["price"])
                    if not 0 < price < 1:
                        continue
                    won = r.get("won")
                    pnl = r.get("pnl")
                    t.bets.append(Bet(key=str(r.get("key", "")), price=price,
                                      stake=float(r.get("stake", price)), ts=float(r.get("ts") or 0),
                                      won=None if won is None else bool(won),
                                      pnl=None if pnl is None else float(pnl), fee=float(r.get("fee") or 0)))
                except Exception:
                    continue
    except (FileNotFoundError, OSError):
        pass
    return t


# ── Statistics ─────────────────────────────────────────────────────────────

def wilson(wins: int, n: int, z: float = 1.96):
    if n == 0:
        return None, None
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(centre - half, 3), round(centre + half, 3)


def max_drawdown(pnls: list) -> float:
    """Largest peak-to-trough fall of the cumulative P&L curve (starting at 0), as a positive number."""
    peak = equity = worst = 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 2)


def bucket_label(price: float) -> str:
    lo = min(int(price / BUCKET + 1e-9), int(1 / BUCKET) - 1) * BUCKET
    return f"{round(lo * 100):02d}-{round((lo + BUCKET) * 100) - 1:02d}c"


def calibration(bets: list) -> list:
    rows: dict = {}
    for b in bets:
        if b.won is None:
            continue
        r = rows.setdefault(bucket_label(b.price), {"n": 0, "wins": 0, "sum_price": 0.0, "pnl": 0.0})
        r["n"] += 1
        r["wins"] += int(b.won)
        r["sum_price"] += b.price
        r["pnl"] += b.pnl or 0.0
    out = []
    for label in sorted(rows):
        r = rows[label]
        avg = r["sum_price"] / r["n"]
        wr = r["wins"] / r["n"]
        lo, hi = wilson(r["wins"], r["n"])
        out.append({"bucket": label, "n": r["n"], "avg_price": round(avg, 3), "win_rate": round(wr, 3),
                    "edge": round(wr - avg, 3), "ci_low": lo, "ci_high": hi, "pnl": round(r["pnl"], 2)})
    return out


def summarize(t: Tracker) -> dict:
    settled = sorted([b for b in t.bets if b.settled], key=lambda b: b.ts)
    scored = [b for b in settled if b.won is not None]
    n = len(scored)
    wins = sum(1 for b in scored if b.won)
    expected = sum(b.price for b in scored)
    var = sum(b.price * (1 - b.price) for b in scored)
    pnl = sum(b.pnl for b in settled)
    fees = sum(b.fee for b in settled)
    staked = sum(b.stake for b in settled)
    lo, hi = wilson(wins, n)
    return {
        "tracker": t.name,
        "money": t.money,
        "bets": len(t.bets),
        "settled": len(settled),
        "open": len(t.bets) - len(settled),
        "wins": wins,
        "win_rate": round(wins / n, 3) if n else None,
        "win_rate_ci95": [lo, hi] if n else None,
        "avg_price": round(expected / n, 3) if n else None,
        "edge_vs_price": round((wins - expected) / n, 3) if n else None,
        # z-score of wins against what the prices implied; |z| < 2 means "can't tell yet"
        "z_score": round((wins - expected) / math.sqrt(var), 2) if var > 0 else None,
        "brier": round(sum((b.price - b.won) ** 2 for b in scored) / n, 4) if n else None,
        "realized_pnl": round(pnl, 2),
        "gross_pnl": round(pnl + fees, 2),
        "fees": round(fees, 2),
        "staked": round(staked, 2),
        "roi_pct": round(100 * pnl / staked, 1) if staked > 0 else None,
        "max_drawdown": max_drawdown([b.pnl for b in settled]),
        "calibration": calibration(scored),
        "notes": list(t.notes),
        **t.extra,
    }


def build_report(trackers: list) -> dict:
    return {"trackers": [summarize(t) for t in trackers]}


# ── Output ─────────────────────────────────────────────────────────────────

def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _money(x):
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def summary_line(report: dict) -> str:
    """One log line: the daily PERFORMANCE summary."""
    parts = []
    for s in report["trackers"]:
        if not s["bets"]:
            continue
        parts.append(f"{s['tracker']}: {s['settled']} settled, win {_pct(s['win_rate'])} vs price "
                     f"{_pct(s['avg_price'])}, P&L {_money(s['realized_pnl'])}, maxDD {_money(s['max_drawdown'])}")
    return " | ".join(parts) or "no bets recorded yet"


def compact(report: dict) -> dict:
    """Small version for the token-gated /health detail (no calibration rows, no notes)."""
    keep = ("money", "bets", "settled", "open", "win_rate", "avg_price", "edge_vs_price", "z_score",
            "brier", "realized_pnl", "fees", "max_drawdown", "roi_pct")
    return {s["tracker"]: {k: s[k] for k in keep} for s in report["trackers"] if s["bets"]}


def to_markdown(report: dict, title: str = "Performance and calibration report", preamble: str = "") -> str:
    out = [f"# {title}", ""]
    if preamble:
        out += [preamble, ""]
    out += ["Win rate above the average price paid is the favorite-longshot edge; "
            "|z| < 2 means the sample is too small to tell.", ""]
    for s in report["trackers"]:
        out += [f"## {s['tracker']} ({s['money']} money)", ""]
        if not s["bets"]:
            out += ["No bets recorded.", ""]
            continue
        ci = s["win_rate_ci95"]
        rows = [
            ("Bets (settled / open)", f"{s['bets']} ({s['settled']} / {s['open']})"),
            ("Win rate (95% CI)", f"{_pct(s['win_rate'])}" + (f" ({_pct(ci[0])} - {_pct(ci[1])})" if ci else "")),
            ("Average price paid (implied win rate)", _pct(s["avg_price"])),
            ("Win rate minus price", _pct(s["edge_vs_price"])),
            ("z-score vs prices", "n/a" if s["z_score"] is None else f"{s['z_score']:+.2f}"),
            ("Brier score (prices as forecasts)", "n/a" if s["brier"] is None else f"{s['brier']:.4f}"),
            ("Realized P&L (net of fees)", _money(s["realized_pnl"])),
            ("Fees", _money(s["fees"])),
            ("Staked on settled bets", _money(s["staked"])),
            ("Return on stake", "n/a" if s["roi_pct"] is None else f"{s['roi_pct']:+.1f}%"),
            ("Max drawdown (realized)", _money(s["max_drawdown"])),
        ]
        out += ["| Metric | Value |", "| --- | --- |"] + [f"| {a} | {b} |" for a, b in rows] + [""]
        if s["calibration"]:
            out += ["| Price bucket | Bets | Avg price | Win rate | Win - price | 95% CI | P&L |",
                    "| --- | ---: | ---: | ---: | ---: | --- | ---: |"]
            for r in s["calibration"]:
                out.append(f"| {r['bucket']} | {r['n']} | {_pct(r['avg_price'])} | {_pct(r['win_rate'])} | "
                           f"{_pct(r['edge'])} | {_pct(r['ci_low'])} - {_pct(r['ci_high'])} | {_money(r['pnl'])} |")
            out.append("")
        out += [f"- {n}" for n in s["notes"]] + [""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only performance and calibration report")
    ap.add_argument("bets", help="bets.jsonl file")
    ap.add_argument("--name", default="bets")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = ap.parse_args(argv)
    report = build_report([load_bets(args.bets, args.name)])
    sys.stdout.write((json.dumps(report, indent=2) if args.json else to_markdown(report)) + "\n")


if __name__ == "__main__":
    main()
