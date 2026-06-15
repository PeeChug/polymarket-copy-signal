"""
Pure aggregation helpers for the dashboard. No Streamlit, no DB — they take
plain lists of row dicts (as returned by the store) and return summary dicts,
so they can be unit-tested without any UI or network.

P&L conventions match poller/strategy.py:
  realized P&L is booked on closed trades; unrealized is the live mark on open
  trades; ROI is P&L over dollars staked.
"""

from __future__ import annotations


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


def dashboard_payload(trades, observations, leaderboard, config_rows, meta=None) -> dict:
    """
    Everything the static GitHub-Pages dashboard needs, precomputed server-side
    (in the poller) so the page is pure render-from-JSON.
    """
    meta = meta or {}
    return {
        "generated_at": meta.get("generated_at"),
        "last_cycle": meta.get("last_cycle"),
        "performance": strategy_performance(trades),
        "tiers": tier_breakdown(trades),
        "open_positions": open_positions(trades),
        "signals": latest_signal_per_market(observations),
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
