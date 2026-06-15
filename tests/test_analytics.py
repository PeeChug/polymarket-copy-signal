"""Unit tests for the dashboard's pure aggregation helpers."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import analytics


def trade(strategy, status, stake=100, realized=None, unrealized=0.0, tier=None):
    return {
        "strategy": strategy, "status": status, "stake_usd": stake,
        "realized_pnl": realized, "unrealized_pnl": unrealized,
        "tier_at_entry": tier, "shares": stake / 0.5, "marked_price": 0.5,
        "entry_price": 0.5, "entry_at": "2026-06-15T00:00:00Z",
    }


class TestStrategyPerformance(unittest.TestCase):
    def setUp(self):
        self.trades = [
            trade("overlap", "CLOSED", realized=300, tier="green"),   # win
            trade("overlap", "CLOSED", realized=-100, tier="blue"),   # loss
            trade("overlap", "OPEN", unrealized=50, tier="green"),
            trade("control", "CLOSED", realized=-100),                # loss
            trade("control", "OPEN", unrealized=10),
        ]

    def test_side_by_side(self):
        perf = analytics.strategy_performance(self.trades)
        ov, ct = perf["overlap"], perf["control"]

        self.assertEqual(ov["closed_count"], 2)
        self.assertEqual(ov["open_count"], 1)
        self.assertEqual(ov["wins"], 1)
        self.assertAlmostEqual(ov["win_rate"], 0.5)
        self.assertAlmostEqual(ov["realized_pnl"], 200.0)
        self.assertAlmostEqual(ov["unrealized_pnl"], 50.0)
        self.assertAlmostEqual(ov["net_pnl"], 250.0)
        self.assertAlmostEqual(ov["roi_realized"], 200.0 / 200.0)  # staked 200 on closed

        self.assertAlmostEqual(ct["realized_pnl"], -100.0)
        self.assertEqual(ct["wins"], 0)

    def test_tier_breakdown(self):
        tiers = analytics.tier_breakdown(self.trades)
        self.assertAlmostEqual(tiers["green"]["realized_pnl"], 300.0)
        self.assertEqual(tiers["green"]["open_count"], 1)
        self.assertAlmostEqual(tiers["blue"]["realized_pnl"], -100.0)

    def test_empty_is_safe(self):
        perf = analytics.strategy_performance([])
        self.assertIsNone(perf["overlap"]["win_rate"])
        self.assertEqual(perf["overlap"]["net_pnl"], 0.0)


class TestSignals(unittest.TestCase):
    def test_latest_per_market_sorted_by_overlap(self):
        obs = [
            {"asset": "A", "overlap": 5, "observed_at": "2026-06-15T00:00:00Z"},
            {"asset": "A", "overlap": 4, "observed_at": "2026-06-15T01:00:00Z"},  # newer wins
            {"asset": "B", "overlap": 2, "observed_at": "2026-06-15T00:30:00Z"},
        ]
        out = analytics.latest_signal_per_market(obs)
        self.assertEqual([o["asset"] for o in out], ["A", "B"])   # sorted by overlap desc
        self.assertEqual(out[0]["overlap"], 4)                     # newer A kept


if __name__ == "__main__":
    unittest.main()
