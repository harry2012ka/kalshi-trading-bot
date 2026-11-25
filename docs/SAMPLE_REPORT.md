# SAMPLE report (synthetic data)

> **SYNTHETIC DATA - NOT REAL TRADING RESULTS.** Every bet below was randomly simulated by `scripts/synthetic_report.py` (seed 7, 500 bets, assumed bias +0.000). It demonstrates the report format only.

Win rate above the average price paid is the favorite-longshot edge; |z| < 2 means the sample is too small to tell.

## Synthetic favorite bets (simulated money)

| Metric | Value |
| --- | --- |
| Bets (settled / open) | 505 (500 / 5) |
| Win rate (95% CI) | 77.0% (73.1% - 80.5%) |
| Average price paid (implied win rate) | 76.8% |
| Win rate minus price | 0.2% |
| z-score vs prices | +0.13 |
| Brier score (prices as forecasts) | 0.1737 |
| Realized P&L (net of fees) | $1.23 |
| Fees | $0.00 |
| Staked on settled bets | $383.77 |
| Return on stake | +0.3% |
| Max drawdown (realized) | $10.33 |

| Price bucket | Bets | Avg price | Win rate | Win - price | 95% CI | P&L |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| 60-64c | 76 | 62.1% | 68.4% | 6.3% | 57.3% - 77.8% | $4.78 |
| 65-69c | 81 | 67.1% | 72.8% | 5.7% | 62.3% - 81.3% | $4.62 |
| 70-74c | 69 | 72.2% | 71.0% | -1.2% | 59.4% - 80.4% | -$0.84 |
| 75-79c | 72 | 77.1% | 77.8% | 0.7% | 66.9% - 85.8% | $0.49 |
| 80-84c | 60 | 82.0% | 78.3% | -3.7% | 66.4% - 86.9% | -$2.19 |
| 85-89c | 70 | 87.2% | 82.9% | -4.4% | 72.4% - 89.9% | -$3.07 |
| 90-94c | 69 | 92.3% | 88.4% | -3.9% | 78.8% - 94.0% | -$2.71 |
| 95-99c | 3 | 95.0% | 100.0% | 5.0% | 43.8% - 100.0% | $0.15 |

- Simulated: post-only maker fills on fee-free series, so fees are 0.

