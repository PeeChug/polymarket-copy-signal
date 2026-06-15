"""
Poller entry point — run once per invocation (every 30 min via GitHub Actions).

    python -m poller.main              # real run; needs SUPABASE_URL + SUPABASE_KEY
    python -m poller.main --dry-run    # full live cycle against an in-memory store
                                       # (hits Polymarket read-only APIs, writes NO db)

The dry-run is the recommended first check: it exercises the entire pipeline
end-to-end with real Polymarket data but requires no database, so you can
confirm the endpoints and logic before wiring up Supabase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# allow `python -m poller.main` from the repo root and direct execution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import PostgrestStore, MemoryStore, FileStore  # noqa: E402
from core.config import sync_yaml_config, default_yaml_path     # noqa: E402
from poller.engine import run_cycle                 # noqa: E402
from poller.polymarket import PolymarketClient       # noqa: E402
from poller.publish import write_site                # noqa: E402


def build_store(dry_run: bool):
    """Pick a backend: in-memory for dry-run, Supabase if configured, else the
    free file-based store (the default for the GitHub-Pages deployment)."""
    if dry_run:
        return MemoryStore(), "memory (dry-run)"
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return PostgrestStore(url, key), "supabase"
    data_dir = os.environ.get("DATA_DIR", "data")
    return FileStore(data_dir), f"file ({data_dir})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Polymarket copy-signal poller (read-only).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run a full live cycle with an in-memory store; write nothing.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the poll_interval_minutes gate (used by manual runs).")
    ap.add_argument("--dump", metavar="FILE",
                    help="(dry-run) write the resulting in-memory tables to FILE as JSON.")
    args = ap.parse_args(argv)

    store, kind = build_store(args.dry_run)
    print(f"== Polymarket copy-signal poller :: store={kind} ==")
    client = PolymarketClient()

    # Forward-only settings editor for the file deployment: a changed config.yaml
    # becomes a new config row applied to this and future cycles only.
    if isinstance(store, FileStore) and sync_yaml_config(store, default_yaml_path()):
        print("applied updated config.yaml as a new forward-only config row")

    # interval gate: the Action wakes every 5 min, but only work once the configured
    # poll_interval_minutes has elapsed (manual --force runs skip the gate).
    if isinstance(store, FileStore) and not args.force:
        last = store.last_cycle()
        interval = (store.latest_config() or {}).get("poll_interval_minutes") or 15
        if last and last.get("run_at"):
            try:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last["run_at"])).total_seconds() / 60
                if elapsed < interval - 1.5:  # tolerance for cron jitter
                    print(f"skip: {elapsed:.1f} min since last cycle (interval {interval}m); "
                          f"use --force to override.")
                    return 0
            except (ValueError, TypeError):
                pass

    result = run_cycle(store, client)

    # Publish the precomputed payload the static dashboard reads.
    if isinstance(store, FileStore):
        site_path = write_site(store, result, os.environ.get("DOCS_DIR", "docs"))
        print(f"wrote dashboard payload -> {site_path}")

    if args.dry_run:
        trades = store.all_trades()
        print("\n-- dry-run paper trades --")
        for t in trades:
            print(f"  [{t['strategy']:7}] {t['tier_at_entry'] or '-':5} {t['status']:6} "
                  f"{(t['title'] or '')[:42]!r} [{t['outcome']}] entry={t['entry_price']:.3f} "
                  f"shares={t['shares']:.1f}")
        if not trades:
            print("  (no signals crossed the guardrails this cycle — expected if the "
                  "current top traders share few liquid, low-priced positions)")
        if args.dump:
            with open(args.dump, "w") as fh:
                json.dump({
                    "cycle": result,
                    "config": store.config_history(),
                    "leaderboard": store.latest_leaderboard(),
                    "observations": store.latest_observations(),
                    "trades": trades,
                }, fh, indent=2, default=str)
            print(f"\nwrote dry-run dump -> {args.dump}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
