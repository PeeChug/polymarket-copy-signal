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
import unicodedata
import urllib.request
from datetime import date as _date_cls

US_BASE = "https://gateway.polymarket.us"
US_EVENT_URL = "https://polymarket.us/event/"          # public event page (slug -> page)
# Sports IS now fetched: Polymarket US lists the full World Cup / MLB / etc. as
# "Team A vs. Team B" match events, matched by TEAM + DATE (not the loose token
# path the non-sports families use). Sports needs closed=false to surface the live
# fixtures (the default page is full of old, closed games).
US_CATEGORIES = ("politics", "finance", "macro", "culture", "sports")

# Words that carry no matching signal — dropped before token comparison.
_STOP = {
    "will", "the", "a", "an", "of", "in", "on", "at", "by", "to", "for", "and",
    "or", "be", "is", "are", "was", "were", "this", "that", "with", "as", "after",
    "before", "win", "wins", "winner", "winning", "won", "election", "elections",
    "who", "which", "what", "vs", "market", "markets", "official", "officially",
    "confirm", "confirms", "confirmed", "officially", "us", "u", "s", "usa",
    "2024", "2025", "2026", "2027", "2028", "year", "yoy", "over", "exist", "exists",
    "party", "control", "controls", "controlled", "race",
}
# kept OUT of _STOP so they can disambiguate primary vs general and party:
#   primary, general, republican(s), democrat(ic|s), nominee


_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"}


def _norm(s: str) -> str:
    """Lowercase, fold accents, strip punctuation, collapse whitespace. Accent
    folding lets 'Türkiye'->'turkiye' and 'Côte d'Ivoire'->'cote d ivoire' match
    the US venue's ASCII team names."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def _slug_date(s: str):
    """First YYYY-MM-DD found in a string (US match slugs end '...-2026-06-18')."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s or "")
    return m.group(0) if m else None


def _date_gap(a, b):
    """Absolute day gap between two YYYY-MM-DD dates, or None if unparseable."""
    try:
        da = _date_cls.fromisoformat((a or "")[:10])
        db = _date_cls.fromisoformat((b or "")[:10])
        return abs((da - db).days)
    except (ValueError, TypeError):
        return None


def _vs_teams(title: str):
    """['team a','team b'] (normalized) for a 'Team A vs. Team B' title, else []."""
    parts = re.split(r"\s+vs\.?\s+", title or "", maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return []
    return [t for t in (_norm(parts[0]), _norm(parts[1])) if t]


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


def _market_open(m: dict) -> bool:
    """A US sub-market is genuinely tradeable right now."""
    if not isinstance(m, dict):
        return False
    if m.get("closed") or m.get("archived") or m.get("hidden"):
        return False
    st = m.get("ep3Status")
    return st in (None, "", "OPEN")          # OPEN = order book live; absent = assume ok


def _event_tradeable(e: dict) -> bool:
    """Event is live AND has at least one genuinely-open market (so we never
    link to an event whose markets have all expired/resolved)."""
    if not e.get("active") or e.get("closed") or e.get("archived") or e.get("hidden"):
        return False
    mk = e.get("markets")
    if not mk:                                # no market detail in payload — trust event flags
        return True
    return any(_market_open(m) for m in mk)


def build_index_from_events(events: list) -> dict:
    """Build the lookup index from a list of US /v1/events dicts (pure).

    Keeps only *genuinely tradeable* events (live, with an open market) in the
    non-sports categories. For each we store its normalized title, significant
    token set, slug, category and a sort key (soonest end first). Also a
    company->event map (finance) so "Stripe IPO" matches the multi-company event.
    """
    by_norm: dict[str, dict] = {}
    items: list[dict] = []
    company: dict[str, dict] = {}
    sports: dict[str, list] = {}     # normalized team -> [(date, ev), ...] for "A vs. B" games

    for e in events or []:
        if not _event_tradeable(e):
            continue
        title = e.get("title") or ""
        slug = e.get("slug") or ""
        if not slug:
            continue
        if (e.get("category") or "").lower() == "sports":
            # Only team-vs-team MATCH events ("Mexico vs. Korea Republic"); these are
            # what a global "Will Mexico win on 2026-06-18?" maps to, keyed by TEAM +
            # DATE. Tournament/award markets ("World Cup Winner", "AL MVP") have no
            # 'vs.' and are skipped — keeping matching precise (date pins the game).
            teams = _vs_teams(title)
            gdate = _slug_date(slug) or str(e.get("startDate") or "")[:10]
            if not teams or not gdate:
                continue
            ev = {"slug": slug, "title": title, "url": US_EVENT_URL + slug, "date": gdate}
            for t in teams:
                sports.setdefault(t, []).append((gdate, ev))
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
    return {"items": items, "by_norm": by_norm, "company": company, "sports": sports}


def _find(index: dict, *need: str, prefer=(), avoid=()):
    """Best tradeable event whose token set contains all of `need`.

    Among candidates, rank by (matched `prefer` tokens − matched `avoid` tokens)
    so a state race links to the right party / primary-vs-general instance — e.g.
    a GENERAL-election market avoids "…Primary Winner" sub-events. Ties keep the
    soonest-resolving (items are pre-sorted soonest-first; strict > is stable).
    """
    need_set, prefer_set, avoid_set = set(need), set(prefer), set(avoid)
    best, best_score = None, -10 ** 9
    for ev in index["items"]:                 # items are soonest-end first
        if need_set <= ev["tokens"]:
            score = len(prefer_set & ev["tokens"]) - len(avoid_set & ev["tokens"])
            if score > best_score:            # strict > keeps the soonest on ties
                best, best_score = ev, score
    return best


def _state_in(norm_title: str):
    """Return the US state name present in a normalized title, if any."""
    for st in _STATES:
        if st in norm_title:
            return st
    return None


def _hit(ev: dict, how: str) -> dict:
    return {"us_available": True, "us_slug": ev["slug"], "us_url": ev["url"],
            "us_title": ev["title"], "us_category": ev.get("category", "sports"), "us_match": how}


def _parse_global_match(title: str, end_date=None):
    """Pull (teams, date) out of a global sports market title, else (None, None):
        'Will Mexico win on 2026-06-18?'        -> (['mexico'], '2026-06-18')
        'Will A vs. B end in a draw?'           -> (['a','b'], end_date)
        'Tampa Bay Rays vs. Los Angeles Dodgers'-> (['tampa bay rays','...'], end_date)
    The single-team 'win on DATE' form carries its own date; the 'vs.' forms fall
    back to the row's resolution date (the game day)."""
    t = title or ""
    m = re.match(r"\s*will\s+(.+?)\s+win\s+on\s+(\d{4}-\d{2}-\d{2})", t, re.IGNORECASE)
    if m:
        team = _norm(m.group(1))
        return ([team] if team else None), m.group(2)
    if re.search(r"\svs\.?\s", t, re.IGNORECASE):
        a, b = re.split(r"\s+vs\.?\s+", t, maxsplit=1, flags=re.IGNORECASE)
        a = re.sub(r"^\s*will\s+", "", a, flags=re.IGNORECASE)
        b = re.sub(r"\s+(?:end\s+in\s+a\s+draw|to\s+win|win).*$", "", b, flags=re.IGNORECASE)
        teams = [x for x in (_norm(a), _norm(b)) if x]
        return (teams or None), (str(end_date)[:10] if end_date else None)
    return None, None


def _match_sports(title: str, index: dict, end_date=None, tol: int = 1):
    """Match a global game to a US 'A vs. B' event by team + nearest date (<= tol
    days, so back-to-back fixtures can't mis-link to the adjacent day)."""
    sports = index.get("sports")
    if not sports:
        return None
    teams, qdate = _parse_global_match(title, end_date)
    if not teams or not qdate:
        return None
    best, best_gap = None, tol + 1
    for team in teams:
        for edate, ev in sports.get(team, ()):
            gap = _date_gap(edate, qdate)
            if gap is not None and gap < best_gap:
                best, best_gap = ev, gap
    return _hit(best, "rule:sports-match") if best is not None else None


def match_title(title: str, index: dict, end_date=None):
    """Return a us_* hit dict for a global market title, or None. Pure.
    `end_date` (the row's resolution date) is used to date-match 'A vs. B' games."""
    if not title or not index or not (index.get("items") or index.get("sports")):
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

    # State governor / senate races (non-national). Prefer the matching party +
    # primary/general instance so the link lands on the right sub-event.
    if {"governor", "gubernatorial", "senate"} & toks:
        st = _state_in(nt)
        if st:
            kind = "governor" if ({"governor", "gubernatorial"} & toks) else "senate"
            is_primary = bool({"primary", "nominee", "runoff"} & toks)
            prefer, avoid = set(), set()
            if is_primary:
                prefer.add("primary")         # primary events are titled "… Primary Winner"
                if {"republican", "republicans", "gop"} & toks:
                    prefer.add("republican")  # route to the right party's primary
                if {"democratic", "democrat", "democrats"} & toks:
                    prefer.add("democratic")
            else:
                avoid.add("primary")          # a general-election market is NOT a primary sub-event
            need = st.split() + [kind]
            if (ev := _find(index, *need, prefer=prefer, avoid=avoid)):
                return _hit(ev, f"rule:state-{kind}")

    # Sports — a global game ("Will Mexico win on 2026-06-18?" / "A vs. B") to the
    # US "Team A vs. Team B" event, matched on team + date.
    sport = _match_sports(title, index, end_date)
    if sport:
        return sport

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
        hit = match_title(r.get(title_key) or "", index, end_date=r.get("end_date")) if index else None
        if hit:
            r.update(hit)
            n += 1
        else:
            r["us_available"] = False
            for f in _US_FIELDS[1:]:
                r.pop(f, None)
    return n


# --- slimming (keep caches small + only the fields the index needs) -----------

_EV_KEEP = ("title", "slug", "category", "active", "closed", "archived", "hidden", "endDate")
_MK_KEEP = ("title", "titleShort", "ep3Status", "closed", "archived", "hidden")


def slim_event(e: dict) -> dict:
    """Reduce a raw US event to just what the matcher needs (drops descriptions,
    images, prices, etc.) so the last-good catalog cache stays small + JSON-safe."""
    out = {k: e.get(k) for k in _EV_KEEP if k in e}
    mk = e.get("markets")
    if mk:
        out["markets"] = [{k: m.get(k) for k in _MK_KEEP if k in m} for m in mk]
    return out


# --- network wrappers (not exercised by unit tests) ---------------------------

def _fetch_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "copy-signal/us"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_us_events(categories=US_CATEGORIES, fetch_json=_fetch_json, attempts=3) -> list:
    """Fetch tradeable US events across the non-sports categories, slimmed.

    Each category is retried with backoff (the gateway can 429/timeout); a
    category that fails ALL attempts is skipped rather than failing the whole
    fetch. Returns [] only if every category failed (caller then uses cache)."""
    import time
    out: list = []
    for cat in categories:
        for a in range(attempts):
            try:
                data = fetch_json(f"{US_BASE}/v1/events?categories={cat}&closed=false&limit=500")
                out.extend(slim_event(e) for e in (data.get("events") or []))
                break
            except Exception as e:
                if a == attempts - 1:
                    print(f"us_market: fetch {cat} failed after {attempts} tries: {e}")
                else:
                    time.sleep(0.6 * (a + 1))   # 0.6s, 1.2s backoff
    return out


def build_us_index(fetch_json=_fetch_json):
    """Fetch + build the live US index. Returns (index, events) or (None, [])
    so the caller can cache `events` as the last-good catalog."""
    events = fetch_us_events(fetch_json=fetch_json)
    if not events:
        return None, []
    idx = build_index_from_events(events)
    return (idx if idx.get("items") else None), events


def _selftest() -> int:
    """Hit the LIVE gateway and assert the catalog shape + that the recurring
    families still resolve — catches Polymarket-US API drift early.
        python -m poller.us_market --selftest
    """
    print("us_market selftest — fetching live Polymarket US catalog…")
    idx, events = build_us_index()
    if not idx:
        print("  FAIL: no events fetched / no tradeable events built.")
        return 1
    print(f"  fetched {len(events)} events; {len(idx['items'])} tradeable non-sports.")
    cats = sorted({e.get("category") for e in events})
    print(f"  categories present: {cats}")
    # shape: every indexed event has the fields the matcher + dashboard need
    bad = [e for e in idx["items"] if not (e.get("slug") and e.get("title") and e.get("url"))]
    if bad:
        print(f"  FAIL: {len(bad)} indexed events missing slug/title/url.")
        return 1
    # behaviour: the canonical families we expect on US should match SOMETHING
    probes = {
        "house-midterm": "Will the Republican Party control the House after the 2026 Midterm elections?",
        "senate-midterm": "Will the Democratic Party control the Senate after the 2026 Midterms?",
    }
    ok = True
    for name, title in probes.items():
        hit = match_title(title, idx)
        print(f"  probe {name:14s}: {'OK -> ' + hit['us_slug'] if hit else 'NO MATCH'}")
        if not hit:
            ok = False
    # a couple of guards against false positives
    for title in ["Will Haiti win the 2026 FIFA World Cup?", "US x Iran permanent peace deal by June 30, 2026?"]:
        if match_title(title, idx):
            print(f"  FAIL: false positive on {title!r}")
            ok = False
    print("  RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    idx, events = build_us_index()
    print(f"{len(events)} events, {len(idx['items']) if idx else 0} tradeable non-sports.")
