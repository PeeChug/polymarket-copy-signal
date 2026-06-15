"""
Integration tests for the cycle engine using a scripted fake client and the
in-memory store. These lock in the three honesty rules and the trade lifecycle:

  * forward-only entry price (rule #1)
  * log EVERY cohort position, including sub-tier ones (rule #2)
  * the #1-copy control benchmark runs alongside overlap (rule #3)
  * mark-to-market, close on resolution, close on abandonment, re-entry
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import MemoryStore
from poller.engine import run_cycle


# --- fakes ----------------------------------------------------------------- #
def L(rank, wallet):
    return types.SimpleNamespace(rank=rank, wallet=wallet, username=wallet.upper(),
                                 pnl=1000.0 * (6 - rank), volume=9999.0)


def P(asset, cond, outcome="Yes", idx=0, cur=0.5, title="Market?"):
    return types.SimpleNamespace(asset=asset, condition_id=cond, outcome=outcome,
                                 outcome_index=idx, size=100.0, avg_price=0.3,
                                 cur_price=cur, current_value=50.0, redeemable=False,
                                 title=title, slug="slug", end_date=None)


def M(cond, closed=False, active=True, liquidity=50000.0, resolved=None):
    m = types.SimpleNamespace(condition_id=cond, closed=closed, active=active,
                              liquidity=liquidity)
    m.resolved_price_for = lambda idx, _r=resolved: _r
    return m


class FakeClient:
    """Scripted, deterministic. Set the public attrs before each run_cycle()."""
    def __init__(self):
        self.lb = []
        self.positions_by_wallet = {}
        self.markets = {}
        self.marks = {}

    def leaderboard(self, window="MONTH", limit=5, order="PNL"):
        return self.lb[:limit]

    def positions(self, wallet, size_threshold=1.0, only_open=True):
        return list(self.positions_by_wallet.get(wallet, []))

    def market(self, condition_id):
        return self.markets.get(condition_id)

    def mark_price(self, token_id, source="midpoint", market=None, outcome_index=0, fallback=None):
        return self.marks.get(token_id, fallback)


CFG = {
    "top_n": 3, "leaderboard_window": "MONTH", "size_threshold": 1,
    "tier_green_min": 3, "tier_blue_min": 2,
    "min_liquidity": 1000, "max_entry_price": 0.90, "min_tier_to_trade": "blue",
    "stake_usd": 100, "price_source": "midpoint", "control_respects_guardrails": True,
    "source": "test",
}


class TestEngineLifecycle(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.insert_config(dict(CFG))   # so load_config uses our test config
        self.c = FakeClient()
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3")]
        self.c.markets = {"mA": M("mA"), "mB": M("mB")}

    def run_quiet(self):
        run_cycle(self.store, self.c, log=lambda *a, **k: None)

    def test_log_everything_and_open_control(self):
        # A: held by all 3 (overlap 3 -> green). B: held by 1 (overlap 1 -> none).
        self.c.positions_by_wallet = {
            "w1": [P("A", "mA", title="Alpha"), P("B", "mB", title="Beta")],
            "w2": [P("A", "mA", title="Alpha")],
            "w3": [P("A", "mA", title="Alpha")],
        }
        self.c.marks = {"A": 0.40, "B": 0.20}
        self.run_quiet()

        obs = self.store.latest_observations()
        assets = {o["asset"]: o for o in obs}
        # rule #2: BOTH A and B are logged, even though B is sub-tier
        self.assertIn("A", assets)
        self.assertIn("B", assets)
        self.assertEqual(assets["A"]["tier"], "green")
        self.assertEqual(assets["B"]["tier"], "none")

        trades = self.store.all_trades()
        overlap = [t for t in trades if t["strategy"] == "overlap"]
        control = [t for t in trades if t["strategy"] == "control"]
        # only A qualifies for an overlap trade (B is tier 'none')
        self.assertEqual([t["asset"] for t in overlap], ["A"])
        # rule #3: control copies #1 (w1 holds A and B); both pass tradeability
        self.assertEqual(sorted(t["asset"] for t in control), ["A", "B"])
        # entry locked at the price available now
        self.assertAlmostEqual(overlap[0]["entry_price"], 0.40)
        self.assertAlmostEqual(overlap[0]["shares"], 250.0)  # 100 / 0.40

    def test_forward_only_entry_then_mark(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks = {"A": 0.40}
        self.run_quiet()
        entry = [t for t in self.store.all_trades() if t["strategy"] == "overlap"][0]["entry_price"]

        # next cycle: price moved up; entry must NOT change, unrealized must update
        self.c.marks = {"A": 0.60}
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertAlmostEqual(t["entry_price"], entry)          # locked forever
        self.assertAlmostEqual(t["entry_price"], 0.40)
        self.assertAlmostEqual(t["marked_price"], 0.60)
        self.assertAlmostEqual(t["unrealized_pnl"], 250.0 * (0.60 - 0.40))  # +50

    def test_close_on_cohort_abandonment_then_reentry(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks = {"A": 0.40}
        self.run_quiet()

        # cohort fully abandons A; mark drifted to 0.55
        self.c.positions_by_wallet = {"w1": [], "w2": [], "w3": []}
        self.c.marks = {"A": 0.55}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["status"], "CLOSED")
        self.assertEqual(overlap[0]["close_reason"], "cohort_abandoned")
        self.assertAlmostEqual(overlap[0]["exit_price"], 0.55)
        self.assertAlmostEqual(overlap[0]["realized_pnl"], 250.0 * (0.55 - 0.40))

        # re-entry: cohort piles back in -> a NEW open trade is allowed
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks = {"A": 0.50}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(overlap), 2)
        self.assertEqual(sum(1 for t in overlap if t["status"] == "OPEN"), 1)

    def test_close_on_resolution_win(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks = {"A": 0.40}
        self.run_quiet()

        # market resolves YES (payout 1.0 for outcome_index 0)
        self.c.markets["mA"] = M("mA", closed=True, resolved=1.0)
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertEqual(t["status"], "CLOSED")
        self.assertEqual(t["close_reason"], "resolved")
        self.assertTrue(t["resolved_won"])
        self.assertAlmostEqual(t["exit_price"], 1.0)
        self.assertAlmostEqual(t["realized_pnl"], 250.0 * (1.0 - 0.40))  # +150

    def test_single_open_per_strategy_market_outcome(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks = {"A": 0.40}
        self.run_quiet()
        self.run_quiet()  # second cycle, same holdings -> must NOT open a duplicate
        overlap_open = [t for t in self.store.all_trades()
                        if t["strategy"] == "overlap" and t["status"] == "OPEN"]
        self.assertEqual(len(overlap_open), 1)


if __name__ == "__main__":
    unittest.main()
