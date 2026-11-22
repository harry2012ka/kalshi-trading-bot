"""
Generate a SAMPLE performance/calibration report from SYNTHETIC (randomly simulated) bets.

This is NOT a trading record. It exists to show what report.py produces and how to read it.
Each simulated bet buys a favorite priced 60-95c and wins with probability price + bias.
The default bias is 0 (a perfectly calibrated market, i.e. NO edge), so a correct report
should show win rate ~= price and |z| < 2.

    python scripts/synthetic_report.py                     # prints markdown
    python scripts/synthetic_report.py --bias 0.02 --bets 2000
    python scripts/synthetic_report.py --out docs/SAMPLE_REPORT.md
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report  # noqa: E402

BANNER = ("> **SYNTHETIC DATA - NOT REAL TRADING RESULTS.** Every bet below was randomly simulated by "
          "`scripts/synthetic_report.py` (seed {seed}, {bets} bets, assumed bias {bias:+.3f}). "
          "It demonstrates the report format only.")


def simulate(n: int, bias: float, seed: int, stake_usd: float = 1.0, open_bets: int = 5) -> report.Tracker:
    rng = random.Random(seed)
    t = report.Tracker("Synthetic favorite bets", "simulated")
    for i in range(n + open_bets):
        price = round(rng.uniform(0.60, 0.95), 2)
        contracts = max(1, int(stake_usd // price))
        bet = report.Bet(key=f"SIM-{i}", price=price, stake=round(contracts * price, 2), ts=float(i))
        if i < n:
            bet.won = rng.random() < min(price + bias, 0.999)
            bet.pnl = round(contracts * (1 - price) if bet.won else -contracts * price, 2)
        t.bets.append(bet)
    t.notes.append("Simulated: post-only maker fills on fee-free series, so fees are 0.")
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--bets", type=int, default=500)
    ap.add_argument("--bias", type=float, default=0.0, help="true win prob minus price (0 = no edge)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", help="write markdown here instead of stdout")
    args = ap.parse_args(argv)
    rep = report.build_report([simulate(args.bets, args.bias, args.seed)])
    md = report.to_markdown(rep, title="SAMPLE report (synthetic data)",
                            preamble=BANNER.format(seed=args.seed, bets=args.bets, bias=args.bias))
    if args.out:
        with open(args.out, "w") as f:
            f.write(md + "\n")
    else:
        sys.stdout.write(md + "\n")


if __name__ == "__main__":
    main()
