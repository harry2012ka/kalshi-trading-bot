"""
Offline tests for the read-only performance report and the token-gated /health payload.
SYNTHETIC data only.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import report  # noqa: E402
from file_server import health_payload, supplied_token, token_ok  # noqa: E402


def write_bets(rows):
    path = os.path.join(tempfile.mkdtemp(), "bets.jsonl")
    with open(path, "w") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    return path


class TestReport(unittest.TestCase):
    def test_metrics_on_known_bets(self):
        path = write_bets([
            {"key": "A", "price": 0.80, "stake": 0.80, "ts": 1, "won": True, "pnl": 0.20},
            {"key": "B", "price": 0.70, "stake": 1.40, "ts": 2, "won": False, "pnl": -1.40},
            {"key": "C", "price": 0.90, "stake": 0.90, "ts": 3, "won": True, "pnl": 0.08, "fee": 0.02},
            {"key": "D", "price": 0.75, "stake": 0.75, "ts": 4, "won": None, "pnl": None},
            "not json",
            {"key": "bad price", "price": 1.5, "won": True, "pnl": 0},
        ])
        s = report.summarize(report.load_bets(path))
        self.assertEqual((s["bets"], s["settled"], s["open"], s["wins"]), (4, 3, 1, 2))
        self.assertEqual(s["realized_pnl"], -1.12)
        self.assertEqual((s["fees"], s["gross_pnl"]), (0.02, -1.1))
        self.assertEqual(s["max_drawdown"], 1.4)
        self.assertEqual(s["avg_price"], 0.8)
        self.assertAlmostEqual(s["brier"], round((0.04 + 0.49 + 0.01) / 3, 4))
        self.assertEqual([r["bucket"] for r in s["calibration"]], ["70-74c", "80-84c", "90-94c"])

    def test_drawdown_and_buckets(self):
        self.assertEqual(report.max_drawdown([1, -0.5, -1, 2, -0.2]), 1.5)
        self.assertEqual(report.bucket_label(0.60), "60-64c")
        self.assertEqual(report.bucket_label(0.999), "95-99c")

    def test_missing_file_gives_empty_report(self):
        rep = report.build_report([report.load_bets("/nonexistent/bets.jsonl")])
        self.assertEqual(rep["trackers"][0]["bets"], 0)
        self.assertIn("No bets recorded", report.to_markdown(rep))

    def test_synthetic_sample_is_labelled_and_shows_no_edge_by_default(self):
        import synthetic_report
        out = os.path.join(tempfile.mkdtemp(), "sample.md")
        synthetic_report.main(["--bets", "2000", "--out", out])
        text = open(out).read()
        self.assertIn("SYNTHETIC DATA - NOT REAL TRADING RESULTS", text)
        s = report.summarize(synthetic_report.simulate(2000, 0.0, 7))
        self.assertLess(abs(s["z_score"]), 3, "a calibrated market should show no significant edge")


class TestHealthGate(unittest.TestCase):
    STATUS = {"healthy": True, "seconds_since_last_good_scan": 3, "cash": 12.34, "daily_pnl": -0.5}

    def test_public_payload_hides_money(self):
        self.assertEqual(health_payload(self.STATUS, False), {"healthy": True, "seconds_since_last_good_scan": 3})
        self.assertEqual(health_payload(self.STATUS, True), self.STATUS)

    def test_token_check(self):
        self.assertFalse(token_ok("x", expected=""), "no token configured -> no detail")
        self.assertFalse(token_ok("", expected="secret"))
        self.assertFalse(token_ok("wrong", expected="secret"))
        self.assertTrue(token_ok("secret", expected="secret"))
        self.assertEqual(supplied_token({"token": "a"}, {}), "a")
        self.assertEqual(supplied_token({}, {"Authorization": "Bearer b"}), "b")


if __name__ == "__main__":
    unittest.main()
