"""
Budgeted paper-trading accounts — a *realistic-bankroll* layer on top of the
consensus signal.

The existing `overlap` paper trades answer "does the signal have edge?" with an
unlimited bankroll (a fresh $100 on every trigger). That's good for ROI/win-rate
but the dollar P&L is fictional — a real account has FINITE cash. This module
replays the very same consensus trades through one or more accounts that each
have a starting budget, size positions from available cash, pay slippage + fees,
SKIP signals when cash runs out, and split realized profit between a withdrawn
bucket and reinvested (compounding) capital.

Design: a PURE function of (trades, config) — no I/O, no persisted state. It's
recomputed from the trade log every publish, so it always reflects the latest
prices and any policy change, and it's deterministic + unit-testable. The engine
and DB are untouched.

Realism modelled here (the gaps called out in the accuracy review):
  * finite cash — open debits cash, close credits it; no cash => signal skipped
  * slippage — each fill is haircut vs the recorded touch price (our order moves
    the book); flat by default, with an optional size/liquidity impact term
  * fees — a per-notional fee on both sides (Polymarket US charges one; global ~0)
  * reinvestment — skim X% of each realized profit to a locked bucket, the rest
    compounds the working bankroll
Still NOT modelled (documented, not hidden): partial fills, queue priority,
intra-trade marking for sizing (we size off book equity), poll-cadence latency.
"""

from __future__ import annotations

# Starter accounts (tunable later via config). Differ by risk profile so the
# policy effects are visible side by side. Prices are 0..1 probabilities, so
# slippage_pct is a fraction of the price (1% = fill 1% worse than the touch).
DEFAULT_ACCOUNTS = [
    {
        "name": "Balanced", "starting_capital": 1000.0,
        "filter": {"tiers": ["green", "blue"]},
        "sizing": {"mode": "equity_frac", "value": 0.05, "max_exposure": 0.70, "min_trade": 5.0},
        "reinvest": {"withdraw_pct": 0.50},        # take 50% of each profit, compound the rest
        "costs": {"slippage_pct": 0.01, "impact_coef": 0.0, "fee_pct": 0.0},
    },
    {
        "name": "Conservative", "starting_capital": 500.0,
        "filter": {"tiers": ["green"]},
        "sizing": {"mode": "equity_frac", "value": 0.03, "max_exposure": 0.50, "min_trade": 5.0},
        "reinvest": {"withdraw_pct": 0.50},
        "costs": {"slippage_pct": 0.015, "impact_coef": 0.0, "fee_pct": 0.0},
    },
    {
        "name": "Compounder", "starting_capital": 2000.0,
        "filter": {"tiers": ["green", "blue"]},
        "sizing": {"mode": "equity_frac", "value": 0.06, "max_exposure": 0.85, "min_trade": 5.0},
        "reinvest": {"withdraw_pct": 0.0},         # reinvest everything (full compound)
        "costs": {"slippage_pct": 0.01, "impact_coef": 0.0, "fee_pct": 0.0},
    },
]

_MAX_SLIP = 0.10   # cap modelled slippage at 10% of price, however large the order


def _num(v, default=0.0) -> float:
    try:
        return default if v is None else float(v)
    except (ValueError, TypeError):
        return default


def _slippage(costs: dict, size: float, liquidity) -> float:
    base = _num(costs.get("slippage_pct"), 0.0)
    impact = _num(costs.get("impact_coef"), 0.0)
    liq = _num(liquidity, 0.0)
    extra = (impact * (size / liq)) if (impact and liq > 0) else 0.0
    return max(0.0, min(_MAX_SLIP, base + extra))


def _size_for(sizing: dict, cash: float, book_equity: float) -> float:
    mode = sizing.get("mode", "equity_frac")
    val = _num(sizing.get("value"), 0.05)
    if mode == "fixed":
        return val
    if mode == "cash_frac":
        return cash * val
    return book_equity * val            # equity_frac (default) — compounds with the bankroll


def _events(trades: list) -> list:
    """Chronological open/close events for the overlap trades. Each open sorts
    before a same-timestamp close so cash from a close isn't reused too early."""
    evs = []
    for t in trades:
        if (t.get("strategy") or "overlap") != "overlap":
            continue
        ea = str(t.get("entry_at") or "")
        evs.append((ea, 0, "open", t))
        if (t.get("status") == "CLOSED") and t.get("exit_at"):
            evs.append((str(t.get("exit_at")), 1, "close", t))
    evs.sort(key=lambda e: (e[0], e[1]))
    return evs


def simulate(trades: list, cfg: dict) -> dict:
    """Replay the consensus trades through one budgeted account. Pure."""
    start = _num(cfg.get("starting_capital"), 1000.0)
    tiers = set((cfg.get("filter") or {}).get("tiers") or ["green", "blue"])
    sizing = cfg.get("sizing") or {}
    max_exp = _num(sizing.get("max_exposure"), 1.0) or 1.0
    min_trade = _num(sizing.get("min_trade"), 0.0)
    withdraw_pct = _num((cfg.get("reinvest") or {}).get("withdraw_pct"), 0.0)
    costs = cfg.get("costs") or {}
    fee_pct = _num(costs.get("fee_pct"), 0.0)

    cash = start
    pos: dict[str, dict] = {}            # asset -> {shares, cost, title, tier}
    realized = withdrawn = 0.0
    opened = closed = wins = skipped = 0
    peak = start
    max_dd = 0.0
    curve = [{"t": None, "equity": start, "total": start}]

    def book_equity():
        return cash + sum(p["cost"] for p in pos.values())

    for ts, _o, kind, t in _events(trades):
        asset = t.get("asset")
        if kind == "open":
            if t.get("tier_at_entry") not in tiers or not asset or asset in pos:
                continue
            entry = _num(t.get("entry_price"))
            if entry <= 0:
                continue
            be = book_equity()
            size = _size_for(sizing, cash, be)
            deployed = sum(p["cost"] for p in pos.values())
            # SKIP when the budget can't take it (this is the realism vs $100-always)
            if size < min_trade or size > cash or (deployed + size) > be * max_exp:
                skipped += 1
                continue
            fill = entry * (1 + _slippage(costs, size, t.get("liquidity")))
            invested = size * (1 - fee_pct)         # fee taken off the stake
            pos[asset] = {"shares": invested / fill, "cost": size,
                          "title": t.get("title"), "tier": t.get("tier_at_entry")}
            cash -= size
            opened += 1
        else:  # close
            p = pos.pop(asset, None)
            if not p:
                continue
            exitp = _num(t.get("exit_price"), _num(t.get("marked_price")))
            gross = p["shares"] * exitp * (1 - _slippage(costs, p["cost"], t.get("liquidity")))
            proceeds = gross * (1 - fee_pct)
            cash += proceeds
            pnl = proceeds - p["cost"]
            realized += pnl
            if pnl > 0:
                wins += 1
                skim = pnl * withdraw_pct           # take a cut of profit off the table
                cash -= skim
                withdrawn += skim
            closed += 1
            eq = book_equity()
            peak = max(peak, eq + withdrawn)
            if peak > 0:
                max_dd = max(max_dd, (peak - (eq + withdrawn)) / peak)
            curve.append({"t": ts, "equity": round(eq, 2), "total": round(eq + withdrawn, 2)})

    # mark whatever's still open at the latest price, net of an exit haircut
    unrealized = deployed_mv = 0.0
    open_rows = []
    for asset, p in pos.items():
        # find the live mark for this still-open trade
        mark = next((_num(t.get("marked_price"), _num(t.get("entry_price")))
                     for t in trades if t.get("asset") == asset and t.get("status") == "OPEN"), None)
        if mark is None:
            mark = p["cost"] / max(p["shares"], 1e-9)
        mv = p["shares"] * mark * (1 - _slippage(costs, p["cost"], None))
        deployed_mv += mv
        unrealized += mv - p["cost"]
        open_rows.append({"title": p["title"], "tier": p["tier"], "cost": round(p["cost"], 2),
                          "value": round(mv, 2), "pnl": round(mv - p["cost"], 2)})

    equity = cash + deployed_mv
    total = equity + withdrawn
    return {
        "name": cfg.get("name"), "starting_capital": start,
        "cash": round(cash, 2), "deployed": round(deployed_mv, 2),
        "equity": round(equity, 2), "withdrawn": round(withdrawn, 2), "total": round(total, 2),
        "realized_pnl": round(realized, 2), "unrealized_pnl": round(unrealized, 2),
        "return_pct": (total - start) / start if start else None,
        "open_count": len(pos), "closed_count": closed, "wins": wins,
        "win_rate": (wins / closed) if closed else None,
        "skipped": skipped,                          # capacity: signals the budget couldn't take
        "utilization": (deployed_mv / equity) if equity else 0.0,
        "max_drawdown": round(max_dd, 4),
        "curve": curve[-400:],
        "open_positions": sorted(open_rows, key=lambda r: r["value"], reverse=True)[:50],
        "config": {"filter": cfg.get("filter"), "sizing": sizing,
                   "reinvest": cfg.get("reinvest"), "costs": costs},
    }


def simulate_all(trades: list, configs: list | None = None) -> list:
    return [simulate(trades or [], c) for c in (configs or DEFAULT_ACCOUNTS)]
