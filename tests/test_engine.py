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
                                 pnl=1000.0 * (6 - rank), volume=9999.0,
                                 profile_image="", x_username="", verified=False)


def P(asset, cond, outcome="Yes", idx=0, cur=0.5, title="Market?"):
    return types.SimpleNamespace(asset=asset, condition_id=cond, outcome=outcome,
                                 outcome_index=idx, size=100.0, avg_price=0.3,
                                 cur_price=cur, current_value=50.0, redeemable=False,
                                 title=title, slug="slug", end_date=None,
                                 cash_pnl=0.0, percent_pnl=0.0)


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
        self.markets_map = {}
        self.marks_map = {}

    def leaderboard(self, window="MONTH", limit=5, order="PNL"):
        return self.lb[:limit]

    def positions(self, wallet, size_threshold=1.0, only_open=True):
        return list(self.positions_by_wallet.get(wallet, []))

    def positions_many(self, wallets, size_threshold=1.0, only_open=True):
        # mirror the real client: return (positions, failed); `fail_wallets`
        # lets a test simulate a transient per-wallet fetch error.
        fail = getattr(self, "fail_wallets", set())
        out = {w: self.positions(w) for w in wallets if w not in fail}
        return out, set(w for w in wallets if w in fail)

    def _src_map(self, source):
        return {"buy": getattr(self, "ask_map", None),
                "sell": getattr(self, "bid_map", None)}.get(source) or self.marks_map

    def market(self, condition_id):
        return self.markets_map.get(condition_id)

    def markets(self, condition_ids, chunk=40):
        return {c: self.markets_map[c] for c in condition_ids if c in self.markets_map}

    def marks(self, token_ids, source="midpoint"):
        m = self._src_map(source)
        return {t: m[t] for t in token_ids if t in m}

    def mark_price(self, token_id, source="midpoint", market=None, outcome_index=0, fallback=None):
        return self.marks_map.get(token_id, fallback)


CFG = {
    "top_n": 3, "leaderboard_window": "MONTH", "size_threshold": 1,
    "tier_green_min": 3, "tier_blue_min": 2,
    "min_liquidity": 1000, "max_entry_price": 0.90, "min_tier_to_trade": "blue",
    "stake_usd": 100, "price_source": "midpoint", "control_respects_guardrails": True,
    "min_holder_value": 0, "min_holder_win_ratio": 0,   # cohort-quality filter off unless a test sets it
    "source": "test",
}


class TestEngineLifecycle(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.insert_config(dict(CFG))   # so load_config uses our test config
        self.c = FakeClient()
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3")]
        self.c.markets_map = {"mA": M("mA"), "mB": M("mB")}

    def run_quiet(self):
        run_cycle(self.store, self.c, log=lambda *a, **k: None)

    def test_log_everything_and_open_control(self):
        # A: held by all 3 (overlap 3 -> green). B: held by 1 (overlap 1 -> none).
        self.c.positions_by_wallet = {
            "w1": [P("A", "mA", title="Alpha"), P("B", "mB", title="Beta")],
            "w2": [P("A", "mA", title="Alpha")],
            "w3": [P("A", "mA", title="Alpha")],
        }
        self.c.marks_map = {"A": 0.40, "B": 0.20}
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
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        entry = [t for t in self.store.all_trades() if t["strategy"] == "overlap"][0]["entry_price"]

        # next cycle: price moved up; entry must NOT change, unrealized must update
        self.c.marks_map = {"A": 0.60}
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertAlmostEqual(t["entry_price"], entry)          # locked forever
        self.assertAlmostEqual(t["entry_price"], 0.40)
        self.assertAlmostEqual(t["marked_price"], 0.60)
        self.assertAlmostEqual(t["unrealized_pnl"], 250.0 * (0.60 - 0.40))  # +50

    def test_close_on_cohort_abandonment_then_reentry(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()

        # cohort fully abandons A; mark drifted to 0.55
        self.c.positions_by_wallet = {"w1": [], "w2": [], "w3": []}
        self.c.marks_map = {"A": 0.55}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["status"], "CLOSED")
        self.assertEqual(overlap[0]["close_reason"], "cohort_abandoned")
        self.assertAlmostEqual(overlap[0]["exit_price"], 0.55)
        self.assertAlmostEqual(overlap[0]["realized_pnl"], 250.0 * (0.55 - 0.40))

        # re-entry: cohort piles back in -> a NEW open trade is allowed
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.50}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(overlap), 2)
        self.assertEqual(sum(1 for t in overlap if t["status"] == "OPEN"), 1)

    def test_close_on_resolution_win(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()

        # market resolves YES (payout 1.0 for outcome_index 0)
        self.c.markets_map["mA"] = M("mA", closed=True, resolved=1.0)
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertEqual(t["status"], "CLOSED")
        self.assertEqual(t["close_reason"], "resolved")
        self.assertTrue(t["resolved_won"])
        self.assertAlmostEqual(t["exit_price"], 1.0)
        self.assertAlmostEqual(t["realized_pnl"], 250.0 * (1.0 - 0.40))  # +150

    def test_single_open_per_strategy_market_outcome(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        self.run_quiet()  # second cycle, same holdings -> must NOT open a duplicate
        overlap_open = [t for t in self.store.all_trades()
                        if t["strategy"] == "overlap" and t["status"] == "OPEN"]
        self.assertEqual(len(overlap_open), 1)

    def test_contested_market_takes_one_side(self):
        # market mC split 2 (Yes) vs 2 (No): contested -> trade only the stronger side,
        # ties broken by holders' 30d P&L ({w1,w2}=9000 > {w3,w4}=5000 -> Yes).
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "top_n": 4, "contested_policy": "dominant"})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3"), L(4, "w4")]
        self.c.markets_map = {"mC": M("mC")}
        self.c.positions_by_wallet = {
            "w1": [P("Cy", "mC", "Yes", 0)], "w2": [P("Cy", "mC", "Yes", 0)],
            "w3": [P("Cn", "mC", "No", 1)],  "w4": [P("Cn", "mC", "No", 1)],
        }
        self.c.marks_map = {"Cy": 0.30, "Cn": 0.70}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(overlap), 1)          # only ONE side, not both (no wash)
        self.assertEqual(overlap[0]["asset"], "Cy")  # higher holder P&L wins the tie

    def test_stop_loss_closes_loser(self):
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "stop_loss_pct": 0.5})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3")]
        self.c.markets_map = {"mA": M("mA")}
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()                 # open A @ 0.40
        self.c.marks_map = {"A": 0.18}   # -55% < -50% stop
        self.run_quiet()
        a = [t for t in self.store.all_trades()
             if t["strategy"] == "overlap" and t["asset"] == "A"][0]
        self.assertEqual(a["status"], "CLOSED")
        self.assertEqual(a["close_reason"], "stop_loss")

    def test_partial_fetch_does_not_falsely_abandon(self):
        # open A, held by the whole cohort
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        # next cycle: every wallet that holds A FAILS to fetch (transient API error).
        # A *looks* abandoned, but a failed fetch must NOT close the trade.
        self.c.fail_wallets = {"w1", "w2", "w3"}
        self.c.marks_map = {"A": 0.55}
        self.run_quiet()
        a = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["status"], "OPEN")          # survived the degraded cycle
        self.assertIsNone(a[0].get("close_reason"))

    def test_resolution_without_payout_marks_won_unknown(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        # market closes but Gamma returns no payout vector -> outcome UNKNOWN
        self.c.markets_map["mA"] = M("mA", closed=True, resolved=None)
        self.c.marks_map = {"A": 0.62}
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertEqual(t["status"], "CLOSED")
        self.assertEqual(t["close_reason"], "resolved")
        self.assertIsNone(t["resolved_won"])              # NOT guessed from price>=0.5

    def test_resolution_loss_sets_won_false(self):
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        self.c.markets_map["mA"] = M("mA", closed=True, resolved=0.0)   # our outcome lost
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertEqual(t["close_reason"], "resolved")
        self.assertFalse(t["resolved_won"])
        self.assertAlmostEqual(t["exit_price"], 0.0)

    def test_cohort_quality_filter_excludes_low_value(self):
        # w1 holds A with only $5 on the table (below min_holder_value); w2,w3 hold
        # plenty. w1 must NOT count toward overlap -> A drops from 3 holders to 2,
        # and w1 is flagged ineligible on the dashboard snapshot.
        def Pv(asset, cond, value):
            p = P(asset, cond); p.current_value = value; p.cash_pnl = 1.0; return p
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "min_holder_value": 1000, "min_holder_win_ratio": 0.0})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3")]
        self.c.markets_map = {"mA": M("mA")}
        self.c.positions_by_wallet = {
            "w1": [Pv("A", "mA", 5.0)],          # too little on the table -> ineligible
            "w2": [Pv("A", "mA", 50000.0)],
            "w3": [Pv("A", "mA", 50000.0)],
        }
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()
        obs = {o["asset"]: o for o in self.store.latest_observations()}
        self.assertEqual(obs["A"]["overlap"], 2)             # w1's $5 holding didn't count
        traders = {t["wallet"]: t for t in self.store.latest_traders()}
        self.assertFalse(traders["w1"]["eligible"])
        self.assertTrue(traders["w2"]["eligible"])

    def test_signal_decay_closes_thinned_position(self):
        # open at overlap 4; cohort thins to 1 holder (< 0.5*4) -> signal_decayed close
        # even though one holder remains (so it is NOT a full abandonment).
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "top_n": 4, "exit_overlap_frac": 0.5,
                                  "tier_blue_min": 2, "tier_green_min": 3})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3"), L(4, "w4")]
        self.c.markets_map = {"mA": M("mA")}
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3", "w4")}
        self.c.marks_map = {"A": 0.40}
        self.run_quiet()                                   # entry overlap 4
        self.c.positions_by_wallet = {"w1": [P("A", "mA")], "w2": [], "w3": [], "w4": []}
        self.c.marks_map = {"A": 0.45}
        self.run_quiet()                                   # overlap 1 < 0.5*4 -> decay
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertEqual(t["status"], "CLOSED")
        self.assertEqual(t["close_reason"], "signal_decayed")

    def test_realistic_fills_enter_at_ask_mark_at_bid(self):
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "price_source": "realistic"})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3")]
        self.c.markets_map = {"mA": M("mA")}
        self.c.positions_by_wallet = {w: [P("A", "mA")] for w in ("w1", "w2", "w3")}
        self.c.ask_map = {"A": 0.42}   # what you pay to enter
        self.c.bid_map = {"A": 0.38}   # what you'd get to exit
        self.run_quiet()
        t = [x for x in self.store.all_trades() if x["strategy"] == "overlap"][0]
        self.assertAlmostEqual(t["entry_price"], 0.42)   # entered at the ask
        self.assertAlmostEqual(t["marked_price"], 0.38)  # marked to the bid
        self.assertLess(t["unrealized_pnl"], 0)          # the spread is an immediate paper loss

    def test_contested_both_opens_both_sides(self):
        self.store = MemoryStore()
        self.store.insert_config({**CFG, "top_n": 4, "contested_policy": "both"})
        self.c.lb = [L(1, "w1"), L(2, "w2"), L(3, "w3"), L(4, "w4")]
        self.c.markets_map = {"mC": M("mC")}
        self.c.positions_by_wallet = {
            "w1": [P("Cy", "mC", "Yes", 0)], "w2": [P("Cy", "mC", "Yes", 0)],
            "w3": [P("Cn", "mC", "No", 1)],  "w4": [P("Cn", "mC", "No", 1)],
        }
        self.c.marks_map = {"Cy": 0.45, "Cn": 0.55}
        self.run_quiet()
        overlap = [t for t in self.store.all_trades() if t["strategy"] == "overlap"]
        self.assertEqual(sorted(t["asset"] for t in overlap), ["Cn", "Cy"])  # BOTH sides


if __name__ == "__main__":
    unittest.main()
