"""Tests for the file-backed store (persistence + reload) and the site payload."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import analytics
from core.store import FileStore


class TestFileStore(unittest.TestCase):
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            s = FileStore(d)
            s.insert_config({"top_n": 5, "source": "test"})
            cyc = s.create_cycle({"top_n": 5, "window": "MONTH", "status": "ok"})
            s.insert_observations([{"cycle_id": cyc["id"], "asset": "A", "overlap": 3,
                                    "tier": "blue", "holder_wallets": ["w1", "w2", "w3"]}])
            t = s.insert_trade({"strategy": "overlap", "condition_id": "c", "asset": "A",
                                "outcome_index": 0, "status": "OPEN", "entry_price": 0.4,
                                "stake_usd": 100, "shares": 250})
            self.assertIsNotNone(t)
            # duplicate OPEN trade rejected
            self.assertIsNone(s.insert_trade({"strategy": "overlap", "condition_id": "c",
                                              "asset": "A", "outcome_index": 0, "status": "OPEN",
                                              "entry_price": 0.4, "stake_usd": 100, "shares": 250}))

            # reload from disk -> state survived
            s2 = FileStore(d)
            self.assertEqual(len(s2.all_trades()), 1)
            self.assertEqual(s2.latest_config()["top_n"], 5)
            self.assertEqual(s2.open_trades()[0]["asset"], "A")
            self.assertEqual(s2.latest_observations()[0]["overlap"], 3)

            # observations.jsonl is the append-only empirical record (rule #2)
            with open(os.path.join(d, "observations.jsonl")) as fh:
                self.assertEqual(len([ln for ln in fh if ln.strip()]), 1)

    def test_update_trade_persists(self):
        with tempfile.TemporaryDirectory() as d:
            s = FileStore(d)
            t = s.insert_trade({"strategy": "overlap", "condition_id": "c", "asset": "A",
                                "outcome_index": 0, "status": "OPEN", "entry_price": 0.4,
                                "stake_usd": 100, "shares": 250})
            s.update_trade(t["id"], {"status": "CLOSED", "realized_pnl": 150})
            self.assertEqual(FileStore(d).all_trades()[0]["status"], "CLOSED")


class TestDashboardPayload(unittest.TestCase):
    def test_shape(self):
        trades = [{"strategy": "overlap", "status": "CLOSED", "stake_usd": 100,
                   "realized_pnl": 50, "tier_at_entry": "green"}]
        obs = [{"asset": "A", "overlap": 4, "observed_at": "2026-06-15T00:00:00Z"}]
        payload = analytics.dashboard_payload(trades, obs, [], [{"top_n": 5}],
                                              meta={"generated_at": "now", "last_cycle": {"status": "ok"}})
        for key in ("performance", "tiers", "open_positions", "signals", "config", "leaderboard", "counts"):
            self.assertIn(key, payload)
        self.assertEqual(payload["counts"]["closed"], 1)
        self.assertEqual(payload["config"]["top_n"], 5)
        self.assertEqual(payload["performance"]["overlap"]["realized_pnl"], 50)


if __name__ == "__main__":
    unittest.main()
