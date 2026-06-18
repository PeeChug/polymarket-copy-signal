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
from collections import Counter
from datetime import datetime, timedelta, timezone

from poller import strategy
from poller.alerts import notify_trade_opened
from core.config import load_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_between(a_iso, b_iso) -> float:
    """Absolute hours between two ISO timestamps; 1e9 (=> 'expired') if unparseable."""
    try:
        a = datetime.fromisoformat(str(a_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(b_iso).replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return abs((b - a).total_seconds()) / 3600.0
    except (ValueError, TypeError, AttributeError):
        return 1e9


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
    publishable = True
    try:
        outcome = _run(store, client, cfg, cid, summary, log) or {}
        status = outcome.get("status", "ok")
        err = outcome.get("reason")
        publishable = outcome.get("publishable", True)
    except Exception as e:  # record the failure on the cycle row, then re-raise
        status, err = "error", f"{type(e).__name__}: {e}"
        store.update_cycle(cid, {**summary, "status": status, "error": err,
                                 "duration_ms": int((time.time() - t0) * 1000)})
        raise

    summary.update({"status": status, "error": err,
                    "duration_ms": int((time.time() - t0) * 1000)})
    store.update_cycle(cid, summary)
    log(f"cycle #{cid} {status} in {summary['duration_ms']}ms: "
        f"{summary['n_observations']} obs, {summary['n_signals']} signals, "
        f"opened overlap={summary['opened_overlap']} control={summary['opened_control']}, "
        f"closed={summary['closed']}" + ("" if publishable else "  [NOT PUBLISHING]"))
    return {"cycle_id": cid, "publishable": publishable, **summary}


def _run(store, client, cfg, cid, summary, log):
    # ---- 1+2. build a cohort of top_n ELIGIBLE earners ---------------------
    # Screen a WIDER candidate pool (several leaderboard slices) down to top_n
    # traders that PASS the quality filter — so the cohort is N active, funded,
    # winning wallets, not "top N by profit of which only some qualify."
    seen, pool = set(), []
    # DAY/PNL catches brand-new hot traders before they climb the 7/30-day boards;
    # the rest give durable rank. Re-pulled every full scan (~10 min) so the cohort
    # tracks new top performers within minutes, not days.
    for win, order in (("DAY", "PNL"), ("WEEK", "PNL"), ("MONTH", "PNL"), ("ALL", "PNL"),
                       ("DAY", "VOL"), ("WEEK", "VOL"), ("MONTH", "VOL"), ("ALL", "VOL")):
        for e in client.leaderboard(window=win, limit=50, order=order):
            if e.wallet and e.wallet not in seen:
                seen.add(e.wallet)
                pool.append(e)
    if not pool:
        log("WARNING: leaderboards returned no entries — DEGRADED cycle, not publishing "
            "(keeps the last good data.json instead of overwriting it with nothing).")
        return {"status": "degraded", "publishable": False, "reason": "empty leaderboard"}
    pool.sort(key=lambda e: e.pnl, reverse=True)
    pool = pool[: getattr(cfg, "candidate_pool", 150) or 150]
    pool_wallets = [e.wallet for e in pool]

    # ---- 3. positions for the candidate pool (fetched concurrently) --------
    all_positions, failed_wallets = client.positions_many(pool_wallets, size_threshold=cfg.size_threshold)
    # Any failed fetch could have dropped an otherwise-eligible trader, so don't
    # trust 'abandoned' this cycle (preserves the no-phantom-exit guarantee).
    cohort_complete = not failed_wallets
    if failed_wallets:
        log(f"WARNING: {len(failed_wallets)}/{len(pool_wallets)} position fetches failed "
            f"this cycle -> NOT trusting 'abandoned' (closes suppressed until a clean cycle).")

    # ---- cohort QUALITY filter: keep the top_n ELIGIBLE earners ------------
    # Active, funded (>= min_holder_value on the table) and winning (>= min_holder_win_ratio
    # of open positions in profit). A #1-by-profit earner who has cashed out is noise.
    eligibility = {e.wallet: strategy.trader_eligibility(all_positions.get(e.wallet, []), cfg) for e in pool}

    # ---- cohort STABILITY (hysteresis) -------------------------------------
    # A wallet ENTERS the cohort the moment it qualifies, but once in it is
    # RETAINED for cfg.cohort_grace_hours through a transient dip (a win-ratio
    # wobble, a day off the DAY leaderboard) as long as it is still active +
    # funded — so the cohort SET stops churning 30<->50 every cycle. Only the
    # win-ratio bar is relaxed during grace; cashed-out/inactive wallets still
    # drop. Entry immediate, exit sticky.
    now = _now_iso()
    qual_wallets = {w for w, (ok, _, _) in eligibility.items() if ok}   # genuine qualifiers this cycle
    grace_h = getattr(cfg, "cohort_grace_hours", 0.0) or 0.0
    prev_state = store.get_cohort_state() if grace_h > 0 else {}
    retained = []
    if grace_h > 0:
        for e in pool:                       # only retain wallets still in the pool (positions in hand)
            w = e.wallet
            if w in qual_wallets:
                continue
            lastq = prev_state.get(w)
            if not lastq or _hours_between(lastq, now) > grace_h:
                continue
            st = eligibility[w][2]            # still active + funded? (relax only the win-ratio bar)
            if st.get("n_positions", 0) > 0 and st.get("total_value", 0.0) >= cfg.min_holder_value:
                eligibility[w] = (True, "retained (grace)", st)
                retained.append(w)

    eligible = [e for e in pool if eligibility[e.wallet][0]]   # qualifiers + retained, pnl-sorted
    cohort = eligible[: cfg.top_n]
    for i, e in enumerate(cohort, 1):   # re-rank 1..N by profit for clean display
        e.rank = i
    cohort_wallets = [e.wallet for e in cohort]
    by_wallet = {e.wallet: e for e in cohort}
    summary["n_traders"] = len(cohort)
    cohort_positions = {w: all_positions.get(w, []) for w in cohort_wallets}
    for e in cohort:
        for p in cohort_positions.get(e.wallet, []):   # annotate for overlap labelling
            p._username = e.username
            p._rank = e.rank

    # persist the membership clock: bump genuine qualifiers to now, and preserve
    # anyone else still within grace (so a one-cycle blip off the pool doesn't
    # reset their clock); everyone past grace is pruned.
    if grace_h > 0:
        new_state = {w: now for w in qual_wallets}
        for w, lastq in prev_state.items():
            if w not in new_state and _hours_between(lastq, now) <= grace_h:
                new_state[w] = lastq
        store.set_cohort_state(new_state)

    n_ret = len([w for w in cohort_wallets if w in set(retained)])
    log(f"cohort: screened {len(pool)} earners -> {len(cohort)}/{cfg.top_n} in cohort "
        f"({len(qual_wallets)} qualifying"
        + (f" + {n_ret} retained within {grace_h:.0f}h grace" if n_ret else "")
        + f"; bar >=${cfg.min_holder_value:,.0f} & >={cfg.min_holder_win_ratio:.0%} in profit)")

    # why the rest were screened out — so we know which knob to turn to grow the cohort
    def _rcat(r):
        if "on the table" in r: return "under_min_value"
        if "in profit" in r:    return "under_win_ratio"
        return r or "other"
    rej = Counter(_rcat(eligibility[e.wallet][1]) for e in pool if not eligibility[e.wallet][0])
    if rej:
        log(f"  ineligible breakdown: {dict(rej)}")
    if len(cohort) < cfg.top_n:
        log(f"NOTE: only {len(cohort)} eligible in a {len(pool)}-earner pool (wanted {cfg.top_n}) "
            f"— the leaderboard caps each slice at 50, so the ceiling is the {len(pool)}-wallet "
            f"universe; relax min_holder_win_ratio/min_holder_value to admit more.")
    if not cohort:
        return {"status": "degraded", "publishable": False, "reason": "no eligible traders in pool"}

    # ---- proportional agreement bar ----------------------------------------
    # Size the tiers to the ACTUAL eligible cohort so the buy threshold isn't
    # mis-sized when the leaderboard yields fewer than top_n that clear the
    # quality bar (30 eligible -> blue>=3/green>=6, not the 50-tuned 5/10).
    eff_blue, eff_green = cfg.proportional_tiers(len(cohort))
    if (eff_blue, eff_green) != (cfg.tier_blue_min, cfg.tier_green_min):
        log(f"  proportional tiers for {len(cohort)} eligible: "
            f"blue>={eff_blue} green>={eff_green} (floor {cfg.tier_blue_min}/{cfg.tier_green_min})")
    cfg.tier_blue_min, cfg.tier_green_min = eff_blue, eff_green

    store.insert_leaderboard([{
        "cycle_id": cid, "window": cfg.leaderboard_window, "rank": e.rank,
        "wallet": e.wallet, "username": e.username, "pnl": e.pnl, "volume": e.volume,
        "in_cohort": True,
    } for e in cohort])

    # The control benchmark FOLLOWS THE WHOLE COHORT — it copies EVERY position any
    # cohort wallet holds (overlap >= 1), not a single leader. That makes it the
    # direct answer to "does the CONSENSUS filter (>= blue bar agree) beat blindly
    # copying everything the top traders do?". It opens off the same observation
    # snapshot the consensus uses (below), so no separate per-trade feed is needed;
    # the only thing snapshots can't see is a position opened AND closed inside a
    # single poll (a rare intra-cycle round-trip), which we can layer on later.

    # ---- 4. overlaps (over the eligible cohort) ----------------------------
    overlaps = strategy.compute_overlaps(cohort_positions)
    cohort_assets = set(overlaps.keys())
    log(f"{sum(len(v) for v in cohort_positions.values())} cohort positions "
        f"-> {len(overlaps)} distinct (market,outcome) pairs")

    # per-trader snapshot for the dashboard (the eligible cohort)
    store.set_traders(_build_traders(cohort, cohort_positions, overlaps, eligibility))

    # accurate full agreement counts (independent of any dashboard cap on observations)
    ovs = [o.overlap for o in overlaps.values()]
    store.set_agreement({
        "cohort_size": len(cohort), "positions": len(ovs),
        "ge2": sum(1 for x in ovs if x >= 2), "ge3": sum(1 for x in ovs if x >= 3),
        "ge5": sum(1 for x in ovs if x >= 5), "max_overlap": max(ovs, default=0),
        "histogram": {str(k): v for k, v in sorted(Counter(ovs).items())},
        # effective (possibly proportional) tiers actually used this cycle, so the
        # dashboard shows the true buy bar instead of the stored floor.
        "tier_blue_min": cfg.tier_blue_min, "tier_green_min": cfg.tier_green_min,
    })

    # existing open trades — needed below (and to know what to price)
    open_index = {(t["strategy"], t["condition_id"], t["outcome_index"]): t
                  for t in store.open_trades()}

    # RE-ENTRY COOLDOWN: markets we STOPPED out of recently. Don't re-buy them even
    # if the cohort still holds — that's the falling-knife spiral (a stuck cohort in
    # a collapsing live game drags us in again and again, stopping out each time).
    # Keyed by OUR stop events, not the market end_date (unreliable for live sports).
    cooldown_h = getattr(cfg, "reentry_cooldown_hours", 0.0) or 0.0
    cooldown_keys = set()
    if cooldown_h > 0:
        since = (datetime.now(timezone.utc) - timedelta(hours=cooldown_h)).isoformat()
        cooldown_keys = store.recently_stopped(since)
        if cooldown_keys:
            log(f"re-entry cooldown: {len(cooldown_keys)} market(s) stopped within "
                f"{cooldown_h:.0f}h are blocked from re-entry")

    # ---- BATCH-FETCH markets + marks once for everything we touch ----------
    # include unresolved consensus-watch markets so we can detect their resolution
    watched_conds = {w["condition_id"] for w in store.get_consensus_watch().values() if not w.get("resolved")}
    all_conditions = ({ov.condition_id for ov in overlaps.values()}
                      | {t["condition_id"] for t in open_index.values()} | watched_conds)
    all_assets = set(overlaps.keys()) | {t["asset"] for t in open_index.values()}
    market_map = client.markets(all_conditions)
    # Realistic fills: you BUY at the ask (entry) and SELL at the bid (mark/exit),
    # so the paper P&L pays the real spread instead of the optimistic midpoint.
    realistic = (cfg.price_source == "realistic")
    if realistic:
        entry_map = client.marks(all_assets, source="buy")    # ask — what you'd pay to enter
        mark_map = client.marks(all_assets, source="sell")    # bid — what you'd get to exit
    else:
        entry_map = mark_map = client.marks(all_assets, source=cfg.price_source)
    log(f"batched {len(market_map)}/{len(all_conditions)} markets, "
        f"{len(mark_map)}/{len(all_assets)} live prices ({'ask/bid' if realistic else cfg.price_source})")

    def get_market(condition_id):
        return market_map.get(condition_id)

    def _price_from(pmap, asset, condition_id, outcome_index, fallback=None):
        p = pmap.get(asset)
        if p is not None:
            return p
        m = market_map.get(condition_id)  # resolved markets have no live book -> use payout
        if m is not None:
            rp = m.resolved_price_for(outcome_index)
            if rp is not None:
                return rp
        return fallback

    def get_entry(asset, condition_id, outcome_index, fallback=None):
        return _price_from(entry_map, asset, condition_id, outcome_index, fallback)

    def get_mark(asset, condition_id, outcome_index, fallback=None):
        return _price_from(mark_map, asset, condition_id, outcome_index, fallback)

    # ---- 5a. observations: LOG EVERYTHING ----------------------------------
    obs_rows = []
    observed = {}  # asset -> (tier, price, liquidity, closed) for the open step
    for asset, ov in overlaps.items():
        m = get_market(ov.condition_id)
        price = get_entry(asset, ov.condition_id, ov.outcome_index, fallback=ov.fallback_price)
        liquidity = m.liquidity if m else None
        closed = bool(m.closed) if m else False
        active = bool(m.active) if m else None
        tier = cfg.tier_for(ov.overlap)
        observed[asset] = (ov, tier, price, liquidity, closed)
        obs_rows.append({
            "cycle_id": cid, "condition_id": ov.condition_id, "asset": asset,
            "outcome": ov.outcome, "outcome_index": ov.outcome_index,
            "title": ov.title, "slug": ov.slug,
            "overlap": ov.overlap, "participants": ov.participants, "tier": tier,
            "holder_wallets": ov.wallets[:20], "holder_usernames": ov.usernames[:20],
            "holder_sizes": [round(s, 2) for s in ov.sizes[:20]],
            "holder_avg_prices": [round(a, 4) for a in ov.avg_prices[:20]],
            "notional": round(ov.notional, 2),
            "price": price, "liquidity": liquidity,
            "market_closed": closed, "market_active": active, "end_date": ov.end_date,
        })
    store.insert_observations(obs_rows)
    summary["n_observations"] = len(obs_rows)

    def try_open(strat, asset, ov, tier, price, liquidity, closed):
        g = strategy.passes_guardrails(tier=tier, price=price, liquidity=liquidity,
                                       market_closed=closed, cfg=cfg, strategy=strat,
                                       end_date=ov.end_date)
        if not g.ok:
            return
        key = (strat, ov.condition_id, ov.outcome_index)
        if key in open_index:
            return  # already holding this (strategy, market, outcome)
        if key in cooldown_keys:
            return  # we stopped out of this market recently — don't re-buy the falling knife
        shares = strategy.shares_for(cfg.stake_usd, price)
        if shares <= 0:
            return
        payload = {
            "strategy": strat, "condition_id": ov.condition_id, "asset": asset,
            "outcome": ov.outcome, "outcome_index": ov.outcome_index, "title": ov.title,
            "status": "OPEN", "entry_at": _now_iso(), "entry_cycle_id": cid,
            "entry_price": price, "stake_usd": cfg.stake_usd, "shares": shares,
            "tier_at_entry": tier, "overlap_at_entry": ov.overlap, "holders_at_entry": ov.wallets,
            "end_date": ov.end_date,                       # for the pre-resolution time-stop
            "marked_price": price, "marked_at": _now_iso(), "unrealized_pnl": 0.0,
            "peak_price": price,                           # high-water mark for the trailing stop
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

    # ---- 5b. open overlap signals (one side per market — handle contested) -
    # When the cohort is split on a market (both Yes and No clear the tier),
    # opening both is a guaranteed wash. Pick the strongest single side instead.
    pnl_by_wallet = {e.wallet: e.pnl for e in cohort}
    candidates = []
    for asset, (ov, tier, price, liquidity, closed) in observed.items():
        if cfg.tier_meets_minimum(tier):
            summary["n_signals"] += 1
            candidates.append((asset, ov, tier, price, liquidity, closed))
    by_market = {}
    for it in candidates:
        by_market.setdefault(it[1].condition_id, []).append(it)
    policy = getattr(cfg, "contested_policy", "dominant")
    for cond, items in by_market.items():
        if len(items) == 1 or policy == "both":
            picks = items
        elif policy == "skip":
            log(f"  SKIP contested {items[0][1].title[:38]!r} "
                f"({', '.join(f'{i[1].outcome}={i[1].overlap}' for i in items)})")
            continue
        else:  # 'dominant': strongest side by overlap; ties broken by holders' 30d P&L
            best = max(items, key=lambda it: (it[1].overlap,
                       sum(pnl_by_wallet.get(w, 0.0) for w in (it[1].wallets or []))))
            picks = [best]
            log(f"  CONTESTED {best[1].title[:34]!r}: "
                f"{', '.join(f'{i[1].outcome}={i[1].overlap}' for i in items)} "
                f"-> take {best[1].outcome}")
        for asset, ov, tier, price, liquidity, closed in picks:
            try_open("overlap", asset, ov, tier, price, liquidity, closed)

    # ---- 5c. open control signals — FOLLOW ALL: copy EVERY position any cohort
    # wallet holds (overlap >= 1), the "follow everything the top traders do"
    # benchmark vs the consensus filter. (Tradeability guardrails still apply.)
    for asset, (ov, tier, price, liquidity, closed) in observed.items():
        try_open("control", asset, ov, tier, price, liquidity, closed)

    # ---- 6. mark-to-market + close every open trade ------------------------
    for t in store.open_trades():
        m = get_market(t["condition_id"])
        mark = get_mark(t["asset"], t["condition_id"], t["outcome_index"],
                        fallback=t.get("marked_price"))
        resolved = bool(m and m.closed)
        # both strategies are held iff ANY cohort wallet still holds the asset:
        # consensus exits when the overlap collapses, control when the LAST holder leaves.
        held = t["asset"] in cohort_assets

        if resolved:
            payout = m.resolved_price_for(t["outcome_index"])
            if payout is not None:
                exit_price, won = payout, (payout >= 0.5)       # real 0/1 settlement
            else:
                # market closed but Gamma gave no payout -> outcome UNKNOWN; close at the
                # best available price but record won=None (never guess from price>=0.5).
                exit_price, won = (mark if mark is not None else t.get("marked_price") or 0.0), None
            _close(store, t, exit_price, "resolved", summary, resolved_won=won)
            log(f"  CLOSE [{t['strategy']}] resolved {t['title'][:36]!r} @ {exit_price:.3f}"
                + ("" if won is None else (" WON" if won else " lost")))
        elif not held and cohort_complete:
            # only trust 'abandoned' when we actually saw the WHOLE cohort this cycle
            exit_price = mark if mark is not None else (t.get("marked_price") or 0.0)
            reason = "cohort_abandoned" if t["strategy"] == "overlap" else "all_exited"
            _close(store, t, exit_price, reason, summary, resolved_won=None)
            log(f"  CLOSE [{t['strategy']}] {reason} {t['title'][:32]!r} @ {exit_price:.3f}")
        else:
            if mark is None:
                continue
            # HOLDER-DRIVEN EXIT (consensus): sell as soon as the cohort starts
            # SELLING — any net holder has left since we entered. Mirrors the
            # control's leader-exit (its win engine) and beats waiting for agreement
            # to decay past the buy bar (which exits late + tiny). Only act on a
            # complete cohort snapshot (a partial fetch understates overlap).
            if cohort_complete and t["strategy"] == "overlap":
                cur_ov = overlaps[t["asset"]].overlap if t["asset"] in overlaps else 0
                entry_ov = t.get("overlap_at_entry") or 0
                buy_bar = cfg.tier_green_min if cfg.min_tier_to_trade == "green" else cfg.tier_blue_min
                left = (cur_ov < entry_ov) if entry_ov else (cur_ov < buy_bar)
                if left:
                    _close(store, t, mark, "holder_exited", summary, resolved_won=None)
                    log(f"  CLOSE [{t['strategy']}] holder_exited {t['title'][:30]!r} "
                        f"holders {entry_ov}->{cur_ov} @ {mark:.3f}")
                    continue
            # PRICE/TIME EXITS (overlap-only): a WIDE % stop + price floor, plus
            # take-profit, a trailing stop, and a pre-resolution time-stop. The
            # holder-exit above is the primary risk control; these catch a fast
            # gap the cohort hasn't reacted to yet and bank gains before they
            # round-trip. SAME logic runs every minute in the Worker (worker.js
            # priceExit) so a position can't crater 50% between 10-min scans.
            peak = max(t.get("peak_price") or t["entry_price"] or 0.0, mark)
            reason, exit_price = strategy.price_exit(
                entry=t["entry_price"], mark=mark, peak=peak,
                end_date=t.get("end_date"), cfg=cfg, strategy=t["strategy"])
            if reason:
                _close(store, t, exit_price, reason, summary, resolved_won=None)
                ret = (mark - (t["entry_price"] or 0)) / t["entry_price"] if t["entry_price"] else 0.0
                log(f"  CLOSE [{t['strategy']}] {reason} {t['title'][:30]!r} "
                    f"@ {exit_price:.3f} ({ret*100:+.0f}%)")
                continue
            # CONTROL (follow-all, ~580 positions): its live P&L is supplied every
            # minute by the Worker fast-mark (marks.json, overlaid on the dashboard),
            # so we DON'T write a per-trade mark here — ~580 DB writes/cycle would blow
            # the time budget and the runs pile up / time out. Overlap is only a handful.
            if t["strategy"] != "overlap":
                continue
            upnl = strategy.unrealized_pnl(t["shares"], t["entry_price"], mark)
            store.update_trade(t["id"], {
                "marked_price": mark, "marked_at": _now_iso(), "unrealized_pnl": upnl,
                "peak_price": peak,
            })

    # ---- accumulate trackers for the dashboard (consensus hit-rate + sparklines)
    _update_trackers(store, cohort, observed, market_map, _now_iso())

    # ---- publishability: never overwrite the public data.json with a badly
    # degraded snapshot. A mostly-failed cohort fetch understates overlap, so
    # flag the cycle degraded and keep the last good payload.
    ok_frac = (len(pool_wallets) - len(failed_wallets)) / len(pool_wallets) if pool_wallets else 0.0
    if ok_frac < 0.5:
        return {"status": "degraded", "publishable": False,
                "reason": f"low fetch coverage {ok_frac:.0%} ({len(failed_wallets)} fetch failures)"}
    return {"status": "ok", "publishable": True, "reason": None}


def _build_traders(cohort, cohort_positions, overlaps, eligibility=None, max_positions=8):
    """One row per cohort trader: leaderboard stats + their open positions,
    each position annotated with how many of the cohort also hold it. Each
    trader is flagged `eligible` (counts toward consensus) with a reason."""
    eligibility = eligibility or {}
    traders = []
    for e in cohort:
        positions = cohort_positions.get(e.wallet, [])
        elig = eligibility.get(e.wallet, (True, "", {}))
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
                "participants": ov.participants if ov else 1,
            })
        rows.sort(key=lambda r: r["current_value"], reverse=True)
        traders.append({
            "rank": e.rank, "wallet": e.wallet, "username": e.username,
            "pnl": e.pnl, "volume": e.volume, "profile_image": e.profile_image,
            "x_username": e.x_username, "verified": e.verified,
            "n_positions": len(positions), "total_value": total_value, "open_pnl": open_pnl,
            "n_winning": sum(1 for p in positions if (p.cash_pnl or 0) > 0),  # open positions in profit
            "eligible": bool(elig[0]),            # counts toward consensus?
            "ineligible_reason": elig[1] or "",   # why not (for the dashboard)
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
                "first_overlap": ov.overlap, "max_overlap": ov.overlap,
                "cur_overlap": ov.overlap, "prev_overlap": ov.overlap, "momentum": 0,
                "last_seen": now_iso, "resolved": False,
                # for smart-money + backtest: who held it (union over time) and tier at peak
                "holders": sorted(set(ov.wallets[:50])), "tier": tier,
            }
        else:
            w["prev_overlap"] = w.get("cur_overlap", ov.overlap)
            w["cur_overlap"] = ov.overlap
            w["momentum"] = ov.overlap - w["prev_overlap"]   # change since last time seen
            if ov.overlap >= w.get("max_overlap", 0):
                w["tier"] = tier                              # tier at the peak agreement
            w["max_overlap"] = max(w.get("max_overlap", 0), ov.overlap)
            w["last_seen"] = now_iso
            if not w.get("first_price") and price:
                w["first_price"] = price
            holders = set(w.get("holders") or [])
            holders.update(ov.wallets[:50])
            w["holders"] = sorted(holders)[:50]
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
