"""D1Store — the Cloudflare-native backend, against a minimal fake D1 HTTP session.

Full SQL correctness is covered by a live smoke test during the migration; here we
pin the D1-specific logic: value serialization (bools/arrays -> 0/1/JSON, and back),
the kv round-trip, and the observations split (dashboard snapshot keeps ALL overlaps
while only consensus rows land in the durable table).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import D1Store


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class FakeD1Session:
    """Answers kv SELECTs from an in-memory dict; records every INSERT."""
    def __init__(self):
        self.kv = {}
        self.inserts = []
        self.headers = {}

    def post(self, url, data=None, timeout=None):
        body = json.loads(data)
        sql, p = body["sql"].strip(), body["params"]
        low = sql.lower()
        if low.startswith("insert into kv_store"):
            self.kv[p[0]] = p[1]
            return _Resp({"success": True, "result": [{"results": [], "meta": {}}]})
        if low.startswith("select value from kv_store"):
            rows = [{"value": self.kv[p[0]]}] if p[0] in self.kv else []
            return _Resp({"success": True, "result": [{"results": rows, "meta": {}}]})
        if low.startswith("insert into"):
            self.inserts.append((sql.split()[2].strip('"'), body))
            return _Resp({"success": True, "result": [{"results": [], "meta": {"last_row_id": len(self.inserts)}}]})
        return _Resp({"success": True, "result": [{"results": [], "meta": {}}]})


class TestD1Store(unittest.TestCase):
    def _store(self):
        s = D1Store("acc", "db", "tok")
        s.session = FakeD1Session()
        return s

    def test_bind_serializes_bools_and_arrays(self):
        self.assertEqual(D1Store._bind(True), 1)
        self.assertEqual(D1Store._bind(False), 0)
        self.assertEqual(D1Store._bind(["a", "b"]), '["a", "b"]')
        self.assertEqual(D1Store._bind(0.5), 0.5)
        self.assertIsNone(D1Store._bind(None))

    def test_out_parses_json_and_bools(self):
        s = self._store()
        row = s._out("paper_trades", {"holders_at_entry": '["w1","w2"]', "resolved_won": 1})
        self.assertEqual(row["holders_at_entry"], ["w1", "w2"])
        self.assertIs(row["resolved_won"], True)
        obs = s._out("observations", {"market_closed": 0, "holder_wallets": "[]"})
        self.assertIs(obs["market_closed"], False)
        self.assertEqual(obs["holder_wallets"], [])

    def test_kv_roundtrip_and_history_cap(self):
        s = self._store()
        s.set_consensus_watch({"k": {"max_overlap": 3, "resolved": True}})
        self.assertEqual(s.get_consensus_watch()["k"]["max_overlap"], 3)
        s.append_history({"i": 1})
        s.append_history({"i": 2})
        self.assertEqual([h["i"] for h in s.history()], [1, 2])

    def test_observations_split_snapshot_vs_table(self):
        s = self._store()
        s.insert_observations([
            {"asset": "A", "overlap": 1, "holder_wallets": ["w"]},          # single wallet
            {"asset": "B", "overlap": 3, "holder_wallets": ["w", "x", "y"]},
        ])
        snap = s.latest_observations()                                       # dashboard feed: ALL
        self.assertEqual([o["asset"] for o in snap], ["B", "A"])
        obs_inserts = [i for i in s.session.inserts if i[0] == "observations"]
        self.assertEqual(len(obs_inserts), 1)                               # table: consensus only
        self.assertIn("B", obs_inserts[0][1]["params"])

    def test_insert_returns_id(self):
        s = self._store()
        t = s.insert_trade({"strategy": "overlap", "condition_id": "c", "asset": "A",
                            "outcome_index": 0, "entry_price": 0.5})
        self.assertEqual(t["id"], 1)


if __name__ == "__main__":
    unittest.main()
