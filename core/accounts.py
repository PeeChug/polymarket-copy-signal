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
# Three WALLETS at different starting capital, to test how budget size affects the
# SAME strategy. Each wallet: buying a position takes the stake OUT of the wallet,
# selling returns the FULL proceeds IN (compounds with wins, shrinks with losses,
# can only ever spend what's in it — a signal it can't cover is skipped). The
# dashboard Settings tune the shared policy (sizing, slippage, fee, green-only,
# min agreement); the three differ ONLY in starting capital.
WALLET_CAPITALS = [500.0, 1000.0, 3000.0]


def _money_name(v) -> str:
    return "$" + format(int(round(v)), ",d")


def wallet_configs_from(settings) -> list:
    """Build the wallet account configs (one per WALLET_CAPITALS) from flat user
    settings (a kv blob the dashboard saves). Missing/invalid fields fall back to
    sensible defaults; the shared policy is applied at every capital level."""
    s = settings or {}

    def g(key, default):
        try:
            return float(s[key])
        except (KeyError, TypeError, ValueError):
            return default

    tiers = ["green"] if s.get("green_only") else ["green", "blue"]
    # FIXED-dollar stake (not % of equity) so starting capital actually matters:
    # a $500 wallet funds far fewer $100 trades than a $3,000 wallet.
    stake, max_exp = g("stake", 100.0), g("max_exposure", 0.80)
    # slippage models MARKET IMPACT (your order moving the book) — SEPARATE from the
    # bid/ask spread, which is already paid via the realistic ask-in/bid-out entry &
    # exit prices. A $100 order on a >=$1k-liquidity market barely moves it, so 1%
    # was far too high — it ate nearly the whole ~2% gross consensus margin and made
    # bigger wallets look WORSE (more marginal near-breakeven trades). 0.5% is a
    # realistic, still-conservative default; raise it in Settings to stress-test.
    slip, fee, min_ov = g("slippage_pct", 0.005), g("fee_pct", 0.0), int(g("min_overlap", 0))
    return [{
        "name": _money_name(cap), "starting_capital": cap,
        "filter": {"tiers": list(tiers), "min_overlap": min_ov},
        "sizing": {"mode": "fixed", "value": stake, "max_exposure": max_exp, "min_trade": 5.0},
        "reinvest": {"withdraw_pct": 0.0},   # full compound — proceeds return to the wallet
        "costs": {"slippage_pct": slip, "impact_coef": 0.0, "fee_pct": fee},
    } for cap in WALLET_CAPITALS]


DEFAULT_ACCOUNTS = wallet_configs_from(None)

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
    """Chronological open/close events for the overlap trades.

    Ordering within a single timestamp (cycle): CLOSES first (free up cash),
    then OPENS ranked by agreement strength DESC — so when the wallet is tight
    it funds the STRONGEST signals first and skips the weaker ones (selectivity),
    rather than whatever happened to be earliest."""
    evs = []
    for t in trades:
        if (t.get("strategy") or "overlap") != "overlap":
            continue
        ov = _num(t.get("overlap_at_entry"))
        evs.append((str(t.get("entry_at") or ""), 1, -ov, "open", t))   # kind 1 = open
        if (t.get("status") == "CLOSED") and t.get("exit_at"):
            evs.append((str(t.get("exit_at")), 0, 0.0, "close", t))     # kind 0 = close (first)
    evs.sort(key=lambda e: (e[0], e[1], e[2]))
    return evs


def simulate(trades: list, cfg: dict) -> dict:
    """Replay the consensus trades through one budgeted account. Pure."""
    start = _num(cfg.get("starting_capital"), 1000.0)
    tiers = set((cfg.get("filter") or {}).get("tiers") or ["green", "blue"])
    min_overlap = _num((cfg.get("filter") or {}).get("min_overlap"), 0)   # selectivity bar
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

    for ts, _k, _s, kind, t in _events(trades):
        asset = t.get("asset")
        if kind == "open":
            if t.get("tier_at_entry") not in tiers or not asset or asset in pos:
                continue
            if _num(t.get("overlap_at_entry")) < min_overlap:   # below the selectivity bar
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
        "wallet": round(cash, 2),                    # spendable balance — buys leave it, sells return to it
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
