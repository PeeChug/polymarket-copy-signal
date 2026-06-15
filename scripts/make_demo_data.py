"""
Generate dashboard/demo_data.json — a realistic, fully synthetic dataset so the
dashboard can be previewed with ZERO setup (no Supabase, no live API).

It runs several cycles of the real engine against a scripted fake client and an
in-memory store, producing a mix of green/blue/control trades, wins/losses,
open positions, and logged observations. Re-run with:

    python scripts/make_demo_data.py
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import MemoryStore
from poller.engine import run_cycle


def L(rank, wallet, name, pnl):
    return types.SimpleNamespace(rank=rank, wallet=wallet, username=name, pnl=pnl, volume=pnl * 4,
                                 profile_image="", x_username="", verified=False)


def P(asset, cond, title, outcome="Yes", idx=0, cur=0.5):
    return types.SimpleNamespace(asset=asset, condition_id=cond, outcome=outcome, outcome_index=idx,
                                 size=5000.0, avg_price=0.3, cur_price=cur, current_value=2500.0,
                                 redeemable=False, title=title, slug=cond, end_date="2026-07-01T00:00:00Z",
                                 cash_pnl=(cur - 0.3) * 5000.0, percent_pnl=(cur - 0.3) / 0.3)


def M(cond, closed=False, liquidity=50000.0, resolved=None):
    m = types.SimpleNamespace(condition_id=cond, closed=closed, active=not closed, liquidity=liquidity)
    m.resolved_price_for = lambda idx, _r=resolved: _r
    return m


class FakeClient:
    def __init__(self):
        self.lb, self.positions_by_wallet, self.markets_map, self.marks_map = [], {}, {}, {}

    def leaderboard(self, window="MONTH", limit=5, order="PNL"):
        return self.lb[:limit]

    def positions(self, wallet, size_threshold=1.0, only_open=True):
        return list(self.positions_by_wallet.get(wallet, []))

    def positions_many(self, wallets, size_threshold=1.0, only_open=True):
        return {w: self.positions(w) for w in wallets}

    def market(self, condition_id):
        return self.markets_map.get(condition_id)

    def markets(self, condition_ids, chunk=40):
        return {c: self.markets_map[c] for c in condition_ids if c in self.markets_map}

    def marks(self, token_ids, source="midpoint"):
        return {t: self.marks_map[t] for t in token_ids if t in self.marks_map}

    def mark_price(self, token_id, source="midpoint", market=None, outcome_index=0, fallback=None):
        return self.marks_map.get(token_id, fallback)


TITLES = {
    "mGREEN": "Will the Fed cut rates in July 2026?",
    "mBLUE": "Will Team USA win Olympic gold in basketball?",
    "mBLUE2": "Will it rain in NYC on July 4, 2026?",
    "mLEADER": "Will BTC close above $150k this month?",
    "mNONE": "Will the new iPhone ship before October?",
}


# wallet ids must be identical in the leaderboard and the holdings book
WALLETS = [
    "0x99aea8f9a64d0142b6b66a4b9d02a2211d45386f",
    "0xf8831548531d56ad6a4331493243c447a827cd1f",
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae",
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563",
    "0x8cb4ca0e0a8f6b9a4d3e2f1c0b9a8d7e6f5c4b3a",
]
BOOK = {
    WALLETS[0]: ["mGREEN", "mBLUE", "mLEADER"],
    WALLETS[1]: ["mGREEN", "mBLUE", "mBLUE2"],
    WALLETS[2]: ["mGREEN", "mBLUE", "mBLUE2"],
    WALLETS[3]: ["mGREEN", "mBLUE2"],
    WALLETS[4]: ["mGREEN", "mNONE"],
}


def positions(prices, drop=()):
    """Build holdings (stable each cycle) minus any resolved markets in `drop`."""
    out = {}
    for w, assets in BOOK.items():
        out[w] = [P(a, a, TITLES[a], cur=prices.get(a, 0.5)) for a in assets if a not in drop]
    return out


def main():
    store = MemoryStore()
    store.insert_config({
        "top_n": 5, "leaderboard_window": "MONTH", "size_threshold": 1,
        "tier_green_min": 5, "tier_blue_min": 3,
        "min_liquidity": 1000, "max_entry_price": 0.90, "min_tier_to_trade": "blue",
        "stake_usd": 100, "price_source": "midpoint", "control_respects_guardrails": True,
        "source": "default-seed", "note": "demo",
    })
    c = FakeClient()
    names = ["WhaleKing", "Inaccuratestake", "Latina", "QuantQueen", "EdgeFinder"]
    pnls = [5_022_101, 3_947_667, 3_720_357, 2_810_004, 2_100_550]
    c.lb = [L(i + 1, WALLETS[i], names[i], pnls[i]) for i in range(5)]

    # cycle 1 — entries
    c.markets_map = {k: M(k, liquidity=80000) for k in TITLES}
    c.marks_map = {"mGREEN": 0.30, "mBLUE": 0.45, "mBLUE2": 0.60, "mLEADER": 0.50, "mNONE": 0.10}
    c.positions_by_wallet = positions(c.marks_map)
    run_cycle(store, c, log=lambda *a, **k: None)

    # cycle 2 — marks drift
    c.marks_map = {"mGREEN": 0.55, "mBLUE": 0.50, "mBLUE2": 0.40, "mLEADER": 0.52, "mNONE": 0.12}
    c.positions_by_wallet = positions(c.marks_map)
    run_cycle(store, c, log=lambda *a, **k: None)

    # cycle 3 — mBLUE2 resolves NO (loss for the Yes holders)
    c.markets_map["mBLUE2"] = M("mBLUE2", closed=True, resolved=0.0)
    c.marks_map = {"mGREEN": 0.70, "mBLUE": 0.58, "mBLUE2": 0.0, "mLEADER": 0.55, "mNONE": 0.11}
    c.positions_by_wallet = positions(c.marks, drop=("mBLUE2",))
    run_cycle(store, c, log=lambda *a, **k: None)

    # cycle 4 — mGREEN resolves YES (win)
    c.markets_map["mGREEN"] = M("mGREEN", closed=True, resolved=1.0)
    c.marks_map = {"mGREEN": 1.0, "mBLUE": 0.62, "mLEADER": 0.58, "mNONE": 0.13}
    c.positions_by_wallet = positions(c.marks, drop=("mBLUE2", "mGREEN"))
    run_cycle(store, c, log=lambda *a, **k: None)

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dashboard", "demo_data.json")
    with open(out_path, "w") as fh:
        json.dump({
            "_demo": True,
            "trades": store.all_trades(),
            "observations": store.latest_observations(),
            "leaderboard": store.latest_leaderboard(),
            "config_rows": store.config_history(50),
        }, fh, indent=2, default=str)
    t = store.all_trades()
    print(f"wrote {out_path}: {len(t)} trades "
          f"({sum(1 for x in t if x['status']=='CLOSED')} closed), "
          f"{len(store.latest_observations())} observations in last cycle")


if __name__ == "__main__":
    main()
