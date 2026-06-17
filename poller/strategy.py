"""
Pure strategy + paper-trade math. No network, no database — so it can be
unit-tested deterministically and reasoned about in isolation.

Position identity throughout = the CLOB `asset` token id, which uniquely
identifies a (market, outcome) pair. Overlap = the number of DISTINCT cohort
wallets holding the same asset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _resolves_within(end_date, hours: float) -> bool:
    """True if the market resolves within `hours` from now (so we should skip it).
    Off when hours<=0 or end_date is missing/unparseable (can't tell => don't skip)."""
    if not end_date or hours is None or hours <= 0:
        return False
    try:
        s = end_date if isinstance(end_date, str) else end_date.isoformat()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc) + timedelta(hours=float(hours))
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass
class Overlap:
    """Aggregated view of one position across the cohort, for one cycle."""
    asset: str
    condition_id: str
    outcome: str
    outcome_index: int
    title: str
    slug: str
    end_date: object = None
    wallets: list = field(default_factory=list)     # distinct holder wallets (this outcome)
    usernames: list = field(default_factory=list)
    ranks: list = field(default_factory=list)
    cur_prices: list = field(default_factory=list)  # holders' reported prices (fallback only)
    sizes: list = field(default_factory=list)        # holders' share counts on this side
    values: list = field(default_factory=list)       # holders' current $ value on this side
    avg_prices: list = field(default_factory=list)   # holders' average entry price
    participants: int = 0   # distinct cohort wallets holding ANY outcome of this market

    @property
    def overlap(self) -> int:
        return len(self.wallets)

    @property
    def notional(self) -> float:
        """Total $ the cohort holds on this side — dollar conviction, not headcount."""
        return float(sum(v for v in self.values if v))

    @property
    def fallback_price(self):
        """Median-ish fallback if no live/Gamma price is available."""
        vals = [p for p in self.cur_prices if p and p > 0]
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]


def compute_overlaps(cohort_positions: dict[str, list]) -> dict[str, Overlap]:
    """
    cohort_positions: {wallet -> [Position, ...]} (open positions only).
    Returns {asset -> Overlap}. A wallet is counted at most once per asset.
    """
    by_asset: dict[str, Overlap] = {}
    market_holders: dict[str, set] = {}   # condition_id -> distinct wallets holding ANY outcome
    for wallet, positions in cohort_positions.items():
        seen_for_wallet = set()
        for p in positions:
            if not p.asset or p.asset in seen_for_wallet:
                continue
            seen_for_wallet.add(p.asset)
            market_holders.setdefault(p.condition_id, set()).add(wallet)
            ov = by_asset.get(p.asset)
            if ov is None:
                ov = Overlap(
                    asset=p.asset, condition_id=p.condition_id, outcome=p.outcome,
                    outcome_index=p.outcome_index, title=p.title, slug=p.slug,
                    end_date=p.end_date,
                )
                by_asset[p.asset] = ov
            ov.wallets.append(wallet)
            ov.usernames.append(getattr(p, "_username", "") or "")
            ov.ranks.append(getattr(p, "_rank", 0) or 0)
            ov.cur_prices.append(p.cur_price)
            ov.sizes.append(getattr(p, "size", 0.0) or 0.0)
            ov.values.append(getattr(p, "current_value", 0.0) or 0.0)
            ov.avg_prices.append(getattr(p, "avg_price", 0.0) or 0.0)
    # participants = distinct cohort wallets with ANY position in the market
    for ov in by_asset.values():
        ov.participants = len(market_holders.get(ov.condition_id, ()))
    return by_asset


# --------------------------------------------------------------------------- #
# Cohort quality — which top earners actually count toward a signal
# --------------------------------------------------------------------------- #
def trader_eligibility(positions, cfg):
    """Is a cohort member good enough to COUNT toward consensus?

    Filters the universe to traders with real skin in the game and a winning
    current book, so a #1-by-30d-profit earner who has cashed out (a few dollars
    on the table) or a coin-flipper (most open bets red) doesn't move the signal.
    Returns (ok: bool, reason: str, stats: dict)."""
    n = len(positions)
    total_value = sum((getattr(p, "current_value", 0.0) or 0.0) for p in positions)
    n_winning = sum(1 for p in positions if (getattr(p, "cash_pnl", 0.0) or 0.0) > 0)
    win_ratio = (n_winning / n) if n else 0.0
    min_val = getattr(cfg, "min_holder_value", 0.0) or 0.0
    min_wr = getattr(cfg, "min_holder_win_ratio", 0.0) or 0.0
    stats = {"n_positions": n, "total_value": total_value,
             "n_winning": n_winning, "win_ratio": win_ratio}
    if n == 0:
        return False, "inactive", stats
    if total_value < min_val:
        return False, f"<${min_val:,.0f} on the table", stats
    if win_ratio < min_wr:
        return False, f"{win_ratio:.0%} in profit (<{min_wr:.0%})", stats
    return True, "", stats


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
@dataclass
class GuardrailResult:
    ok: bool
    reason: str = ""


def passes_guardrails(*, tier: str, price, liquidity, market_closed: bool,
                      cfg, strategy: str, end_date=None) -> GuardrailResult:
    """
    Decide whether an observed position may become a paper trade THIS cycle.
    'overlap' strategy enforces the tier minimum; 'control' does not (it has no
    tier) but optionally shares the tradeability guardrails so the only thing
    being compared is the SELECTION rule.
    """
    if market_closed:
        return GuardrailResult(False, "market_closed")
    if price is None:
        return GuardrailResult(False, "no_price")

    if strategy == "overlap":
        if not cfg.tier_meets_minimum(tier):
            return GuardrailResult(False, f"tier<{cfg.min_tier_to_trade}")
        check_tradeability = True
    else:  # control
        check_tradeability = bool(cfg.control_respects_guardrails)

    if check_tradeability:
        if liquidity is not None and liquidity < cfg.min_liquidity:
            return GuardrailResult(False, "low_liquidity")
        if price > cfg.max_entry_price:
            return GuardrailResult(False, "price_too_high")
        if price < getattr(cfg, "min_entry_price", 0.0):
            return GuardrailResult(False, "price_too_low")   # deep longshot: spread eats any "win"
        if _resolves_within(end_date, getattr(cfg, "min_resolve_hours", 24.0)):
            return GuardrailResult(False, "resolves_too_soon")
    return GuardrailResult(True, "ok")


# --------------------------------------------------------------------------- #
# Paper-trade P&L for a binary outcome share
#
#   A fixed $ stake buys `shares = stake / entry_price` shares at the locked
#   entry price. Each share is worth the current price (0..1). On resolution a
#   winning share is worth 1.0, a losing share 0.0.
#       value(price)     = shares * price
#       unrealized P&L   = shares * (mark  - entry)   = value(mark)  - stake
#       realized   P&L   = shares * (exit  - entry)   = value(exit)  - stake
# --------------------------------------------------------------------------- #
def shares_for(stake: float, entry_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return stake / entry_price


def unrealized_pnl(shares: float, entry_price: float, mark_price: float) -> float:
    return shares * (mark_price - entry_price)


def realized_pnl(shares: float, entry_price: float, exit_price: float) -> float:
    return shares * (exit_price - entry_price)


def roi(pnl: float, stake: float) -> float:
    return (pnl / stake) if stake else 0.0
