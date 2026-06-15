"""
Pure strategy + paper-trade math. No network, no database — so it can be
unit-tested deterministically and reasoned about in isolation.

Position identity throughout = the CLOB `asset` token id, which uniquely
identifies a (market, outcome) pair. Overlap = the number of DISTINCT cohort
wallets holding the same asset.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    wallets: list = field(default_factory=list)     # distinct holder wallets
    usernames: list = field(default_factory=list)
    ranks: list = field(default_factory=list)
    cur_prices: list = field(default_factory=list)  # holders' reported prices (fallback only)

    @property
    def overlap(self) -> int:
        return len(self.wallets)

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
    for wallet, positions in cohort_positions.items():
        seen_for_wallet = set()
        for p in positions:
            if not p.asset or p.asset in seen_for_wallet:
                continue
            seen_for_wallet.add(p.asset)
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
    return by_asset


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
@dataclass
class GuardrailResult:
    ok: bool
    reason: str = ""


def passes_guardrails(*, tier: str, price, liquidity, market_closed: bool,
                      cfg, strategy: str) -> GuardrailResult:
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
