"""Unit tests for the budgeted-account simulator (core/accounts.py).

All pure/offline: synthetic overlap trades replayed through an account with
fixed-dollar sizing (deterministic) to pin the cash-flow, reinvestment,
slippage, filter and budget-skip behaviour.
"""

from core import accounts


def T(asset, entry, exit=None, tier="green", mark=None, ov=5,
      ea="2026-06-01T00:00:00", xa="2026-06-02T00:00:00"):
    d = {"strategy": "overlap", "asset": asset, "title": asset, "tier_at_entry": tier,
         "entry_price": entry, "entry_at": ea, "overlap_at_entry": ov,
         "status": "CLOSED" if exit is not None else "OPEN"}
    if exit is not None:
        d["exit_price"] = exit; d["exit_at"] = xa
    if mark is not None:
        d["marked_price"] = mark
    return d


def CFG(start=1000, size=100, withdraw=0.0, slip=0.0, tiers=("green", "blue"),
        max_exp=1.0, fee=0.0):
    return {"name": "t", "starting_capital": start, "filter": {"tiers": list(tiers)},
            "sizing": {"mode": "fixed", "value": size, "max_exposure": max_exp, "min_trade": 1},
            "reinvest": {"withdraw_pct": withdraw},
            "costs": {"slippage_pct": slip, "fee_pct": fee}}


def near(a, b, tol=0.5):
    return abs(a - b) <= tol


def test_winning_trade_no_costs():
    r = accounts.simulate([T("a", 0.50, 1.00)], CFG())
    assert r["closed_count"] == 1 and r["wins"] == 1
    assert near(r["realized_pnl"], 100) and near(r["cash"], 1100)
    assert near(r["equity"], 1100) and near(r["total"], 1100)
    assert near(r["return_pct"], 0.10, 0.01)
    assert r["withdrawn"] == 0


def test_us_fee_model_drags_buy_side_only():
    # Polymarket US taker fee (price-dependent, buy-side only) must cut the win vs the
    # fee-free global venue. At p=0.50 the fee buys ~2.5% fewer shares -> ~$5 off a 2x.
    trades = [T("a", 0.50, 1.00)]
    free = accounts.simulate(trades, CFG())
    us = accounts.simulate(trades, {
        "name": "us", "starting_capital": 1000, "filter": {"tiers": ["green", "blue"]},
        "sizing": {"mode": "fixed", "value": 100, "max_exposure": 1.0, "min_trade": 1},
        "reinvest": {"withdraw_pct": 0.0},
        "costs": {"slippage_pct": 0.0, "fee_model": "us", "us_fee_coef": 0.05}})
    assert us["realized_pnl"] < free["realized_pnl"]     # US fee drags it
    assert near(us["realized_pnl"], 95, 0.5)             # +95 vs +100 fee-free


def test_loss_reduces_cash():
    r = accounts.simulate([T("a", 0.50, 0.00)], CFG())
    assert near(r["realized_pnl"], -100) and near(r["cash"], 900) and near(r["total"], 900)
    assert r["wins"] == 0 and r["withdrawn"] == 0


def test_withdraw_skims_profit():
    r = accounts.simulate([T("a", 0.50, 1.00)], CFG(withdraw=0.50))
    assert near(r["withdrawn"], 50)        # half the $100 profit taken off the table
    assert near(r["cash"], 1050)           # the rest stays/compounds
    assert near(r["total"], 1100) and near(r["return_pct"], 0.10, 0.01)


def test_slippage_reduces_pnl():
    clean = accounts.simulate([T("a", 0.50, 1.00)], CFG(slip=0.0))["realized_pnl"]
    slipped = accounts.simulate([T("a", 0.50, 1.00)], CFG(slip=0.10))["realized_pnl"]
    assert slipped < clean                 # worse fills both ways cut the profit
    assert slipped < 80                     # ~+63.6 vs +100


def test_fee_reduces_pnl():
    clean = accounts.simulate([T("a", 0.50, 1.00)], CFG(fee=0.0))["realized_pnl"]
    feed = accounts.simulate([T("a", 0.50, 1.00)], CFG(fee=0.02))["realized_pnl"]
    assert feed < clean


def test_budget_skips_when_cash_runs_out():
    # $250 budget, $100 each, three never-closing opens => only 2 fit, 1 skipped
    trades = [T("a", 0.5), T("b", 0.5), T("c", 0.5)]
    r = accounts.simulate(trades, CFG(start=250, size=100))
    assert r["open_count"] == 2 and r["skipped"] == 1
    assert near(r["cash"], 50)


def test_selectivity_funds_strongest_first():
    # same cycle, wallet only fits one $100 trade => the higher-agreement one wins
    trades = [T("weak", 0.5, ov=6, ea="2026-06-01"), T("strong", 0.5, ov=12, ea="2026-06-01")]
    r = accounts.simulate(trades, CFG(start=160, size=100))
    assert r["open_count"] == 1 and r["skipped"] == 1
    assert r["open_positions"][0]["title"] == "strong"


def test_min_overlap_bar_skips_weak():
    trades = [T("a", 0.5, ov=6), T("b", 0.5, ov=11)]
    cfg = CFG(); cfg["filter"]["min_overlap"] = 8
    r = accounts.simulate(trades, cfg)
    assert r["open_count"] == 1 and r["open_positions"][0]["title"] == "b"


def test_tier_filter_green_only():
    trades = [T("a", 0.5, 1.0, tier="green"), T("b", 0.5, 1.0, tier="blue")]
    r = accounts.simulate(trades, CFG(tiers=("green",)))
    assert r["closed_count"] == 1          # the blue trade is filtered out


def test_open_position_marked_to_live():
    r = accounts.simulate([T("a", 0.50, mark=0.80)], CFG())   # open, marked up
    assert r["open_count"] == 1 and r["closed_count"] == 0
    assert r["unrealized_pnl"] > 0 and r["deployed"] > 0
    assert near(r["cash"], 900)            # $100 deployed


def test_max_exposure_cap():
    # 70% cap on $1000 => at most $700 deployed across $100 trades => 7 opens
    trades = [T(str(i), 0.5) for i in range(10)]
    r = accounts.simulate(trades, CFG(max_exp=0.70))
    assert r["open_count"] == 7 and r["skipped"] == 3


def test_equity_frac_compounds():
    # 10%-of-equity sizing: a win grows equity, so the next trade is larger
    cfg = {"name": "t", "starting_capital": 1000, "filter": {"tiers": ["green"]},
           "sizing": {"mode": "equity_frac", "value": 0.10, "max_exposure": 1.0, "min_trade": 1},
           "reinvest": {"withdraw_pct": 0.0}, "costs": {}}
    # one win that resolves before the next opens
    trades = [T("a", 0.50, 1.00, ea="2026-06-01", xa="2026-06-02"),
              T("b", 0.50, ea="2026-06-03")]
    r = accounts.simulate(trades, cfg)
    # after +$100 win equity ~1100, 10% => ~$110 second trade => cash ~ 1100-110
    assert r["open_count"] == 1 and near(r["cash"], 990, 5)


def test_simulate_all_defaults():
    rs = accounts.simulate_all([])
    assert [r["name"] for r in rs] == ["$500", "$1,000", "$3,000"]
    assert [r["starting_capital"] for r in rs] == [500.0, 1000.0, 3000.0]
    assert all(r["equity"] == r["starting_capital"] for r in rs)   # empty => untouched


def test_wallet_configs_from_settings():
    cfgs = accounts.wallet_configs_from({"stake": 50, "max_exposure": 0.5,
                                         "slippage_pct": 0.02, "fee_pct": 0.01,
                                         "min_overlap": 7, "green_only": True})
    assert [c["starting_capital"] for c in cfgs] == [500.0, 1000.0, 3000.0]  # 3 capitals
    c = cfgs[0]
    assert c["sizing"]["mode"] == "fixed" and c["sizing"]["value"] == 50 and c["sizing"]["max_exposure"] == 0.5
    assert c["costs"]["slippage_pct"] == 0.02 and c["costs"]["fee_pct"] == 0.01
    assert c["filter"]["tiers"] == ["green"] and c["filter"]["min_overlap"] == 7
    # bad/missing settings fall back to defaults ($100 fixed stake, green+blue)
    d = accounts.wallet_configs_from({})[1]
    assert d["sizing"]["value"] == 100.0 and d["filter"]["tiers"] == ["green", "blue"]


def test_wallet_compounds_full_proceeds():
    # buy debits the wallet; sell returns the FULL proceeds (it compounds)
    r = accounts.simulate([T("a", 0.50, 1.00)], CFG(withdraw=0.0))
    assert near(r["wallet"], 1100)          # $100 stake out, $200 proceeds back in
    assert near(r["realized_pnl"], 100) and r["withdrawn"] == 0
    # a second winning trade compounds on the bigger wallet (10%-of-equity sizing)
    cfg = {"name": "t", "starting_capital": 1000, "filter": {"tiers": ["green"]},
           "sizing": {"mode": "equity_frac", "value": 1.0, "max_exposure": 1.0, "min_trade": 1},
           "reinvest": {"withdraw_pct": 0.0}, "costs": {}}
    r2 = accounts.simulate([T("a", 0.50, 0.60, ea="2026-06-01", xa="2026-06-02"),
                            T("b", 0.50, 0.60, ea="2026-06-03", xa="2026-06-04")], cfg)
    # +20% then +20% on the full wallet => ~1.44x, clearly compounding past +40%
    assert r2["wallet"] > 1400
