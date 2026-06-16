"""
US-availability matcher — which Polymarket (global) positions can a US-based
user actually trade on Polymarket US?

Background
----------
Polymarket US (gateway.polymarket.us, a PUBLIC keyless gateway) lists a small,
slow-moving set of non-sports event families: U.S. elections (midterms, state
governor/senate races), the Fed/FOMC, CPI, jobs/unemployment/GDP, IPO
confirmations, and a little culture. The global cohort's edge is mostly
geopolitics / soccer / foreign elections that ISN'T on Polymarket US — but a
real slice (U.S. elections, the Fed, IPOs) IS, just worded differently across
the two venues (our "Republican control of the House" == their "U.S. House
Midterm Winner").

This module fetches the live US event catalog and tags a global position with
``us_available`` + a link when it maps to a *tradeable* US event.

Matching is precise on purpose — NO loose fuzzy (that produced "Red Sox vs
Yankees" -> "New York vs. Boston" and "aliens" false positives). Two tiers:

  A. exact normalized-title match against the live US event titles, and
  B. a small set of entity rules for the recurring families whose wording
     differs across venues (midterms, Fed, CPI, jobs, GDP, IPOs, state races).

Every rule resolves to a LIVE event slug by anchor tokens looked up in the
fetched catalog, so nothing is pinned to a slug that could rotate (e.g. the
Fed event slug changes every meeting).

The pure helpers (`build_index_from_events`, `match_title`, `tag_rows`) take
plain data and never touch the network, so they're unit-tested deterministically.
`fetch_us_events` / `build_us_index` are the thin network wrappers used by the
poller's publish step.
"""

from __future__ import annotations

import json
import re
import urllib.request

US_BASE = "https://gateway.polymarket.us"
US_EVENT_URL = "https://polymarket.us/event/"          # public event page (slug -> page)
US_CATEGORIES = ("politics", "finance", "macro", "culture")  # sports excluded by design

# Words that carry no matching signal — dropped before token comparison.
_STOP = {
    "will", "the", "a", "an", "of", "in", "on", "at", "by", "to", "for", "and",
    "or", "be", "is", "are", "was", "were", "this", "that", "with", "as", "after",
    "before", "win", "wins", "winner", "winning", "won", "election", "elections",
    "who", "which", "what", "vs", "market", "markets", "official", "officially",
    "confirm", "confirms", "confirmed", "officially", "us", "u", "s", "usa",
    "2024", "2025", "2026", "2027", "2028", "year", "yoy", "over", "exist", "exists",
    "party", "control", "controls", "controlled", "primary", "general", "race",
}


_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"}


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for exact compare."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()


def _tokens(s: str) -> set:
    """Significant tokens of a title (normalized, stopwords + dust removed)."""
    return {t for t in _norm(s).split() if t not in _STOP and len(t) > 1}


# --- US state names (incl. multiword) for state-race matching -----------------
_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
]


def build_index_from_events(events: list) -> dict:
    """Build the lookup index from a list of US /v1/events dicts (pure).

    Keeps only *tradeable* events (active and not closed) in the non-sports
    categories. For each we store its normalized title, significant token set,
    slug, category and a sort key (soonest end first). Also a company->event
    map (finance) so "Stripe IPO" matches the multi-company IPO event.
    """
    by_norm: dict[str, dict] = {}
    items: list[dict] = []
    company: dict[str, dict] = {}

    for e in events or []:
        if (e.get("category") or "").lower() == "sports":
            continue
        if not e.get("active") or e.get("closed"):
            continue
        title = e.get("title") or ""
        slug = e.get("slug") or ""
        if not slug:
            continue
        toks = _tokens(title)
        # fold each sub-market's outcome title into the event's tokens so
        # company/candidate names (Stripe, Revolut, ...) are matchable
        sub_toks = set()
        for m in (e.get("markets") or []):
            sub_toks |= _tokens(m.get("title") or m.get("titleShort") or "")
        ev = {
            "slug": slug,
            "title": title,
            "category": (e.get("category") or "").lower(),
            "url": US_EVENT_URL + slug,
            "end": str(e.get("endDate") or "9999"),
            "tokens": toks | sub_toks,
        }
        items.append(ev)
        nt = _norm(title)
        if nt and nt not in by_norm:
            by_norm[nt] = ev
        if ev["category"] == "finance":
            for tk in (toks | sub_toks):
                company.setdefault(tk, ev)

    items.sort(key=lambda e: e["end"])     # soonest-resolving first
    return {"items": items, "by_norm": by_norm, "company": company}


def _find(index: dict, *need: str):
    """Soonest tradeable event whose token set contains all of `need`."""
    need_set = set(need)
    for ev in index["items"]:
        if need_set <= ev["tokens"]:
            return ev
    return None


def _state_in(norm_title: str):
    """Return the US state name present in a normalized title, if any."""
    for st in _STATES:
        if st in norm_title:
            return st
    return None


def _hit(ev: dict, how: str) -> dict:
    return {"us_available": True, "us_slug": ev["slug"], "us_url": ev["url"],
            "us_title": ev["title"], "us_category": ev["category"], "us_match": how}


def match_title(title: str, index: dict):
    """Return a us_* hit dict for a global market title, or None. Pure."""
    if not title or not index or not index.get("items"):
        return None
    nt = _norm(title)
    toks = _tokens(title)

    # Tier A — exact normalized-title match against the live catalog.
    ev = index["by_norm"].get(nt)
    if ev:
        return _hit(ev, "exact")

    # Tier B — entity rules for families worded differently across venues.
    # Midterms (national House / Senate): party-control or "midterm" wording.
    midterm = ("midterm" in toks) or ("midterms" in toks)
    if "house" in toks and (midterm or "house" in nt and "midterm" in nt):
        if (ev := _find(index, "house", "midterm")):
            return _hit(ev, "rule:house-midterm")
    if "senate" in toks and midterm:
        if (ev := _find(index, "senate", "midterm")):
            return _hit(ev, "rule:senate-midterm")

    # The Fed / FOMC rate decision. Polymarket US lists ONE upcoming meeting at
    # a time ("Fed Decision in <Month>"), so only match a per-meeting global
    # market whose month matches the listed event — not annual-aggregate
    # markets ("10 rate cuts in 2026") or other months not yet listed.
    if ({"fed", "fomc"} & toks) and ({"decision", "rate", "rates", "hike",
                                      "cut", "cuts", "fomc", "bps", "basis",
                                      "interest"} & toks):
        if (ev := _find(index, "fed")):
            ev_month = _MONTHS & ev["tokens"]
            q_month = _MONTHS & toks
            if ev_month and ev_month == q_month:
                return _hit(ev, "rule:fed")

    # CPI / inflation, jobs/unemployment, GDP (macro), only if currently listed.
    if ("cpi" in toks or "inflation" in toks) and (ev := _find(index, "cpi")):
        return _hit(ev, "rule:cpi")
    if "unemployment" in toks and (ev := _find(index, "unemployment")):
        return _hit(ev, "rule:unemployment")
    if ({"jobs", "payrolls", "nonfarm"} & toks) and (ev := _find(index, "jobs")):
        return _hit(ev, "rule:jobs")
    if "gdp" in toks and (ev := _find(index, "gdp")):
        return _hit(ev, "rule:gdp")

    # IPO confirmations — match the company to a finance event.
    if "ipo" in toks:
        for tk in toks:
            ev = index["company"].get(tk)
            if ev:
                return _hit(ev, "rule:ipo")

    # State governor / senate races (non-national).
    if {"governor", "gubernatorial", "senate"} & toks:
        st = _state_in(nt)
        if st:
            kind = "governor" if ({"governor", "gubernatorial"} & toks) else "senate"
            need = st.split() + [kind]
            if (ev := _find(index, *need)):
                return _hit(ev, f"rule:state-{kind}")

    return None


_US_FIELDS = ("us_available", "us_slug", "us_url", "us_title", "us_category", "us_match")


def tag_rows(rows: list, index: dict, title_key: str = "title") -> int:
    """Tag each row in-place with us_* fields. Returns the match count. Pure.

    Rows that don't match are stamped ``us_available=False`` so the dashboard's
    US filter (`us_available===true`) treats them consistently.
    """
    n = 0
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        hit = match_title(r.get(title_key) or "", index) if index else None
        if hit:
            r.update(hit)
            n += 1
        else:
            r["us_available"] = False
            for f in _US_FIELDS[1:]:
                r.pop(f, None)
    return n


# --- network wrappers (not exercised by unit tests) ---------------------------

def _fetch_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "copy-signal/us"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_us_events(categories=US_CATEGORIES, fetch_json=_fetch_json) -> list:
    """Fetch tradeable-ish US events across the non-sports categories."""
    out: list = []
    for cat in categories:
        try:
            data = fetch_json(f"{US_BASE}/v1/events?categories={cat}&limit=500")
            out.extend(data.get("events") or [])
        except Exception as e:               # best-effort per category
            print(f"us_market: fetch {cat} failed: {e}")
    return out


def build_us_index(fetch_json=_fetch_json):
    """Fetch + build the live US index. Returns None on total failure."""
    events = fetch_us_events(fetch_json=fetch_json)
    if not events:
        return None
    idx = build_index_from_events(events)
    return idx if idx.get("items") else None
