"""One-time data migration: Supabase (PostgrestStore) -> Cloudflare D1 (D1Store).

Wipes the D1 tables, then copies the DURABLE data preserving ids:
  * config_history (last 50)      — so the D1 poller uses the tuned config
  * cycles (last 500)             — last_cycle gate + FK targets
  * paper_trades (ALL)            — the track record / P&L history
  * kv_store (every blob)         — consensus_watch = the calibration/backtest
                                    history (the resolution-fix win), plus
                                    history/agreement/traders/wallet_config/etc.
observations + leaderboard_snapshots are transient (48h retention) and are
started fresh. Idempotent — safe to re-run (it wipes first).

Runs in GitHub Actions, where both SUPABASE_* and CF_* secrets exist, so no
credential is ever handled locally.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import PostgrestStore, D1Store   # noqa: E402


def _src() -> PostgrestStore:
    key = os.environ.get("SUPABASE_KEY") or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return PostgrestStore(os.environ["SUPABASE_URL"], key)


def _dst() -> D1Store:
    return D1Store(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_DATABASE_ID"],
                   os.environ["CF_D1_TOKEN"])


def main() -> int:
    src, dst = _src(), _dst()

    print("wiping D1 tables for a clean copy…")
    for t in ("paper_trades", "config_history", "cycles", "kv_store",
              "observations", "leaderboard_snapshots", "site_blob"):
        dst._run(f"DELETE FROM {t}")

    cfgs = list(reversed(src.config_history(limit=50)))          # oldest first, preserve ids
    for r in cfgs:
        dst._insert("config_history", r)
    print(f"config_history: {len(cfgs)}")

    cyc = list(reversed(src._select("cycles", order="id.desc", limit=500)))
    for r in cyc:
        dst._insert("cycles", r)
    print(f"cycles: {len(cyc)}")

    trades = src.all_trades()
    ok = sum(1 for r in trades if dst._insert("paper_trades", r) is not None)
    print(f"paper_trades: {ok}/{len(trades)}")

    kv = src._select("kv_store", select="key,value")
    for row in kv:
        try:
            dst._kv_set(row["key"], row["value"])
            print(f"  kv {row['key']}: ok")
        except Exception as e:                                   # keep going on one bad blob
            print(f"  kv {row['key']}: FAILED ({str(e)[:140]})")
    print(f"kv_store: {len(kv)} keys attempted")

    def _n(t):
        rows, _ = dst._run(f"SELECT count(*) AS c FROM {t}")
        return rows[0]["c"]

    print(f"VERIFY D1 -> paper_trades={_n('paper_trades')} kv_store={_n('kv_store')} "
          f"config_history={_n('config_history')} cycles={_n('cycles')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
