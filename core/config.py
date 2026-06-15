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
    "top_n", "leaderboard_window", "size_threshold",
    "tier_green_min", "tier_blue_min",
    "min_liquidity", "max_entry_price", "min_tier_to_trade",
    "stake_usd", "price_source", "control_respects_guardrails",
)

_TIER_RANK = {"none": 0, "blue": 1, "green": 2}


@dataclass
class Config:
    top_n: int = 5
    leaderboard_window: str = "MONTH"
    size_threshold: float = 1.0

    tier_green_min: int = 5
    tier_blue_min: int = 3

    min_liquidity: float = 1000.0
    max_entry_price: float = 0.90
    min_tier_to_trade: str = "blue"

    stake_usd: float = 100.0
    price_source: str = "midpoint"
    control_respects_guardrails: bool = True

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
