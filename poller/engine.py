"""
The cycle engine. One `run_cycle()` does the whole job:

  1. load the live (newest) config
  2. fetch the profit leaderboard -> snapshot it
  3. fetch every cohort wallet's OPEN positions
  4. compute overlap per (market, outcome) and LOG EVERY ONE (honesty rule #2)
  5. open paper trades for qualifying signals — overlap strategy AND the
     #1-copy control benchmark (honesty rule #3) — at the price available NOW,
     locked forever (honesty rule #1)
  6. mark every open trade to market; close on resolution or abandonment

It is deliberately decoupled from transport: it takes a `store` (Supabase or
in-memory) and a `client` (the thin Polymarket client). Nothing here can place
a real order — the client is read-only.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from poller import strategy
from poller.alerts import notify_trade_opened
from core.config import load_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle(store, client, seed_path=None, log=print) -> dict:
    t0 = time.time()
    cfg = load_config(store, seed_path)
    log(f"config #{cfg.id} (source={cfg.source}): top_n={cfg.top_n} window={cfg.leaderboard_window} "
        f"tiers green>={cfg.tier_green_min}/blue>={cfg.tier_blue_min} "
        f"min_tier={cfg.min_tier_to_trade} stake=${cfg.stake_usd} "
        f"min_liq=${cfg.min_liquidity} max_entry={cfg.max_entry_price}")

    cycle = store.create_cycle({
        "config_id": cfg.id, "top_n": cfg.top_n, "window": cfg.leaderboard_window, "status": "ok",
    })
    cid = cycle["id"]

    summary = {"n_traders": 0, "n_observations": 0, "n_signals": 0,
               "opened_overlap": 0, "opened_control": 0, "closed": 0}
    try:
        _run(store, client, cfg, cid, summary, log)
        status, err = "ok", None
    except Exception as e:  # record the failure on the cycle row, then re-raise
        status, err = "error", f"{type(e).__name__}: {e}"
        store.update_cycle(cid, {**summary, "status": status, "error": err,
                                 "duration_ms": int((time.time() - t0) * 1000)})
        raise

    summary.update({"status": status, "error": err,
                    "duration_ms": int((time.time() - t0) * 1000)})
    store.update_cycle(cid, summary)
    log(f"cycle #{cid} done in {summary['duration_ms']}ms: "
        f"{summary['n_observations']} obs, {summary['n_signals']} signals, "
        f"opened overlap={summary['opened_overlap']} control={summary['opened_control']}, "
        f"closed={summary['closed']}")
    return {"cycle_id": cid, **summary}


def _run(store, client, cfg, cid, summary, log):
    # ---- 1+2. leaderboard --------------------------------------------------
    entries = client.leaderboard(window=cfg.leaderboard_window, limit=cfg.top_n)
    if not entries:
        log("WARNING: leaderboard returned no entries; nothing to do this cycle.")
        return
    cohort = entries[: cfg.top_n]
    cohort_wallets = [e.wallet for e in cohort]
    leader = cohort[0]
    by_wallet = {e.wallet: e for e in cohort}
    summary["n_traders"] = len(cohort)

    store.insert_leaderboard([{
        "cycle_id": cid, "window": cfg.leaderboard_window, "rank": e.rank,
        "wallet": e.wallet, "username": e.username, "pnl": e.pnl, "volume": e.volume,
        "in_cohort": True,
    } for e in cohort])
    log(f"cohort: " + ", ".join(f"#{e.rank} {e.username or e.wallet[:8]}" for e in cohort))

    # ---- 3. open positions per cohort wallet (fetched concurrently) --------
    cohort_positions = client.positions_many(cohort_wallets, size_threshold=cfg.size_threshold)
    for e in cohort:
        for p in cohort_positions.get(e.wallet, []):   # annotate for overlap labelling
            p._username = e.username
            p._rank = e.rank
    leader_positions = cohort_positions.get(leader.wallet, [])
    leader_assets = {p.asset for p in leader_positions}

    # ---- 4. overlaps -------------------------------------------------------
    overlaps = strategy.compute_overlaps(cohort_positions)
    cohort_assets = set(overlaps.keys())
    log(f"{sum(len(v) for v in cohort_positions.values())} positions across cohort "
        f"-> {len(overlaps)} distinct (market,outcome) pairs")

    # per-trader snapshot for the dashboard (their positions + agreement enrichment)
    store.set_traders(_build_traders(cohort, cohort_positions, overlaps))

    # existing open trades — needed below (and to know what to price)
    open_index = {(t["strategy"], t["condition_id"], t["outcome_index"]): t
                  for t in store.open_trades()}

    # ---- BATCH-FETCH markets + marks once for everything we touch ----------
    # include unresolved consensus-watch markets so we can detect their resolution
    watched_conds = {w["condition_id"] for w in store.get_consensus_watch().values() if not w.get("resolved")}
    all_conditions = ({ov.condition_id for ov in overlaps.values()}
                      | {t["condition_id"] for t in open_index.values()} | watched_conds)
    all_assets = set(overlaps.keys()) | {t["asset"] for t in open_index.values()}
    market_map = client.markets(all_conditions)
    mark_map = client.marks(all_assets, source=cfg.price_source)
    log(f"batched {len(market_map)}/{len(all_conditions)} markets, {len(mark_map)}/{len(all_assets)} live prices")

    def get_market(condition_id):
        return market_map.get(condition_id)

    def get_mark(asset, condition_id, outcome_index, fallback=None):
        p = mark_map.get(asset)
        if p is not None:
            return p
        m = market_map.get(condition_id)  # resolved markets have no live book -> use payout
        if m is not None:
            rp = m.resolved_price_for(outcome_index)
            if rp is not None:
                return rp
        return fallback

    # ---- 5a. observations: LOG EVERYTHING ----------------------------------
    obs_rows = []
    observed = {}  # asset -> (tier, price, liquidity, closed) for the open step
    for asset, ov in overlaps.items():
        m = get_market(ov.condition_id)
        price = get_mark(asset, ov.condition_id, ov.outcome_index, fallback=ov.fallback_price)
        liquidity = m.liquidity if m else None
        closed = bool(m.closed) if m else False
        active = bool(m.active) if m else None
        tier = cfg.tier_for(ov.overlap)
        observed[asset] = (ov, tier, price, liquidity, closed)
        obs_rows.append({
            "cycle_id": cid, "condition_id": ov.condition_id, "asset": asset,
            "outcome": ov.outcome, "outcome_index": ov.outcome_index,
            "title": ov.title, "slug": ov.slug,
            "overlap": ov.overlap, "tier": tier,
            "holder_wallets": ov.wallets, "holder_usernames": ov.usernames, "holder_ranks": ov.ranks,
            "price": price, "liquidity": liquidity,
            "market_closed": closed, "market_active": active, "end_date": ov.end_date,
        })
    store.insert_observations(obs_rows)
    summary["n_observations"] = len(obs_rows)

    def try_open(strat, asset, ov, tier, price, liquidity, closed):
        g = strategy.passes_guardrails(tier=tier, price=price, liquidity=liquidity,
                                       market_closed=closed, cfg=cfg, strategy=strat)
        if not g.ok:
            return
        key = (strat, ov.condition_id, ov.outcome_index)
        if key in open_index:
            return  # already holding this (strategy, market, outcome)
        shares = strategy.shares_for(cfg.stake_usd, price)
        if shares <= 0:
            return
        payload = {
            "strategy": strat, "condition_id": ov.condition_id, "asset": asset,
            "outcome": ov.outcome, "outcome_index": ov.outcome_index, "title": ov.title,
            "status": "OPEN", "entry_at": _now_iso(), "entry_cycle_id": cid,
            "entry_price": price, "stake_usd": cfg.stake_usd, "shares": shares,
            "tier_at_entry": tier, "overlap_at_entry": ov.overlap, "holders_at_entry": ov.wallets,
            "marked_price": price, "marked_at": _now_iso(), "unrealized_pnl": 0.0,
        }
        row = store.insert_trade(payload)
        if row is None:
            return
        open_index[key] = row
        summary["opened_overlap" if strat == "overlap" else "opened_control"] += 1
        log(f"  OPEN [{strat}] {tier:5} {ov.title[:40]!r} [{ov.outcome}] "
            f"@ {price:.3f}  overlap={ov.overlap}")
        if strat == "overlap":
            # ---- ALERT SEAM ---------------------------------------------- #
            # A new green/blue overlap trade just opened. A future Telegram /
            # phone alert hooks in here. Intentionally a no-op for now.
            notify_trade_opened(row, cfg)

    # ---- 5b. open overlap signals -----------------------------------------
    for asset, (ov, tier, price, liquidity, closed) in observed.items():
        if cfg.tier_meets_minimum(tier):
            summary["n_signals"] += 1
        try_open("overlap", asset, ov, tier, price, liquidity, closed)

    # ---- 5c. open control signals (naive copy of the #1 trader) -----------
    for asset in leader_assets:
        rec = observed.get(asset)
        if rec is None:
            continue
        ov, tier, price, liquidity, closed = rec
        try_open("control", asset, ov, tier, price, liquidity, closed)

    # ---- 6. mark-to-market + close every open trade ------------------------
    for t in store.open_trades():
        m = get_market(t["condition_id"])
        mark = get_mark(t["asset"], t["condition_id"], t["outcome_index"],
                        fallback=t.get("marked_price"))
        resolved = bool(m and m.closed)
        held = (t["asset"] in cohort_assets) if t["strategy"] == "overlap" else (t["asset"] in leader_assets)

        if resolved:
            exit_price = m.resolved_price_for(t["outcome_index"])
            if exit_price is None:
                exit_price = mark if mark is not None else t.get("marked_price") or 0.0
            _close(store, t, exit_price, "resolved", summary,
                   resolved_won=(exit_price is not None and exit_price >= 0.5))
            log(f"  CLOSE [{t['strategy']}] resolved {t['title'][:36]!r} @ {exit_price:.3f}")
        elif not held:
            exit_price = mark if mark is not None else (t.get("marked_price") or 0.0)
            reason = "cohort_abandoned" if t["strategy"] == "overlap" else "leader_abandoned"
            _close(store, t, exit_price, reason, summary, resolved_won=None)
            log(f"  CLOSE [{t['strategy']}] {reason} {t['title'][:32]!r} @ {exit_price:.3f}")
        else:
            if mark is None:
                continue
            upnl = strategy.unrealized_pnl(t["shares"], t["entry_price"], mark)
            store.update_trade(t["id"], {
                "marked_price": mark, "marked_at": _now_iso(), "unrealized_pnl": upnl,
            })

    # ---- accumulate trackers for the dashboard (consensus hit-rate + sparklines)
    _update_trackers(store, cohort, observed, market_map, _now_iso())


def _build_traders(cohort, cohort_positions, overlaps, max_positions=25):
    """One row per cohort trader: leaderboard stats + their open positions,
    each position annotated with how many of the cohort also hold it."""
    traders = []
    for e in cohort:
        positions = cohort_positions.get(e.wallet, [])
        rows, total_value, open_pnl = [], 0.0, 0.0
        for p in positions:
            ov = overlaps.get(p.asset)
            total_value += p.current_value
            open_pnl += p.cash_pnl
            rows.append({
                "title": p.title, "outcome": p.outcome, "asset": p.asset,
                "condition_id": p.condition_id, "slug": p.slug,
                "size": p.size, "avg_price": p.avg_price, "cur_price": p.cur_price,
                "current_value": p.current_value, "cash_pnl": p.cash_pnl,
                "percent_pnl": p.percent_pnl, "overlap": ov.overlap if ov else 1,
            })
        rows.sort(key=lambda r: r["current_value"], reverse=True)
        traders.append({
            "rank": e.rank, "wallet": e.wallet, "username": e.username,
            "pnl": e.pnl, "volume": e.volume, "profile_image": e.profile_image,
            "x_username": e.x_username, "verified": e.verified,
            "n_positions": len(positions), "total_value": total_value, "open_pnl": open_pnl,
            "positions": rows[:max_positions],
        })
    return traders


def _update_trackers(store, cohort, observed, market_map, now_iso):
    """Maintain the accumulating dashboard trackers:
       - trader_series: a capped per-wallet 30d-profit series for sparklines
       - consensus_watch: every position ever held by >=2, and its resolution
         outcome (the consensus hit-rate / calibration data)."""
    # per-trader profit sparkline (kept only for the current cohort, capped)
    series = store.get_trader_series()
    store.set_trader_series({e.wallet: (series.get(e.wallet, []) + [round(e.pnl, 2)])[-60:]
                             for e in cohort})

    watch = store.get_consensus_watch()
    for asset, (ov, tier, price, liquidity, closed) in observed.items():
        if ov.overlap < 2:
            continue
        key = f"{ov.condition_id}|{ov.outcome_index}"
        w = watch.get(key)
        if w is None:
            watch[key] = {
                "condition_id": ov.condition_id, "outcome_index": ov.outcome_index,
                "title": ov.title, "outcome": ov.outcome, "slug": ov.slug,
                "first_seen": now_iso, "first_price": price,
                "first_overlap": ov.overlap, "max_overlap": ov.overlap, "resolved": False,
            }
        else:
            w["max_overlap"] = max(w.get("max_overlap", 0), ov.overlap)
            if not w.get("first_price") and price:
                w["first_price"] = price
    # resolve any watched position whose market has closed
    for w in watch.values():
        if w.get("resolved"):
            continue
        m = market_map.get(w["condition_id"])
        if m is not None and m.closed:
            exit_price = m.resolved_price_for(w["outcome_index"])
            if exit_price is None:
                continue
            w.update(resolved=True, resolved_at=now_iso, exit_price=exit_price,
                     won=exit_price >= 0.5)
    store.set_consensus_watch(watch)


def _close(store, trade, exit_price, reason, summary, resolved_won):
    rpnl = strategy.realized_pnl(trade["shares"], trade["entry_price"], exit_price)
    store.update_trade(trade["id"], {
        "status": "CLOSED", "exit_at": _now_iso(),
        "exit_price": exit_price, "realized_pnl": rpnl,
        "marked_price": exit_price, "marked_at": _now_iso(), "unrealized_pnl": 0.0,
        "close_reason": reason, "resolved_won": resolved_won,
    })
    summary["closed"] += 1
