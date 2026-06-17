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


class TestConsensus(unittest.TestCase):
    def setUp(self):
        # one cycle's observations across a top-10 cohort
        self.obs = [
            {"asset": "A", "overlap": 6, "tier": "green", "title": "Strong", "observed_at": "t"},
            {"asset": "B", "overlap": 3, "tier": "blue", "title": "Mod", "observed_at": "t"},
            {"asset": "C", "overlap": 2, "tier": "none", "title": "Weak", "observed_at": "t"},
            {"asset": "D", "overlap": 1, "tier": "none", "title": "Solo", "observed_at": "t"},
            {"asset": "E", "overlap": 1, "tier": "none", "title": "Solo2", "observed_at": "t"},
        ]
        self.lb = [{"rank": i + 1, "wallet": f"w{i}"} for i in range(10)]

    def test_agreement_summary(self):
        a = analytics.agreement_summary(self.obs, cohort_size=10)
        self.assertEqual(a["cohort_size"], 10)
        self.assertEqual(a["positions"], 5)
        self.assertEqual(a["ge2"], 3)        # A, B, C
        self.assertEqual(a["ge3"], 2)        # A, B
        self.assertEqual(a["ge5"], 1)        # A
        self.assertEqual(a["max_overlap"], 6)
        self.assertEqual(a["histogram"], {"1": 2, "2": 1, "3": 1, "6": 1})

    def test_payload_consensus_excludes_single_holders(self):
        p = analytics.dashboard_payload([], self.obs, self.lb, [{"top_n": 10}])
        # consensus = only positions with 2+ holders, strongest first
        self.assertEqual([s["asset"] for s in p["consensus"]], ["A", "B", "C"])
        self.assertEqual(p["agreement"]["cohort_size"], 10)
        self.assertEqual(p["consensus"][0]["overlap"], 6)


class TestCalibration(unittest.TestCase):
    def test_buckets_and_returns(self):
        watch = {
            "a": {"max_overlap": 6, "resolved": True, "won": True, "first_price": 0.4, "exit_price": 1.0},
            "b": {"max_overlap": 3, "resolved": True, "won": False, "first_price": 0.5, "exit_price": 0.0},
            "c": {"max_overlap": 2, "resolved": False},  # still open, counts as tracking only
        }
        cal = analytics.calibration(watch)
        self.assertEqual(cal["ge2"]["tracking"], 3)
        self.assertEqual(cal["ge2"]["resolved"], 2)
        self.assertEqual(cal["ge2"]["wins"], 1)
        self.assertAlmostEqual(cal["ge2"]["win_rate"], 0.5)
        self.assertAlmostEqual(cal["ge2"]["avg_return"], (1.5 + -1.0) / 2)  # +150% and -100%
        self.assertEqual(cal["ge3"]["resolved"], 2)
        self.assertEqual(cal["ge5"]["resolved"], 1)
        self.assertAlmostEqual(cal["ge5"]["win_rate"], 1.0)

    def test_empty(self):
        cal = analytics.calibration({})
        self.assertIsNone(cal["ge2"]["win_rate"])
        self.assertEqual(cal["ge2"]["resolved"], 0)


class TestSmartMoneyAndBacktest(unittest.TestCase):
    WATCH = {
        "a": {"max_overlap": 6, "tier": "green", "resolved": True, "won": True,
              "first_price": 0.4, "exit_price": 1.0, "resolved_at": "2026-06-16T00:00:00Z",
              "holders": ["w1", "w2"], "title": "A"},
        "b": {"max_overlap": 3, "tier": "blue", "resolved": True, "won": False,
              "first_price": 0.5, "exit_price": 0.0, "resolved_at": "2026-06-17T00:00:00Z",
              "holders": ["w1", "w3"], "title": "B"},
        "c": {"max_overlap": 2, "tier": "blue", "resolved": False, "holders": ["w2"], "title": "C"},
    }

    def test_trader_scores(self):
        sc = analytics.trader_scores(self.WATCH)
        # w1 held a (win) and b (loss) -> 2 resolved, 1 win, 50%
        self.assertEqual(sc["w1"]["held"], 2)
        self.assertEqual(sc["w1"]["resolved"], 2)
        self.assertEqual(sc["w1"]["wins"], 1)
        self.assertAlmostEqual(sc["w1"]["win_rate"], 0.5)
        self.assertAlmostEqual(sc["w1"]["avg_return"], (1.5 + -1.0) / 2)
        # w2 held a (win) and c (open) -> 1 resolved win, 100%
        self.assertEqual(sc["w2"]["held"], 2)
        self.assertEqual(sc["w2"]["resolved"], 1)
        self.assertAlmostEqual(sc["w2"]["win_rate"], 1.0)

    def test_backtest(self):
        bt = analytics.backtest(self.WATCH)
        self.assertEqual(bt["strategies"]["ge2"]["tracking"], 3)
        self.assertEqual(bt["strategies"]["ge2"]["resolved"], 2)
        self.assertAlmostEqual(bt["strategies"]["ge2"]["win_rate"], 0.5)
        self.assertEqual(bt["strategies"]["ge5"]["resolved"], 1)
        self.assertEqual(bt["tiers"]["green"]["resolved"], 1)
        self.assertEqual(bt["tiers"]["blue"]["resolved"], 1)
        # equity curve ordered by resolution, cumulative $1/entry return: +1.5 then -1.0
        self.assertEqual([round(p["cum"], 2) for p in bt["curve"]], [1.5, 0.5])
        self.assertEqual(bt["resolved_total"], 2)

    def test_backtest_empty(self):
        bt = analytics.backtest({})
        self.assertEqual(bt["resolved_total"], 0)
        self.assertEqual(bt["curve"], [])
        self.assertIsNone(bt["strategies"]["ge2"]["win_rate"])


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


class TestClosedPositionsPerStrategy(unittest.TestCase):
    def test_control_churn_does_not_bury_consensus_closes(self):
        trades = []
        for i in range(500):   # 500 control closes with NEWER exits
            t = trade("control", "CLOSED", realized=1)
            t["exit_at"] = f"2026-06-16T{i // 60:02d}:{i % 60:02d}:00Z"
            trades.append(t)
        for i in range(15):    # 15 consensus closes with OLDER exits
            t = trade("overlap", "CLOSED", realized=2)
            t["exit_at"] = f"2026-06-15T00:{i:02d}:00Z"
            trades.append(t)
        out = analytics.closed_positions(trades, limit=200)
        # every consensus close survives even though control's exits are all newer
        self.assertEqual(sum(1 for t in out if t["strategy"] == "overlap"), 15)
        self.assertEqual(sum(1 for t in out if t["strategy"] == "control"), 200)


if __name__ == "__main__":
    unittest.main()
