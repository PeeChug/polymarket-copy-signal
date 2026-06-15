"""
Publish a precomputed dashboard payload (docs/data.json) for the static
GitHub-Pages site. Runs in the poller after each cycle; reuses the tested
pure aggregation in core.analytics so the browser page is render-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from core import analytics


def write_site(store, run_result: dict, docs_dir: str = "docs") -> str:
    os.makedirs(docs_dir, exist_ok=True)
    payload = analytics.dashboard_payload(
        trades=store.all_trades(),
        observations=store.latest_observations(),
        leaderboard=store.latest_leaderboard(),
        config_rows=store.config_history(limit=50),
        traders=store.latest_traders(),
        meta={"generated_at": datetime.now(timezone.utc).isoformat(),
              "last_cycle": run_result},
    )
    path = os.path.join(docs_dir, "data.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)
    return path
