"""PostgrestStore (Supabase) tracker methods, against an in-memory fake PostgREST.

We can't reach a real Supabase from CI, so we stub the HTTP session and verify
the kv_store-backed trackers + last_cycle round-trip correctly (the same data
the dashboard payload is built from). The relational methods reuse the same
_req/_select plumbing exercised here.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import PostgrestStore


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Minimal PostgREST emulator for kv_store + cycles."""
    def __init__(self):
        self.kv = {}            # key -> value (jsonb)
        self.cycles = []        # list of cycle dicts
        self.trades = []        # list of paper_trade dicts
        self.observations = []  # list of observation dicts
        self.leaderboard = []   # list of leaderboard_snapshot dicts
        self.headers = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        table = url.rstrip("/").split("/")[-1]
        body = json.loads(data) if data else None
        params = params or {}
        if table == "paper_trades" and method == "GET":
            # emulate PostgREST's hard 1000-row response cap + offset paging
            rows = sorted(self.trades, key=lambda t: t["id"])
            offset = int(params.get("offset", 0))
            limit = min(int(params.get("limit", 1000)), 1000)
            return _Resp(200, rows[offset:offset + limit])
        if table == "kv_store":
            if method == "POST":                      # upsert on primary key
                self.kv[body["key"]] = body["value"]
                return _Resp(201, None)
            if method == "GET":
                key = params.get("key", "")
                k = key[3:] if key.startswith("eq.") else None
                return _Resp(200, [{"value": self.kv[k]}] if k in self.kv else [])
        if table == "cycles" and method == "GET":
            rows = sorted(self.cycles, key=lambda c: c["id"], reverse=True)
            lim = int(params.get("limit", len(rows) or 1))
            return _Resp(200, rows[:lim])
        if table in ("observations", "leaderboard_snapshots"):
            store = self.observations if table == "observations" else self.leaderboard
            tcol = "observed_at" if table == "observations" else "captured_at"
            if method == "POST":
                store.extend(body if isinstance(body, list) else [body])
                return _Resp(201, None)
            if method == "DELETE":
                cut = params.get(tcol, "")              # PostgREST op, e.g. "lt.<iso>"
                if cut.startswith("lt."):
                    c = cut[3:]
                    keep = [r for r in store if str(r.get(tcol) or "") >= c]
                    store[:] = keep
                return _Resp(204, None)
        return _Resp(200, [])


class TestPostgrestTrackers(unittest.TestCase):
    def _store(self):
        s = PostgrestStore("https://x.supabase.co", "service-key")
        s.session = FakeSession()
        return s

    def test_kv_trackers_roundtrip(self):
        s = self._store()
        # consensus watch
        self.assertEqual(s.get_consensus_watch(), {})
        s.set_consensus_watch({"a|0": {"max_overlap": 3, "resolved": False}})
        self.assertEqual(s.get_consensus_watch()["a|0"]["max_overlap"], 3)
        # history accumulates in order
        s.append_history({"ts": "t1", "ge2": 5})
        s.append_history({"ts": "t2", "ge2": 7})
        self.assertEqual([r["ge2"] for r in s.history()], [5, 7])
        # agreement / series / traders
        s.set_agreement({"ge2": 10})
        self.assertEqual(s.get_agreement()["ge2"], 10)
        s.set_trader_series({"w1": [1, 2, 3]})
        self.assertEqual(s.get_trader_series()["w1"], [1, 2, 3])
        s.set_traders([{"wallet": "w1", "pnl": 100}])
        self.assertEqual(s.latest_traders()[0]["wallet"], "w1")

    def test_history_cap(self):
        s = self._store()
        for i in range(1600):
            s.append_history({"i": i})
        h = s.history(limit=5000)
        self.assertEqual(len(h), 1500)            # capped to the most recent 1500
        self.assertEqual(h[0]["i"], 100)
        self.assertEqual(h[-1]["i"], 1599)

    def test_all_trades_pages_past_the_1000_row_cap(self):
        # the control benchmark alone blows past 1000 rows; a single GET returns
        # only the oldest 1000, which silently truncated every realized stat.
        s = self._store()
        s.session.trades = [{"id": i, "strategy": "control", "status": "CLOSED"} for i in range(1, 2301)]
        got = s.all_trades()
        self.assertEqual(len(got), 2300)          # paged through, not truncated
        self.assertEqual(got[0]["id"], 1)
        self.assertEqual(got[-1]["id"], 2300)

    def test_last_cycle(self):
        s = self._store()
        self.assertIsNone(s.last_cycle())
        s.session.cycles = [{"id": 1, "run_at": "t1"}, {"id": 2, "run_at": "t2"}]
        self.assertEqual(s.last_cycle()["id"], 2)

    def test_write_site_supabase_path(self):
        """The hybrid production path: build docs/data.json from a Supabase-backed
        store. The watch-derived sections must populate from kv_store."""
        import tempfile
        from poller import publish
        s = self._store()
        s.session.kv["consensus_watch"] = {
            "c1|0": {"condition_id": "c1", "outcome_index": 0, "title": "A", "outcome": "Yes",
                     "slug": "a", "first_price": 0.4, "max_overlap": 6, "tier": "green",
                     "resolved": True, "resolved_at": "2026-06-16T00:00:00Z",
                     "exit_price": 1.0, "won": True, "holders": ["w1", "w2"]},
        }
        # a real cycle always has a cohort snapshot too; write_site now refuses to
        # publish a hollow payload, so give it one trader to represent that.
        s.session.kv["latest_traders"] = [{"wallet": "w1", "pnl": 100.0, "positions": []}]
        with tempfile.TemporaryDirectory() as d:
            path = publish.write_site(s, {"cycle_id": 1, "status": "ok"}, docs_dir=d)
            payload = json.load(open(path))
        self.assertEqual(payload["backtest"]["resolved_total"], 1)
        self.assertEqual(len(payload["resolved_markets"]), 1)
        self.assertTrue(payload["resolved_markets"][0]["won"])
        self.assertIn("w1", payload["trader_scores"])
        self.assertEqual(payload["calibration"]["ge5"]["wins"], 1)

    def test_observations_table_keeps_only_consensus_snapshot_keeps_all(self):
        """The durable table persists only overlap>=2 (the empirical record), while
        the dashboard snapshot (latest_observations) keeps every overlap, strongest
        first — so the single-wallet 'All Signals' rows still render."""
        s = self._store()
        rows = [
            {"asset": "a", "overlap": 1, "cycle_id": 1},   # single wallet -> snapshot only
            {"asset": "b", "overlap": 2, "cycle_id": 1},
            {"asset": "c", "overlap": 5, "cycle_id": 1},
        ]
        s.insert_observations(rows)
        # table = consensus only
        self.assertEqual({o["asset"] for o in s.session.observations}, {"b", "c"})
        # snapshot = all overlaps, sorted desc, with observed_at stamped
        snap = s.latest_observations()
        self.assertEqual([o["asset"] for o in snap], ["c", "b", "a"])
        self.assertTrue(all(o.get("observed_at") for o in snap))

    def test_prune_snapshots_drops_old_keeps_recent_and_spares_trades(self):
        from datetime import datetime, timezone, timedelta
        s = self._store()
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=72)).isoformat()
        recent = (now - timedelta(hours=2)).isoformat()
        s.session.observations = [{"asset": "old", "observed_at": old},
                                  {"asset": "new", "observed_at": recent}]
        s.session.leaderboard = [{"wallet": "old", "captured_at": old},
                                 {"wallet": "new", "captured_at": recent}]
        s.session.trades = [{"id": 1, "status": "CLOSED"}]      # the durable record
        s.prune_snapshots(retain_hours=48)
        self.assertEqual([o["asset"] for o in s.session.observations], ["new"])
        self.assertEqual([o["wallet"] for o in s.session.leaderboard], ["new"])
        self.assertEqual(len(s.session.trades), 1)             # paper_trades untouched
        # self-throttle: an immediate second call is a no-op (it just ran)
        s.session.observations.append({"asset": "old2", "observed_at": old})
        s.prune_snapshots(retain_hours=48)
        self.assertIn("old2", [o["asset"] for o in s.session.observations])


if __name__ == "__main__":
    unittest.main()
