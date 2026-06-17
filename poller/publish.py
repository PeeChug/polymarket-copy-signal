"""
Publish a precomputed dashboard payload (docs/data.json) for the static
GitHub-Pages site. Runs in the poller after each cycle; reuses the tested
pure aggregation in core.analytics so the browser page is render-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from core import analytics, accounts
from poller import us_market


def _tag_us_availability(payload: dict, store=None) -> None:
    """Flag which positions/signals are tradeable on Polymarket US (best-effort).

    Powers the dashboard's "US Only" toggle. Pulls the live keyless US event
    catalog (with retries), caches it as the last-good catalog, and tags
    us_available + a link on each row. If the live fetch fails this cycle we tag
    from the cached catalog (marked `stale`) instead of blanking the US view —
    that resilience is what makes the US signals consistent. Never raises."""
    try:
        idx, events = us_market.build_us_index()
        stale, fetched_at = False, None
        if idx and events:
            fetched_at = datetime.now(timezone.utc).isoformat()
            if store:
                try:
                    store.set_us_catalog({"events": events, "fetched_at": fetched_at})
                except Exception as e:
                    print(f"us_market: catalog cache write failed: {e}")
        else:
            # live fetch failed → fall back to the last-good cached catalog
            cached = (store.get_us_catalog() if store else {}) or {}
            events = cached.get("events") or []
            fetched_at = cached.get("fetched_at")
            idx = us_market.build_index_from_events(events) if events else None
            if not idx:
                print("us_market: no live catalog and no cache — skipping US tagging.")
                return
            stale = True
            print(f"us_market: live fetch failed; tagging from cached catalog ({fetched_at}).")

        counts = {
            "consensus": us_market.tag_rows(payload.get("consensus"), idx),
            "open": us_market.tag_rows(payload.get("open_positions"), idx),
            "closed": us_market.tag_rows(payload.get("closed_positions"), idx),
        }
        us_market.tag_rows(payload.get("signals"), idx)   # keep signals consistent

        by_rule: dict = {}                                # per-rule breakdown (tuning/observability)
        for r in (payload.get("consensus") or []):
            if r.get("us_available"):
                k = r.get("us_match", "?")
                by_rule[k] = by_rule.get(k, 0) + 1

        payload["us"] = {
            **counts, "events": len(idx["items"]), "by_rule": by_rule,
            "ok": not stale, "stale": stale, "fetched_at": fetched_at,
            "event_base": us_market.US_EVENT_URL,
        }
        print(f"us_market: tagged US-tradeable — {payload['us']}")
    except Exception as e:                                # never break publish
        print(f"us_market: tagging skipped ({e})")


def write_site(store, run_result: dict, docs_dir: str = "docs") -> str:
    os.makedirs(docs_dir, exist_ok=True)
    payload = analytics.dashboard_payload(
        trades=store.all_trades(),
        observations=store.latest_observations(),
        leaderboard=store.latest_leaderboard(),
        config_rows=store.config_history(limit=50),
        traders=store.latest_traders(),
        agreement=store.get_agreement(),
        meta={"generated_at": datetime.now(timezone.utc).isoformat(),
              "last_cycle": run_result},
    )

    # Safety: never overwrite a good dashboard payload with a hollow one. If this
    # cycle produced no cohort/observations (e.g. a Polymarket outage), keep the
    # last good data.json / Storage object instead of blanking the dashboard.
    if not payload.get("leaderboard") and not payload.get("traders"):
        print("write_site: empty payload (no leaderboard/traders) — keeping last good, not publishing.")
        return None

    # append a compact time-series snapshot, then attach the series + derived views
    perf, ag = payload["performance"], payload["agreement"]
    store.append_history({
        "ts": payload["generated_at"], "cycle": (run_result or {}).get("cycle_id"),
        "ov_net": perf["overlap"]["net_pnl"], "ov_real": perf["overlap"]["realized_pnl"],
        "ov_unreal": perf["overlap"]["unrealized_pnl"],
        "ct_net": perf["control"]["net_pnl"], "ct_real": perf["control"]["realized_pnl"],
        "ct_unreal": perf["control"]["unrealized_pnl"],
        # ROI% (net / dollars staked) so consensus vs control compares fairly even
        # though the control deploys far more capital (it copies the whole #1 book)
        "ov_roi": perf["overlap"].get("roi_total") or 0,
        "ct_roi": perf["control"].get("roi_total") or 0,
        "green_net": payload["tiers"]["green"]["net_pnl"], "blue_net": payload["tiers"]["blue"]["net_pnl"],
        "ge2": ag.get("ge2"), "ge3": ag.get("ge3"), "ge5": ag.get("ge5"),
        "max_overlap": ag.get("max_overlap"), "positions": ag.get("positions"),
    })
    payload["history"] = store.history(limit=1000)
    watch = store.get_consensus_watch()
    payload["calibration"] = analytics.calibration(watch)
    payload["backtest"] = analytics.backtest(watch)
    scores = analytics.trader_scores(watch)
    payload["trader_scores"] = scores            # keyed by wallet (smart-money lookup)
    # resolved consensus markets (the "who won" history), newest first, capped
    resolved_mkts = sorted([w for w in watch.values() if w.get("resolved")],
                           key=lambda w: str(w.get("resolved_at") or ""), reverse=True)
    payload["resolved_markets"] = [{
        "title": w.get("title"), "outcome": w.get("outcome"), "slug": w.get("slug"),
        "tier": w.get("tier"), "max_overlap": w.get("max_overlap"), "won": w.get("won"),
        "first_price": w.get("first_price"), "exit_price": w.get("exit_price"),
        "resolved_at": w.get("resolved_at"), "holders": (w.get("holders") or [])[:30],
    } for w in resolved_mkts[:150]]
    series = store.get_trader_series()
    for t in payload["traders"]:
        t["spark"] = series.get(t["wallet"], [])
        t["sharp"] = scores.get(t["wallet"])     # per-trader consensus hit-rate (accrues)

    # enrich signals/consensus with consensus age + peak agreement from the watch
    # (consensus items are references into the signals list, so iterating signals covers both)
    for r in payload["signals"]:
        w = watch.get(f"{r.get('condition_id')}|{r.get('outcome_index')}")
        if w:
            r["first_seen"] = w.get("first_seen")
            r["peak_overlap"] = w.get("max_overlap")
            r["momentum"] = w.get("momentum", 0)

    # "what changed" this cycle: new / growing / resolved consensus (recent activity)
    now = datetime.now(timezone.utc)

    def _recent(ts, mins=5):
        try:
            return bool(ts) and (now - datetime.fromisoformat(ts)).total_seconds() <= mins * 60
        except (ValueError, TypeError):
            return False

    changes = {"new": [], "grown": [], "resolved": []}
    for w in watch.values():
        base = {"title": w.get("title"), "outcome": w.get("outcome"), "slug": w.get("slug"),
                "overlap": w.get("cur_overlap", w.get("max_overlap")), "momentum": w.get("momentum", 0)}
        if _recent(w.get("resolved_at")):
            changes["resolved"].append({**base, "won": w.get("won")})
        elif _recent(w.get("first_seen")):
            changes["new"].append(base)
        elif _recent(w.get("last_seen")) and w.get("momentum", 0) > 0:
            changes["grown"].append(base)
    changes["new"].sort(key=lambda x: x["overlap"], reverse=True)
    changes["grown"].sort(key=lambda x: x["momentum"], reverse=True)
    payload["changes"] = {k: v[:20] for k, v in changes.items()}

    # realistic finite-budget account sims (slippage/fees/reinvestment) — a pure
    # replay of the consensus trades, so no engine/DB changes needed
    try:
        wcfgs = accounts.wallet_configs_from(store.get_wallet_config())
        payload["accounts"] = accounts.simulate_all(store.all_trades(), wcfgs)
    except Exception as e:
        print(f"accounts: sim skipped ({e})")

    # flag which markets a US-based user could actually trade (US Only toggle)
    _tag_us_availability(payload, store)

    path = os.path.join(docs_dir, "data.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)

    pub = _publish_to_supabase_storage(payload)
    if pub:
        print(f"published dashboard payload -> {pub}")
    return path


def _publish_to_supabase_storage(payload: dict, bucket: str = "dashboard", name: str = "data.json"):
    """Best-effort: upload the dashboard payload to a PUBLIC Supabase Storage
    bucket so the dashboard can fetch it directly — no git commit / Pages build,
    which is what lets the poll cadence go high. No-op when SUPABASE_* aren't set.
    The bucket is created on first run (idempotent). Same already-public data as
    the committed docs/data.json; this just hosts it where it can update freely."""
    import requests
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        return None
    base = url.rstrip("/") + "/storage/v1"
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}
    body = json.dumps(payload, default=str).encode("utf-8")
    try:
        # ensure the public bucket exists (409/400 if it already does — fine)
        requests.post(f"{base}/bucket", headers={**auth, "Content-Type": "application/json"},
                      data=json.dumps({"id": bucket, "name": bucket, "public": True}), timeout=15)
        r = requests.post(f"{base}/object/{bucket}/{name}",
                          headers={**auth, "Content-Type": "application/json", "x-upsert": "true"},
                          data=body, timeout=30)
        if r.ok:
            return f"{url.rstrip('/')}/storage/v1/object/public/{bucket}/{name}"
        print(f"supabase storage upload failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"supabase storage upload error: {e}")
    return None
