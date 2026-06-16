"""
Thin, normalizing client for Polymarket's public (read-only, no-auth) REST APIs.

THIS IS THE ONLY FILE THAT KNOWS THE RAW API SHAPE.
The rest of the app consumes the normalized dataclasses below. If Polymarket
changes an endpoint or a field name, fix it here and nowhere else.

--------------------------------------------------------------------------
VERIFIED CONTRACT — confirmed against the LIVE APIs on 2026-06-15.
(Field names drift between versions and the docs lagged reality in three
places, so these were checked by actually calling the endpoints, not from
docs/memory. Re-verify with `python -m poller.polymarket --selftest`.)

Hosts (all no-auth for the reads below):
  data-api.polymarket.com   leaderboard, positions
  clob.polymarket.com       current prices / midpoints
  gamma-api.polymarket.com  market metadata, liquidity, resolution status

1) PROFIT LEADERBOARD ("monthly winners")
   GET data-api.polymarket.com/v1/leaderboard
       ?timePeriod=MONTH        DAY|WEEK|MONTH|ALL   (MONTH = 30-day window)
       &orderBy=PNL             PNL=profit ranking, VOL=volume
       &limit=N                 1..50
   row -> {rank: "1" (STRING!), proxyWallet, userName, pnl: float, vol: float}

2) A WALLET'S POSITIONS
   GET data-api.polymarket.com/positions
       ?user=0x...&sizeThreshold=1&limit=500
   pos -> {conditionId, asset (=CLOB token id), outcome, outcomeIndex,
           size, avgPrice, curPrice, currentValue, redeemable (bool),
           title, slug, endDate}
   NOTE: redeemable=true  =>  market has resolved (a closed/redeemable holding),
         so it is NOT an open position.

3) AN OUTCOME TOKEN'S CURRENT PRICE (CLOB)
   GET clob.polymarket.com/price?token_id=<asset>&side=BUY|SELL -> {"price":"0.928"}
   GET clob.polymarket.com/midpoint?token_id=<asset>           -> {"mid":"0.9295"}
   (both prices are STRINGS; field is "mid", NOT "mid_price")
   404 "No orderbook exists" is EXPECTED for resolved markets -> treated as None.

4) A MARKET'S STATUS + LIQUIDITY + TOKEN IDS (Gamma)
   GET gamma-api.polymarket.com/markets?condition_ids=0x...
   market -> {conditionId, closed (bool), active (bool), liquidityNum (float),
              outcomes '["Yes","No"]' (STRINGIFIED json),
              clobTokenIds '["...","..."]' (STRINGIFIED json, index-aligned to outcomes),
              outcomePrices '["0","1"]' (STRINGIFIED; on resolution this is the payout),
              umaResolutionStatuses (STRINGIFIED; note the trailing 'es'), endDate}
   Gamma does NOT serve every archived/resolved market -> a lookup may return
   [] even for a real conditionId; callers must tolerate `None`.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

_VALID_WINDOWS = {"DAY", "WEEK", "MONTH", "ALL"}


# --------------------------------------------------------------------------- #
# Normalized return types
# --------------------------------------------------------------------------- #
@dataclass
class LeaderboardEntry:
    rank: int
    wallet: str
    username: str
    pnl: float
    volume: float
    profile_image: str = ""
    x_username: str = ""
    verified: bool = False


@dataclass
class Position:
    wallet: str
    condition_id: str
    asset: str            # CLOB token id; uniquely identifies market+outcome
    outcome: str
    outcome_index: int
    size: float
    avg_price: float
    cur_price: float      # the trader's reported current price (a useful fallback)
    current_value: float
    redeemable: bool      # True => market resolved (not an open position)
    title: str
    slug: str
    end_date: Optional[str]
    cash_pnl: float = 0.0     # the trader's open P&L on this position (USD)
    percent_pnl: float = 0.0  # their open P&L as a fraction


@dataclass
class MarketInfo:
    condition_id: str
    closed: bool
    active: bool
    liquidity: float
    outcomes: list = field(default_factory=list)          # ["Yes","No"]
    clob_token_ids: list = field(default_factory=list)    # index-aligned to outcomes
    outcome_prices: list = field(default_factory=list)    # floats; payout once resolved
    uma_resolution_statuses: list = field(default_factory=list)
    end_date: Optional[str] = None

    def resolved_price_for(self, outcome_index: int) -> Optional[float]:
        """Resolved payout (0 or 1) for an outcome, if the market is closed."""
        if not self.closed:
            return None
        if 0 <= outcome_index < len(self.outcome_prices):
            return self.outcome_prices[outcome_index]
        return None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class PolymarketClient:
    """All methods are read-only. This client cannot place orders."""

    def __init__(self, timeout: float = 20.0, retries: int = 3, backoff: float = 1.5):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "polymarket-copy-signal/1.0 (read-only)", "Accept": "application/json"}
        )

    # -- low-level HTTP ----------------------------------------------------- #
    # A 4xx won't fix itself and fails fast — EXCEPT rate-limit (429) and request
    # timeout (408), which are transient and get retried with jittered backoff.
    _RETRYABLE_4XX = (408, 429)

    def _backoff_sleep(self, attempt, last_err):
        """Sleep before a retry: honor Retry-After on a 429, else jittered
        exponential backoff. Jitter avoids a synchronized re-burst across the
        50-wallet thread pool that would just trip the limit again."""
        ra = None
        try:
            resp = getattr(last_err, "response", None)
            if resp is not None:
                ra = float(resp.headers.get("Retry-After") or 0) or None
        except (ValueError, TypeError):
            ra = None
        time.sleep(ra if ra else self.backoff * (2 ** attempt) * (1 + random.random() * 0.3))

    def _get(self, url: str, params: dict | None = None, ok_404: bool = False):
        """GET with retries. Returns parsed JSON, or None on a tolerated 404."""
        last_err = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 404 and ok_404:
                    return None
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else None
                if code is not None and 400 <= code < 500 and code not in self._RETRYABLE_4XX:
                    raise
                last_err = e
            except requests.RequestException as e:
                last_err = e
            if attempt < self.retries - 1:
                self._backoff_sleep(attempt, last_err)
        raise RuntimeError(f"GET {url} failed after {self.retries} attempts: {last_err}")

    def _post(self, url: str, body):
        last_err = None
        for attempt in range(self.retries):
            try:
                r = self.session.post(url, json=body, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else None
                if code is not None and 400 <= code < 500 and code not in self._RETRYABLE_4XX:
                    raise
                last_err = e
            except requests.RequestException as e:
                last_err = e
            if attempt < self.retries - 1:
                self._backoff_sleep(attempt, last_err)
        raise RuntimeError(f"POST {url} failed after {self.retries} attempts: {last_err}")

    @staticmethod
    def _parse_json_array(value, cast=None) -> list:
        """Gamma returns several arrays as JSON-encoded strings; parse defensively."""
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return []
        if not isinstance(value, list):
            return []
        if cast is not None:
            out = []
            for v in value:
                try:
                    out.append(cast(v))
                except (ValueError, TypeError):
                    out.append(None)
            return out
        return value

    @staticmethod
    def _f(value, default=0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # -- 1) leaderboard ----------------------------------------------------- #
    def leaderboard(self, window: str = "MONTH", limit: int = 5, order: str = "PNL") -> list[LeaderboardEntry]:
        window = (window or "MONTH").upper()
        if window not in _VALID_WINDOWS:
            window = "MONTH"
        rows = self._get(
            f"{DATA_API}/v1/leaderboard",
            params={"timePeriod": window, "orderBy": order, "limit": max(1, min(int(limit), 50))},
        ) or []
        out = []
        for row in rows:
            try:
                rank = int(row.get("rank"))
            except (ValueError, TypeError):
                rank = len(out) + 1
            out.append(
                LeaderboardEntry(
                    rank=rank,
                    wallet=(row.get("proxyWallet") or "").lower(),
                    username=row.get("userName") or "",
                    pnl=self._f(row.get("pnl")),
                    volume=self._f(row.get("vol")),
                    profile_image=row.get("profileImage") or "",
                    x_username=row.get("xUsername") or "",
                    verified=bool(row.get("verifiedBadge")),
                )
            )
        out.sort(key=lambda e: e.rank)
        return out

    # -- 2) positions ------------------------------------------------------- #
    def positions(self, wallet: str, size_threshold: float = 1.0, only_open: bool = True) -> list[Position]:
        rows = self._get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": size_threshold, "limit": 500},
        ) or []
        out = []
        for row in rows:
            redeemable = bool(row.get("redeemable"))
            if only_open and redeemable:
                continue  # resolved/redeemable holding -> not an open position
            out.append(
                Position(
                    wallet=wallet.lower(),
                    condition_id=row.get("conditionId") or "",
                    asset=str(row.get("asset") or ""),
                    outcome=row.get("outcome") or "",
                    outcome_index=int(row.get("outcomeIndex") or 0),
                    size=self._f(row.get("size")),
                    avg_price=self._f(row.get("avgPrice")),
                    cur_price=self._f(row.get("curPrice")),
                    current_value=self._f(row.get("currentValue")),
                    redeemable=redeemable,
                    title=row.get("title") or "",
                    slug=row.get("slug") or "",
                    end_date=row.get("endDate"),
                    cash_pnl=self._f(row.get("cashPnl")),
                    percent_pnl=self._f(row.get("percentPnl")),
                )
            )
        return out

    # -- 3) prices ---------------------------------------------------------- #
    def price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        data = self._get(f"{CLOB_API}/price", params={"token_id": token_id, "side": side}, ok_404=True)
        if not data or "price" not in data:
            return None
        return self._f(data["price"], default=None) if data.get("price") not in (None, "") else None

    def midpoint(self, token_id: str) -> Optional[float]:
        data = self._get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, ok_404=True)
        if not data or "mid" not in data:
            return None
        return self._f(data["mid"], default=None) if data.get("mid") not in (None, "") else None

    # -- 4) market metadata ------------------------------------------------- #
    def market(self, condition_id: str) -> Optional[MarketInfo]:
        rows = self._get(f"{GAMMA_API}/markets", params={"condition_ids": condition_id}, ok_404=True) or []
        # Gamma returns a list; match exactly (it may ignore the filter on some hosts).
        match = next((m for m in rows if (m.get("conditionId") or "") == condition_id), None)
        if match is None:
            return None
        return MarketInfo(
            condition_id=match.get("conditionId") or condition_id,
            closed=bool(match.get("closed")),
            active=bool(match.get("active")),
            liquidity=self._f(match.get("liquidityNum"), default=self._f(match.get("liquidity"))),
            outcomes=self._parse_json_array(match.get("outcomes")),
            clob_token_ids=self._parse_json_array(match.get("clobTokenIds")),
            outcome_prices=self._parse_json_array(match.get("outcomePrices"), cast=float),
            uma_resolution_statuses=self._parse_json_array(match.get("umaResolutionStatuses")),
            end_date=match.get("endDate"),
        )

    # -- BATCH variants (one call for many) -------------------------------- #
    def positions_many(self, wallets, size_threshold: float = 1.0, only_open: bool = True):
        """Fetch many wallets' positions concurrently.

        Returns (positions, failed): `positions` is {wallet: [Position]} for every
        wallet that fetched OK; `failed` is the set of wallets whose fetch raised.
        Callers MUST distinguish these — an empty list means "holds nothing", a
        failed fetch means "we couldn't look". Treating the latter as the former
        fabricates phantom cohort-abandonment exits (a real bug this guards)."""
        wallets = list(wallets)
        if not wallets:
            return {}, set()
        out: dict = {}
        failed: set = set()
        with ThreadPoolExecutor(max_workers=min(10, len(wallets))) as ex:
            futs = {ex.submit(self.positions, w, size_threshold, only_open): w for w in wallets}
            for fut, w in futs.items():
                try:
                    out[w] = fut.result()
                except Exception:
                    failed.add(w)
        return out, failed

    def midpoints(self, token_ids, chunk: int = 250) -> dict:
        """POST /midpoints (chunked). Returns {token_id: float} (omits tokens with no book)."""
        ids = [t for t in dict.fromkeys(token_ids) if t]
        out = {}
        for i in range(0, len(ids), chunk):
            try:
                data = self._post(f"{CLOB_API}/midpoints", [{"token_id": t} for t in ids[i:i + chunk]]) or {}
            except Exception:
                continue
            for t, v in (data.items() if isinstance(data, dict) else []):
                f = self._f(v, default=None) if v not in (None, "") else None
                if f is not None:
                    out[t] = f
        return out

    def prices(self, token_ids, side: str = "BUY", chunk: int = 250) -> dict:
        """POST /prices (chunked). Returns {token_id: float} for the given side."""
        ids = [t for t in dict.fromkeys(token_ids) if t]
        out = {}
        for i in range(0, len(ids), chunk):
            try:
                data = self._post(f"{CLOB_API}/prices", [{"token_id": t, "side": side} for t in ids[i:i + chunk]]) or {}
            except Exception:
                continue
            for t, v in (data.items() if isinstance(data, dict) else []):
                px = v.get(side) if isinstance(v, dict) else v
                f = self._f(px, default=None) if px not in (None, "") else None
                if f is not None:
                    out[t] = f
        return out

    def marks(self, token_ids, source: str = "midpoint") -> dict:
        """Best current price for many tokens in one shot: {token_id: float}."""
        return self.prices(token_ids, "BUY") if source == "buy" else self.midpoints(token_ids)

    def markets(self, condition_ids, chunk: int = 40) -> dict:
        """Fetch many markets by condition id (repeated `condition_ids` param,
        chunked). Returns {condition_id: MarketInfo}."""
        ids = [c for c in dict.fromkeys(condition_ids) if c]
        out: dict = {}
        for i in range(0, len(ids), chunk):
            group = ids[i:i + chunk]
            params = [("condition_ids", c) for c in group] + [("limit", "500")]
            try:
                rows = self._get(f"{GAMMA_API}/markets", params=params, ok_404=True) or []
            except Exception:
                rows = []
            for m in rows:
                cid = m.get("conditionId")
                if not cid or cid in out:
                    continue
                out[cid] = MarketInfo(
                    condition_id=cid, closed=bool(m.get("closed")), active=bool(m.get("active")),
                    liquidity=self._f(m.get("liquidityNum"), default=self._f(m.get("liquidity"))),
                    outcomes=self._parse_json_array(m.get("outcomes")),
                    clob_token_ids=self._parse_json_array(m.get("clobTokenIds")),
                    outcome_prices=self._parse_json_array(m.get("outcomePrices"), cast=float),
                    uma_resolution_statuses=self._parse_json_array(m.get("umaResolutionStatuses")),
                    end_date=m.get("endDate"),
                )
        return out

    # -- composite: current mark price ------------------------------------- #
    def mark_price(self, token_id: str, source: str = "midpoint",
                   market: Optional[MarketInfo] = None, outcome_index: int = 0,
                   fallback: Optional[float] = None) -> Optional[float]:
        """
        Best available 'current market price' for an outcome token, with a
        resolved-market fallback chain:
          live orderbook (midpoint or best-ask)  ->  resolved payout  ->  fallback
        """
        if source == "buy":
            p = self.price(token_id, side="BUY")
        else:
            p = self.midpoint(token_id)
            if p is None:
                p = self.price(token_id, side="BUY")  # midpoint missing but book may price one side
        if p is not None:
            return p
        if market is not None:
            rp = market.resolved_price_for(outcome_index)
            if rp is not None:
                return rp
        return fallback


# --------------------------------------------------------------------------- #
# Self-test: hit the live APIs end-to-end and confirm the contract still holds.
#   python -m poller.polymarket --selftest
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    c = PolymarketClient()
    print("1) leaderboard(MONTH, top 3):")
    lb = c.leaderboard(window="MONTH", limit=3)
    for e in lb:
        print(f"   #{e.rank:<2} {e.wallet}  {e.username[:20]:<20} pnl=${e.pnl:,.0f}")
    assert lb and lb[0].wallet.startswith("0x"), "leaderboard returned no usable rows"

    print(f"\n2) positions for #1 ({lb[0].wallet}):")
    pos = c.positions(lb[0].wallet, only_open=True)
    print(f"   {len(pos)} open positions")
    sample = None
    # find a sample with a live orderbook to exercise the price path
    for p in pos:
        m = c.market(p.condition_id)
        if m and not m.closed:
            sample = (p, m)
            break
    if sample is None and pos:
        sample = (pos[0], c.market(pos[0].condition_id))

    if sample:
        p, m = sample
        print(f"   sample: {p.title[:48]!r} [{p.outcome}] size={p.size:,.0f}")
        print(f"\n3) prices for asset {p.asset[:18]}...:")
        print(f"   midpoint={c.midpoint(p.asset)}  buy={c.price(p.asset,'BUY')}  mark={c.mark_price(p.asset, market=m, outcome_index=p.outcome_index)}")
        print(f"\n4) market {p.condition_id[:18]}...:")
        if m:
            print(f"   closed={m.closed} active={m.active} liquidity=${m.liquidity:,.0f} outcomes={m.outcomes} tokens={len(m.clob_token_ids)}")
        else:
            print("   (not served by Gamma — fallback path would apply)")
    print("\nOK: live contract holds.")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
