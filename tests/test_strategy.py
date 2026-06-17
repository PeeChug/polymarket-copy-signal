"""Unit tests for the pure strategy + P&L logic (no network, no DB)."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from poller import strategy


def pos(wallet, asset, cond="c", outcome="Yes", idx=0, cur=0.5):
    """Minimal duck-typed position for compute_overlaps."""
    p = types.SimpleNamespace(
        wallet=wallet, asset=asset, condition_id=cond, outcome=outcome,
        outcome_index=idx, title="t", slug="s", end_date=None, cur_price=cur,
    )
    p._username = wallet.upper()
    p._rank = 1
    return p


class TestTiering(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(top_n=5, tier_green_min=5, tier_blue_min=3, min_tier_to_trade="blue")

    def test_tier_for(self):
        self.assertEqual(self.cfg.tier_for(5), "green")
        self.assertEqual(self.cfg.tier_for(6), "green")
        self.assertEqual(self.cfg.tier_for(4), "blue")
        self.assertEqual(self.cfg.tier_for(3), "blue")
        self.assertEqual(self.cfg.tier_for(2), "none")
        self.assertEqual(self.cfg.tier_for(0), "none")

    def test_min_tier_blue_admits_blue_and_green(self):
        self.assertTrue(self.cfg.tier_meets_minimum("green"))
        self.assertTrue(self.cfg.tier_meets_minimum("blue"))
        self.assertFalse(self.cfg.tier_meets_minimum("none"))

    def test_min_tier_green_admits_only_green(self):
        cfg = Config(min_tier_to_trade="green")
        self.assertTrue(cfg.tier_meets_minimum("green"))
        self.assertFalse(cfg.tier_meets_minimum("blue"))


class TestOverlap(unittest.TestCase):
    def test_counts_and_dedupes(self):
        cohort = {
            "w1": [pos("w1", "A"), pos("w1", "B"), pos("w1", "A")],  # dup A ignored
            "w2": [pos("w2", "A"), pos("w2", "B")],
            "w3": [pos("w3", "A"), pos("w3", "C")],
        }
        ov = strategy.compute_overlaps(cohort)
        self.assertEqual(ov["A"].overlap, 3)              # all three
        self.assertEqual(ov["B"].overlap, 2)
        self.assertEqual(ov["C"].overlap, 1)
        self.assertEqual(set(ov["A"].wallets), {"w1", "w2", "w3"})  # w1 counted once

    def test_participants_vs_overlap(self):
        # market m1: 3 wallets on Yes, 1 on No -> 4 participants; Yes overlap 3 => "3/4"
        cohort = {
            "w1": [pos("w1", "Y", cond="m1", outcome="Yes")],
            "w2": [pos("w2", "Y", cond="m1", outcome="Yes")],
            "w3": [pos("w3", "Y", cond="m1", outcome="Yes")],
            "w4": [pos("w4", "N", cond="m1", outcome="No", idx=1)],
        }
        ov = strategy.compute_overlaps(cohort)
        self.assertEqual(ov["Y"].overlap, 3)
        self.assertEqual(ov["Y"].participants, 4)   # 4 wallets hold a position in m1
        self.assertEqual(ov["N"].overlap, 1)
        self.assertEqual(ov["N"].participants, 4)

    def test_fallback_price_is_median_of_positive(self):
        cohort = {"w1": [pos("w1", "A", cur=0.2)], "w2": [pos("w2", "A", cur=0.0)],
                  "w3": [pos("w3", "A", cur=0.4)]}
        ov = strategy.compute_overlaps(cohort)["A"]
        self.assertEqual(ov.fallback_price, 0.4)  # median of [0.2, 0.4] (0.0 dropped)


class TestPnLMath(unittest.TestCase):
    def test_shares_and_pnl(self):
        shares = strategy.shares_for(100, 0.25)
        self.assertAlmostEqual(shares, 400.0)
        # mark up to 0.50 -> +100 unrealized
        self.assertAlmostEqual(strategy.unrealized_pnl(shares, 0.25, 0.50), 100.0)
        # resolve win (1.0) -> +300 realized
        self.assertAlmostEqual(strategy.realized_pnl(shares, 0.25, 1.0), 300.0)
        # resolve loss (0.0) -> -100 realized (lose the whole stake)
        self.assertAlmostEqual(strategy.realized_pnl(shares, 0.25, 0.0), -100.0)
        self.assertAlmostEqual(strategy.roi(300.0, 100.0), 3.0)

    def test_zero_entry_is_safe(self):
        self.assertEqual(strategy.shares_for(100, 0.0), 0.0)


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(min_liquidity=1000, max_entry_price=0.90, min_tier_to_trade="blue",
                          control_respects_guardrails=True)

    def g(self, **kw):
        base = dict(tier="blue", price=0.5, liquidity=5000, market_closed=False,
                    cfg=self.cfg, strategy="overlap")
        base.update(kw)
        return strategy.passes_guardrails(**base)

    def test_overlap_happy_path(self):
        self.assertTrue(self.g().ok)

    def test_tier_below_minimum_blocked(self):
        self.assertFalse(self.g(tier="none").ok)

    def test_low_liquidity_blocked(self):
        r = self.g(liquidity=10)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "low_liquidity")

    def test_price_too_high_blocked(self):
        self.assertFalse(self.g(price=0.95).ok)

    def test_price_too_low_blocked(self):
        self.cfg.min_entry_price = 0.05
        r = self.g(price=0.01)                 # deep longshot
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "price_too_low")
        self.assertTrue(self.g(price=0.06).ok)

    def test_closed_market_blocked(self):
        self.assertFalse(self.g(market_closed=True).ok)

    def test_missing_price_blocked(self):
        self.assertFalse(self.g(price=None).ok)

    def test_control_ignores_tier_but_keeps_tradeability(self):
        # control: tier 'none' is fine, but low liquidity still blocks
        self.assertTrue(self.g(strategy="control", tier="none").ok)
        self.assertFalse(self.g(strategy="control", tier="none", liquidity=10).ok)

    def test_control_can_ignore_all_guardrails(self):
        cfg = Config(control_respects_guardrails=False, min_liquidity=1000, max_entry_price=0.9)
        r = strategy.passes_guardrails(tier="none", price=0.99, liquidity=1,
                                       market_closed=False, cfg=cfg, strategy="control")
        self.assertTrue(r.ok)

    def test_resolves_too_soon_blocked(self):
        from datetime import datetime, timezone, timedelta
        soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        r = self.g(end_date=soon)            # default min_resolve_hours = 24
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "resolves_too_soon")

    def test_far_resolution_ok(self):
        from datetime import datetime, timezone, timedelta
        far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        self.assertTrue(self.g(end_date=far).ok)
        self.assertTrue(self.g(end_date=None).ok)   # unknown end date => don't block

    def test_resolve_filter_off_when_zero(self):
        from datetime import datetime, timezone, timedelta
        self.cfg.min_resolve_hours = 0
        soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertTrue(self.g(end_date=soon).ok)


class TestPriceExit(unittest.TestCase):
    """The fast price/time exits (overlap-only) mirrored in the Worker."""
    def setUp(self):
        # dataclass defaults: stop 0.25, floor 0.05, tp 0(off), trail 0.15,
        # arm 0.20, time_stop 30 min, fast_exit_slippage 0.02
        self.cfg = Config()

    def pe(self, **kw):
        base = dict(entry=0.50, mark=0.50, peak=0.50, end_date=None,
                    cfg=self.cfg, strategy="overlap")
        base.update(kw)
        return strategy.price_exit(**base)

    def test_no_exit_when_flat(self):
        self.assertEqual(self.pe(), (None, None))

    def test_stop_loss_takes_a_panic_haircut(self):
        # down 60% (> default 25% stop). exit = mark*(1 - fast_exit_slippage)
        reason, xp = self.pe(entry=0.50, mark=0.20)
        self.assertEqual(reason, "stop_loss")
        self.assertAlmostEqual(xp, 0.20 * (1 - 0.02))

    def test_price_floor_exits_a_near_dead_outcome(self):
        self.cfg.stop_loss_pct = 0           # isolate the floor
        reason, _ = self.pe(entry=0.06, mark=0.04)   # below the 0.05 floor
        self.assertEqual(reason, "stop_loss")

    def test_take_profit_when_enabled_no_haircut(self):
        self.cfg.take_profit_pct = 0.30
        reason, xp = self.pe(entry=0.50, mark=0.66, peak=0.66)   # +32%
        self.assertEqual(reason, "take_profit")
        self.assertAlmostEqual(xp, 0.66)     # planned sell fills at the touch bid

    def test_take_profit_off_by_default(self):
        reason, _ = self.pe(entry=0.50, mark=0.80, peak=0.80)    # +60% but tp=0
        self.assertNotEqual(reason, "take_profit")

    def test_trailing_arms_then_triggers(self):
        # peak +30% (>= arm 0.20) -> armed; give back 15% from the 0.65 peak
        reason, xp = self.pe(entry=0.50, mark=0.55, peak=0.65)   # 0.55 <= 0.65*0.85
        self.assertEqual(reason, "trailing_stop")
        self.assertAlmostEqual(xp, 0.55 * (1 - 0.02))            # panic haircut applies

    def test_trailing_not_armed_below_arm(self):
        # peak only +10% (< arm 0.20) -> never trails, even sitting at a small give-back
        reason, _ = self.pe(entry=0.50, mark=0.50, peak=0.55)
        self.assertIsNone(reason)

    def test_time_stop_before_resolution(self):
        from datetime import datetime, timezone, timedelta
        soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()  # < 30 min
        reason, xp = self.pe(entry=0.50, mark=0.55, peak=0.55, end_date=soon)
        self.assertEqual(reason, "time_stop")
        self.assertAlmostEqual(xp, 0.55)     # scheduled exit, no panic haircut

    def test_time_stop_precedes_stop_loss(self):
        from datetime import datetime, timezone, timedelta
        soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        # both a -60% stop AND inside the time window -> time_stop is checked first
        reason, _ = self.pe(entry=0.50, mark=0.20, peak=0.50, end_date=soon)
        self.assertEqual(reason, "time_stop")

    def test_control_never_price_exits(self):
        # control stays naive: even a -60% move yields no price exit
        self.assertEqual(self.pe(strategy="control", entry=0.50, mark=0.20), (None, None))

    def test_zero_entry_is_safe(self):
        self.assertEqual(self.pe(entry=0.0, mark=0.0), (None, None))


if __name__ == "__main__":
    unittest.main()
