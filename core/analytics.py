"""
Pure aggregation helpers for the dashboard. No Streamlit, no DB — they take
plain lists of row dicts (as returned by the store) and return summary dicts,
so they can be unit-tested without any UI or network.

P&L conventions match poller/strategy.py:
  realized P&L is booked on closed trades; unrealized is the live mark on open
  trades; ROI is P&L over dollars staked.
"""

from __future__ import annotations

from collections import Counter


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def _metrics(trades: list[dict]) -> dict:
    open_t = [t for t in trades if t.get("status") == "OPEN"]
    closed_t = [t for t in trades if t.get("status") == "CLOSED"]

    realized = sum(_num(t.get("realized_pnl")) for t in closed_t)
    unrealized = sum(_num(t.get("unrealized_pnl")) for t in open_t)
    staked_closed = sum(_num(t.get("stake_usd")) for t in closed_t)
    staked_open = sum(_num(t.get("stake_usd")) for t in open_t)
    staked_all = staked_closed + staked_open
    wins = sum(1 for t in closed_t if _num(t.get("realized_pnl")) > 0)
    # "on table" = current market value of the OPEN positions (shares × live mark),
    # i.e. what's riding on the table right now — vs "staked" (the cost basis).
    on_table = sum(_num(t.get("shares")) * _num(t.get("marked_price"), default=_num(t.get("entry_price")))
                   for t in open_t)

    return {
        "open_count": len(open_t),
        "closed_count": len(closed_t),
        "wins": wins,
        "losses": len(closed_t) - wins,
        "win_rate": (wins / len(closed_t)) if closed_t else None,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "net_pnl": realized + unrealized,
        "staked_closed": staked_closed,
        "staked_open": staked_open,
        "staked_all": staked_all,
        "on_table": on_table,
        "roi_realized": (realized / staked_closed) if staked_closed else None,
        "roi_total": ((realized + unrealized) / staked_all) if staked_all else None,
    }


def strategy_performance(trades: list[dict]) -> dict[str, dict]:
    """Side-by-side metrics for 'overlap' vs 'control'."""
    out = {}
    for strat in ("overlap", "control"):
        out[strat] = _metrics([t for t in trades if t.get("strategy") == strat])
    return out


def tier_breakdown(trades: list[dict]) -> dict[str, dict]:
    """Overlap-strategy metrics split by the tier the trade entered at."""
    overlap = [t for t in trades if t.get("strategy") == "overlap"]
    out = {}
    for tier in ("green", "blue"):
        out[tier] = _metrics([t for t in overlap if t.get("tier_at_entry") == tier])
    return out


def open_positions(trades: list[dict]) -> list[dict]:
    """Open trades enriched with a value column, newest first."""
    rows = []
    for t in trades:
        if t.get("status") != "OPEN":
            continue
        shares = _num(t.get("shares"))
        mark = _num(t.get("marked_price"), default=_num(t.get("entry_price")))
        rows.append({
            **t,
            "mark_value": shares * mark,
            "unrealized_pnl": _num(t.get("unrealized_pnl")),
        })
    rows.sort(key=lambda r: str(r.get("entry_at") or ""), reverse=True)
    return rows


def closed_positions(trades: list[dict], limit: int = 100) -> list[dict]:
    """Closed trades (the realized track record), newest exit first."""
    rows = [dict(t) for t in trades if t.get("status") == "CLOSED"]
    rows.sort(key=lambda r: str(r.get("exit_at") or ""), reverse=True)
    return rows[:limit]


def latest_signal_per_market(observations: list[dict]) -> list[dict]:
    """Keep the most recent observation per asset, sorted by overlap desc."""
    latest: dict[str, dict] = {}
    for o in observations:
        key = o.get("asset")
        prev = latest.get(key)
        if prev is None or str(o.get("observed_at") or "") >= str(prev.get("observed_at") or ""):
            latest[key] = o
    rows = list(latest.values())
    rows.sort(key=lambda r: (r.get("overlap") or 0), reverse=True)
    return rows


def agreement_summary(observations, cohort_size=None) -> dict:
    """
    How much the top earners agree this cycle — the core 'correlation' readout.
    Counts distinct (market, outcome) positions by how many of the cohort hold them.
    """
    def ov(o):
        return int(o.get("overlap") or 0)
    return {
        "cohort_size": cohort_size,
        "positions": len(observations),                       # distinct positions held by anyone in the cohort
        "ge2": sum(1 for o in observations if ov(o) >= 2),     # held by 2+ earners
        "ge3": sum(1 for o in observations if ov(o) >= 3),     # moderate agreement
        "ge5": sum(1 for o in observations if ov(o) >= 5),     # strong agreement
        "max_overlap": max((ov(o) for o in observations), default=0),
        "histogram": {str(k): v for k, v in sorted(Counter(ov(o) for o in observations).items())},
    }


def calibration(watch) -> dict:
    """
    Consensus hit-rate — the hypothesis test. From the watch of every position
    ever held by >=2 of the cohort, bucketed by the MAX agreement it reached:
    among the ones that have since resolved, how often did the outcome win, and
    what's the average return of buying $1 at first sighting and holding to close.
    """
    entries = list(watch.values()) if isinstance(watch, dict) else (watch or [])
    out = {}
    for k, thresh in (("ge2", 2), ("ge3", 3), ("ge5", 5)):
        pool = [w for w in entries if (w.get("max_overlap") or 0) >= thresh]
        resolved = [w for w in pool if w.get("resolved")]
        wins = sum(1 for w in resolved if w.get("won"))
        rets = [(w["exit_price"] - w["first_price"]) / w["first_price"]
                for w in resolved if w.get("first_price") and w.get("exit_price") is not None]
        out[k] = {
            "tracking": len(pool),
            "resolved": len(resolved),
            "wins": wins,
            "win_rate": (wins / len(resolved)) if resolved else None,
            "avg_return": (sum(rets) / len(rets)) if rets else None,
        }
    return out


def _watch_entries(watch):
    return list(watch.values()) if isinstance(watch, dict) else (watch or [])


def trader_scores(watch) -> dict[str, dict]:
    """Per-trader 'sharp' record on CONSENSUS positions (held by >=2 of the cohort).

    For every watched position, credit each holder wallet; among the ones that
    have resolved, how often did the trader's side win and what was the average
    buy-at-first-sighting return. Keyed by wallet so the dashboard can merge it
    into traders[]. Accrues as markets resolve (empty until the first resolution).
    """
    agg: dict[str, dict] = {}
    for w in _watch_entries(watch):
        resolved = bool(w.get("resolved"))
        won = bool(w.get("won"))
        fp, xp = w.get("first_price"), w.get("exit_price")
        ret = ((xp - fp) / fp) if (resolved and fp and xp is not None) else None
        for wallet in (w.get("holders") or []):
            a = agg.setdefault(wallet, {"held": 0, "resolved": 0, "wins": 0, "ret_sum": 0.0, "ret_n": 0})
            a["held"] += 1
            if resolved:
                a["resolved"] += 1
                a["wins"] += 1 if won else 0
                if ret is not None:
                    a["ret_sum"] += ret
                    a["ret_n"] += 1
    out = {}
    for wallet, a in agg.items():
        out[wallet] = {
            "held": a["held"], "resolved": a["resolved"], "wins": a["wins"],
            "win_rate": (a["wins"] / a["resolved"]) if a["resolved"] else None,
            "avg_return": (a["ret_sum"] / a["ret_n"]) if a["ret_n"] else None,
        }
    return out


def backtest(watch) -> dict:
    """Replay the core hypothesis: buy $1 of every consensus position at first
    sighting and hold to resolution. Reports per agreement threshold (>=2/3/5)
    and per tier (green/blue), plus a cumulative-return equity curve ordered by
    resolution. Real numbers for whatever has resolved; grows over time.
    """
    entries = _watch_entries(watch)

    def stat(pool):
        resolved = [w for w in pool if w.get("resolved")]
        wins = sum(1 for w in resolved if w.get("won"))
        rets = [(w["exit_price"] - w["first_price"]) / w["first_price"]
                for w in resolved if w.get("first_price") and w.get("exit_price") is not None]
        return {
            "tracking": len(pool), "resolved": len(resolved), "wins": wins,
            "losses": len(resolved) - wins,
            "win_rate": (wins / len(resolved)) if resolved else None,
            "avg_return": (sum(rets) / len(rets)) if rets else None,
            "total_return": round(sum(rets), 4) if rets else 0.0,
        }

    strategies = {k: stat([w for w in entries if (w.get("max_overlap") or 0) >= th])
                  for k, th in (("ge2", 2), ("ge3", 3), ("ge5", 5))}
    tiers = {tier: stat([w for w in entries if w.get("tier") == tier]) for tier in ("green", "blue")}

    resolved = sorted(
        [w for w in entries if w.get("resolved") and w.get("first_price") and w.get("exit_price") is not None],
        key=lambda w: str(w.get("resolved_at") or ""))
    curve, cum = [], 0.0
    for w in resolved:
        cum += (w["exit_price"] - w["first_price"]) / w["first_price"]
        curve.append({"t": w.get("resolved_at"), "cum": round(cum, 4), "won": bool(w.get("won")),
                      "title": w.get("title"), "overlap": w.get("max_overlap"), "tier": w.get("tier")})
    return {"strategies": strategies, "tiers": tiers, "curve": curve,
            "resolved_total": len(resolved), "tracking_total": len(entries)}


def dashboard_payload(trades, observations, leaderboard, config_rows, traders=None,
                      agreement=None, meta=None) -> dict:
    """
    Everything the static GitHub-Pages dashboard needs, precomputed server-side
    (in the poller) so the page is pure render-from-JSON.
    """
    meta = meta or {}
    signals = latest_signal_per_market(observations)
    cohort_size = len(leaderboard) if leaderboard else (
        (config_rows[0] or {}).get("top_n") if config_rows else None)
    return {
        "generated_at": meta.get("generated_at"),
        "last_cycle": meta.get("last_cycle"),
        "traders": traders or [],
        # the headline: positions the top earners AGREE on (held by 2+), strongest first
        "consensus": [s for s in signals if (s.get("overlap") or 0) >= 2],
        # prefer the accurate full counts computed by the poller (independent of any cap)
        "agreement": agreement or agreement_summary(observations, cohort_size),
        "performance": strategy_performance(trades),
        "tiers": tier_breakdown(trades),
        "open_positions": open_positions(trades),
        "closed_positions": closed_positions(trades),
        "signals": signals,
        "config": config_rows[0] if config_rows else None,
        "config_history": config_rows,
        "leaderboard": leaderboard,
        "counts": {
            "trades": len(trades),
            "open": sum(1 for t in trades if t.get("status") == "OPEN"),
            "closed": sum(1 for t in trades if t.get("status") == "CLOSED"),
            "observations_last_cycle": len(observations),
        },
    }
