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

    def test_jsonl_rotation_bounds_growth(self):
        """Append-only logs are trimmed to the most-recent N lines once big."""
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            s = FileStore(d)
            path = os.path.join(d, "rot.jsonl")
            s._append(path, [{"i": i} for i in range(100)])
            # force rotation (size_gate=0) keeping the most recent 10
            s._rotate(path, max_lines=10, size_gate=0)
            with open(path) as fh:
                lines = [ln for ln in fh if ln.strip()]
            self.assertEqual(len(lines), 10)
            self.assertEqual(_json.loads(lines[0])["i"], 90)
            self.assertEqual(_json.loads(lines[-1])["i"], 99)
            # below the size gate, nothing is trimmed
            p2 = os.path.join(d, "rot2.jsonl")
            s._append(p2, [{"i": i} for i in range(5)], max_lines=2)
            with open(p2) as fh:
                self.assertEqual(len([ln for ln in fh if ln.strip()]), 5)


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
