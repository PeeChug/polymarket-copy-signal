"""
Configuration model + forward-only loader.

The poller always reads the NEWEST row from `config_history`. The dashboard
"saves" settings by INSERTing a brand-new row (never updating an old one), so
every change applies only to future cycles and never rewrites past trades.

If the table is empty (first ever run), we seed it once from config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict, fields
from typing import Optional

# Fields the dashboard is allowed to edit / that live in config_history.
_CONFIG_FIELDS = (
    "top_n", "candidate_pool", "leaderboard_window", "size_threshold", "poll_interval_minutes",
    "tier_green_min", "tier_blue_min", "tier_green_frac", "tier_blue_frac",
    "min_liquidity", "min_entry_price", "max_entry_price", "min_resolve_hours", "min_tier_to_trade",
    "stake_usd", "price_source", "control_respects_guardrails",
    "stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trailing_arm_pct",
    "time_stop_minutes", "fast_exit_slippage_pct", "contested_policy",
    "min_holder_value", "min_holder_win_ratio", "cohort_grace_hours",
)

_TIER_RANK = {"none": 0, "blue": 1, "green": 2}


@dataclass
class Config:
    top_n: int = 5
    # screen this many top earners (across all 8 window×order leaderboard slices) down
    # to top_n that pass the quality filter — so the cohort is top_n ELIGIBLE wallets.
    # Bigger pool = more wallets clear the 65%/$10k bar (the leaderboard caps each
    # slice at 50, so this is the real lever for cohort size).
    candidate_pool: int = 400
    leaderboard_window: str = "MONTH"
    size_threshold: float = 1.0
    poll_interval_minutes: int = 15

    # Tiers can be ABSOLUTE (tier_*_min) or PROPORTIONAL to the live eligible cohort
    # (tier_*_frac > 0 wins). Proportional keeps the agreement bar correctly sized
    # whether the cohort comes in at 30 or 50 — anchored so a 50-cohort reproduces
    # the original 5/10 (0.10 -> 5, 0.20 -> 10). tier_*_min then acts as the floor.
    tier_green_min: int = 5
    tier_blue_min: int = 3
    tier_green_frac: float = 0.20    # green overlap >= round(this * eligible cohort size); 0 = use the absolute
    tier_blue_frac: float = 0.10     # blue  overlap >= round(this * eligible cohort size); 0 = use the absolute

    min_liquidity: float = 1000.0
    # skip deep longshots: at a price like 0.001 the bid/ask spread is ~100% of the
    # price (one tick) and the book is empty, so a "win" is untradeable noise. Also
    # used as the price floor for the stop (exit if a held position falls below it).
    min_entry_price: float = 0.05
    max_entry_price: float = 0.85
    # skip markets that resolve within this many hours — live/same-day bets (esp.
    # sports) resolve to 0/1 in hours, so copying them adds fat-tail variance, not
    # consensus edge. 0 = off.
    min_resolve_hours: float = 24.0
    min_tier_to_trade: str = "blue"

    stake_usd: float = 100.0
    # 'realistic' = enter at the ask / exit at the bid (pays the real spread, so the
    # paper P&L isn't optimistic); 'midpoint' = mid of bid/ask; 'buy' = best ask both ways
    price_source: str = "realistic"
    control_respects_guardrails: bool = True

    # exit: close a trade if it falls this fraction below entry (0 = off, 0.25 = -25%, the default)
    stop_loss_pct: float = 0.25
    # --- fast, price-based exits (run every minute in the Worker, not just the 10-min
    #     scan) so a position can't crater 50% between scans. All overlap-only; the
    #     control benchmark stays naive (exits only on leader-abandon / resolution). ---
    take_profit_pct: float = 0.0        # bank a defined gain (>= +X%); 0 = off (let winners run)
    trailing_stop_pct: float = 0.15     # once armed, exit if price gives back this much from its peak; 0 = off
    trailing_arm_pct: float = 0.20      # only arm the trailing stop after the trade is up at least this much
    time_stop_minutes: float = 30.0     # force-exit this many minutes before resolution (short-fuse safety); 0 = off
    fast_exit_slippage_pct: float = 0.02  # extra haircut on a PANIC sell (stop/trailing) — thin book on the way down
    # signal-decay exit is rule-based (no knob): close a held position the moment its
    # agreement falls back below the tier floor we require to open (the "buy bar").
    # when the cohort is split on a market (both sides held): 'both' = trade both
    # sides (default — top traders often run box spreads / hedges, so this is a real
    # signal worth testing; the unique index still blocks identical dupes), 'dominant'
    # = only the stronger side (ties -> holders' 30d P&L), 'skip' = don't trade it.
    contested_policy: str = "both"

    # cohort QUALITY: a top earner must have real skin in the game and a winning
    # current book to count toward consensus — filters out cashed-out whales (a #1
    # earner with $10 on the table) and coin-flippers. Active is implied (value>0).
    min_holder_value: float = 10000.0      # min USD in OPEN positions to count toward overlap
    min_holder_win_ratio: float = 0.5      # min fraction of their open positions currently in profit

    # cohort STABILITY (hysteresis): once a wallet qualifies, keep it in the cohort
    # for this many hours through transient dips (a win-ratio wobble or a day off the
    # DAY leaderboard) as long as it's still active + funded — so the cohort SET stops
    # churning 30<->50 cycle to cycle. Entry is immediate; only EXIT is sticky. 0 = off.
    cohort_grace_hours: float = 48.0

    # metadata (set when loaded from the DB; not user-editable)
    id: Optional[int] = None
    source: str = "default-seed"

    # -- derived logic ------------------------------------------------------ #
    def tier_for(self, overlap: int) -> str:
        """Map an overlap count to a tier. green takes precedence over blue."""
        if overlap >= self.tier_green_min:
            return "green"
        if overlap >= self.tier_blue_min:
            return "blue"
        return "none"

    def tier_meets_minimum(self, tier: str) -> bool:
        return _TIER_RANK.get(tier, 0) >= _TIER_RANK.get(self.min_tier_to_trade, 1)

    def proportional_tiers(self, cohort_n: int) -> tuple:
        """Effective (blue_min, green_min) for a cohort of `cohort_n` eligible wallets.
        Proportional when the fracs are set (>0), else the absolute mins. Blue is
        floored at 2 (need >=2 holders to call it consensus) and green is kept
        strictly above blue, so the tiers never collapse on a tiny cohort."""
        bf = getattr(self, "tier_blue_frac", 0.0) or 0.0
        gf = getattr(self, "tier_green_frac", 0.0) or 0.0
        blue = max(2, round(bf * cohort_n)) if bf else self.tier_blue_min
        green = max(blue + 1, round(gf * cohort_n)) if gf else self.tier_green_min
        return blue, green

    def editable_dict(self) -> dict:
        d = asdict(self)
        return {k: d[k] for k in _CONFIG_FIELDS}

    @classmethod
    def from_row(cls, row: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in row.items() if k in known}
        return cls(**kwargs)


def defaults_from_yaml(path: str) -> Config:
    """Read seed defaults from config.yaml (only used to seed an empty DB)."""
    try:
        import yaml  # lazy import so this module stays usable without PyYAML
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        return Config.from_row(data)
    except FileNotFoundError:
        return Config()


def default_yaml_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


def load_config(store, seed_path: Optional[str] = None) -> Config:
    """
    Return the live config: the newest config_history row, seeding the table
    from config.yaml the first time if it is empty.
    """
    row = store.latest_config()
    if row is not None:
        return Config.from_row(row)
    seed = defaults_from_yaml(seed_path or default_yaml_path())
    payload = seed.editable_dict()
    payload["source"] = "default-seed"
    payload["note"] = "auto-seeded from config.yaml on first run"
    inserted = store.insert_config(payload)
    return Config.from_row(inserted or payload)


def sync_yaml_config(store, path: Optional[str] = None) -> bool:
    """
    Forward-only settings editor for the file-based deployment: if config.yaml
    differs from the newest stored config (or none exists yet), append a NEW
    config row. Editing config.yaml and committing therefore changes only future
    cycles and never rewrites past trades. Returns True if a new row was written.
    """
    path = path or default_yaml_path()
    yaml_cfg = defaults_from_yaml(path)
    latest = store.latest_config()
    if latest is not None and Config.from_row(latest).editable_dict() == yaml_cfg.editable_dict():
        return False
    payload = yaml_cfg.editable_dict()
    payload["source"] = "config.yaml"
    payload["note"] = "synced from config.yaml" if latest else "initial from config.yaml"
    store.insert_config(payload)
    return True
