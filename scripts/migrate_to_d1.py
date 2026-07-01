"""One-time data migration: Supabase (PostgrestStore) -> Cloudflare D1 (D1Store).

Copies the DURABLE data preserving ids:
  * config_history (last 50)      — so the D1 poller uses the tuned config
  * cycles (last 500)             — last_cycle gate + FK targets
  * paper_trades (ALL)            — the track record / P&L history
  * kv_store (every blob)         — consensus_watch = the calibration/backtest
                                    history, plus history/agreement/traders/etc.
observations + leaderboard_snapshots are transient (48h) and started fresh.

RESUMABLE: each table is copied only if the D1 table is still empty, so a re-run
after a partial failure continues where it left off (paper_trades won't be
re-copied). kv blobs are read PER KEY with a long timeout — the consensus_watch
blob is multi-MB and a single all-rows GET times out. Runs in GitHub Actions
where both SUPABASE_* and CF_* secrets live.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import PostgrestStore, D1Store   # noqa: E402


def main() -> int:
    key = os.environ.get("SUPABASE_KEY") or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    src = PostgrestStore(os.environ["SUPABASE_URL"], key, timeout=120)   # big reads need headroom
    dst = D1Store(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_DATABASE_ID"],
                  os.environ["CF_D1_TOKEN"], timeout=120)

    def n(t):
        rows, _ = dst._run(f"SELECT count(*) AS c FROM {t}")
        return rows[0]["c"]

    if n("config_history") == 0:
        for r in reversed(src.config_history(limit=50)):
            dst._insert("config_history", r)
    print(f"config_history: {n('config_history')}")

    if n("cycles") == 0:
        for r in reversed(src._select("cycles", order="id.desc", limit=500)):
            dst._insert("cycles", r)
    print(f"cycles: {n('cycles')}")

    if n("paper_trades") == 0:
        trades = src.all_trades()
        ok = sum(1 for r in trades if dst._insert("paper_trades", r) is not None)
        print(f"paper_trades: {ok}/{len(trades)} copied")
    else:
        print(f"paper_trades: {n('paper_trades')} already present — skipped")

    if n("kv_store") == 0:
        import json as _json
        from core.analytics import bound_consensus_watch
        keys = [r["key"] for r in src._select("kv_store", select="key")]
        print(f"kv keys: {keys}")
        for k in keys:                                   # per-key: the big blob alone, not all at once
            try:
                rows = src._select("kv_store", select="value", key=f"eq.{k}", limit=1)
                if not rows:
                    continue
                val = rows[0]["value"]
                if k == "consensus_watch":               # 6.5MB -> trim to fit D1's 2MB value limit
                    cap = 1200
                    val = bound_consensus_watch(val, max_entries=cap)
                    while len(_json.dumps(val, default=str)) > 1_900_000 and cap > 100:
                        cap = int(cap * 0.7)
                        val = bound_consensus_watch(rows[0]["value"], max_entries=cap)
                    print(f"  consensus_watch trimmed to {len(val)} entries "
                          f"({len(_json.dumps(val, default=str)) // 1024} KB)")
                if len(_json.dumps(val, default=str)) > 1_950_000:
                    print(f"  kv {k}: SKIPPED (over D1's 2MB value limit)")
                    continue
                dst._kv_set(k, val)
                print(f"  kv {k}: ok")
            except Exception as e:
                print(f"  kv {k}: FAILED ({str(e)[:160]})")
    print(f"kv_store: {n('kv_store')}")

    print(f"VERIFY D1 -> paper_trades={n('paper_trades')} kv_store={n('kv_store')} "
          f"config_history={n('config_history')} cycles={n('cycles')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
