"""
Unit tests for the US-availability matcher (poller/us_market.py).

All pure / offline: we build the index from a fixture event list shaped like
the live gateway.polymarket.us /v1/events payload, then assert match_title /
tag_rows behaviour. The point of the matcher is PRECISION — these tests pin the
real matches (midterms, Fed, state races, IPOs) and the deliberate non-matches
(foreign elections, sports, wrong-month Fed, annual-aggregate Fed).
"""

from poller import us_market as um


# Fixture US events — mirrors the live shape (title/slug/category/active/closed,
# plus sub-markets whose titles carry company/candidate names).
EVENTS = [
    {"title": "U.S House Midterm Winner", "slug": "usho-midterms-2026-11-03",
     "category": "politics", "active": True, "closed": False, "endDate": "2026-11-03"},
    {"title": "U.S Senate Midterm Winner", "slug": "usse-midterms-2026-11-03",
     "category": "politics", "active": True, "closed": False, "endDate": "2026-11-03"},
    {"title": "Georgia Senate Election Winner", "slug": "usse-ga-2026-11-03",
     "category": "politics", "active": True, "closed": False, "endDate": "2026-11-03",
     "markets": [{"title": "Republican Party", "ep3Status": "OPEN", "closed": False}]},
    # a competing Georgia Senate PRIMARY (same state+office) to test instance routing
    {"title": "Georgia Senate Republican Primary Winner", "slug": "ussep-ga-2026-08-04-rep",
     "category": "politics", "active": True, "closed": False, "endDate": "2026-08-04"},
    {"title": "South Carolina Governor Republican Primary Winner",
     "slug": "usgubp-sc-2026-06-09-rep", "category": "politics",
     "active": True, "closed": False, "endDate": "2026-06-09"},
    # active event but every market is closed/expired -> NOT tradeable, must be dropped
    {"title": "Old Resolved Macro Print", "slug": "old-resolved", "category": "macro",
     "active": True, "closed": False, "endDate": "2026-01-01",
     "markets": [{"title": "Yes", "ep3Status": "EXPIRED", "closed": True}]},
    {"title": "Fed Decision in June", "slug": "usfed-fomc-2026-06-17",
     "category": "macro", "active": True, "closed": False, "endDate": "2026-06-17"},
    {"title": "Which Companies Will Confirm an IPO in 2026?", "slug": "2026ipos",
     "category": "finance", "active": True, "closed": False, "endDate": "2027-01-14",
     "markets": [{"title": "Stripe"}, {"title": "Revolut"}, {"title": "Canva"}]},
    {"title": "GTA VI Released?", "slug": "gtavi", "category": "culture",
     "active": True, "closed": False, "endDate": "2026-12-31"},
    # excluded: sports, and a closed macro event
    {"title": "Los Angeles vs. Tennessee", "slug": "aec-nfl-lac-ten",
     "category": "sports", "active": True, "closed": False, "endDate": "2025-11-02"},
    {"title": "CPI year-over-year in April", "slug": "cpic-apr",
     "category": "macro", "active": True, "closed": True, "endDate": "2026-05-12"},
]


def idx():
    return um.build_index_from_events(EVENTS)


def test_index_excludes_sports_and_closed():
    i = idx()
    slugs = {e["slug"] for e in i["items"]}
    assert "aec-nfl-lac-ten" not in slugs      # sports excluded by design
    assert "cpic-apr" not in slugs             # closed event excluded
    assert "usho-midterms-2026-11-03" in slugs


def test_exact_title_match():
    hit = um.match_title("GTA VI Released?", idx())
    assert hit and hit["us_slug"] == "gtavi" and hit["us_match"] == "exact"


def test_house_and_senate_midterm_rules():
    i = idx()
    h = um.match_title("Will the Republican Party control the House after the 2026 Midterm elections?", i)
    assert h and h["us_slug"] == "usho-midterms-2026-11-03" and h["us_match"] == "rule:house-midterm"
    s = um.match_title("Will the Democratic Party control the Senate after the 2026 Midterms?", i)
    assert s and s["us_slug"] == "usse-midterms-2026-11-03"


def test_fed_requires_matching_month():
    i = idx()
    assert um.match_title("Will there be no change in Fed interest rates after the June 2026 meeting?", i)["us_slug"] == "usfed-fomc-2026-06-17"
    # wrong month (no July event) and annual-aggregate must NOT match the June event
    assert um.match_title("Will the Fed increase interest rates by 25 bps after the July 2026 meeting?", i) is None
    assert um.match_title("Will 10 Fed rate cuts happen in 2026?", i) is None


def test_state_race_rules():
    i = idx()
    g = um.match_title("Will Nancy Mace win the 2026 South Carolina Governor Republican primary?", i)
    assert g and g["us_slug"] == "usgubp-sc-2026-06-09-rep" and g["us_match"] == "rule:state-governor"
    se = um.match_title("Will the Republicans win the Georgia Senate race in 2026?", i)
    assert se and se["us_slug"] == "usse-ga-2026-11-03" and se["us_match"] == "rule:state-senate"


def test_general_vs_primary_routing():
    """A general-election market must NOT route to a same-state Primary sub-event,
    even though both contain the state+office and the party word is in the title."""
    i = idx()
    gen = um.match_title("Will the Republicans win the Georgia Senate race in 2026?", i)
    assert gen["us_slug"] == "usse-ga-2026-11-03"            # the general, not the R primary
    prim = um.match_title("Will Jon Ossoff be the Republican nominee for Senate in Georgia?", i)
    assert prim["us_slug"] == "ussep-ga-2026-08-04-rep"      # routed to the R primary


def test_untradeable_events_excluded():
    """Active event whose markets are all closed/expired is not in the index."""
    i = idx()
    slugs = {e["slug"] for e in i["items"]}
    assert "old-resolved" not in slugs
    assert um.match_title("Old Resolved Macro Print", i) is None


def test_market_open_helper():
    assert um._market_open({"ep3Status": "OPEN", "closed": False})
    assert um._market_open({"closed": False})                # absent status -> assume ok
    assert not um._market_open({"ep3Status": "EXPIRED", "closed": True})
    assert not um._market_open({"closed": True})
    assert not um._market_open({"archived": True})


def test_slim_event_keeps_matchable_fields():
    raw = {"title": "Fed Decision in June", "slug": "x", "category": "macro",
           "active": True, "closed": False, "endDate": "z", "description": "huge text",
           "image": "u", "markets": [{"title": "Maintains", "ep3Status": "OPEN",
                                       "outcomePrices": "[...]", "closed": False}]}
    s = um.slim_event(raw)
    assert "description" not in s and "image" not in s
    assert s["title"] == "Fed Decision in June" and s["slug"] == "x"
    assert s["markets"][0]["ep3Status"] == "OPEN" and "outcomePrices" not in s["markets"][0]
    # a slimmed event still builds a working index + matches
    i = um.build_index_from_events([s])
    assert um.match_title("no change in Fed interest rates after the June meeting?", i)


def test_ipo_company_match():
    i = idx()
    hit = um.match_title("Will Stripe confirm an IPO in 2026?", i)
    assert hit and hit["us_slug"] == "2026ipos" and hit["us_match"] == "rule:ipo"


def test_no_false_positives():
    i = idx()
    for title in [
        "Will Keiko Fujimori win the 2026 Peruvian presidential election?",
        "Will New Zealand win on 2026-06-15?",
        "Pittsburgh Pirates vs. Athletics",
        "US x Iran permanent peace deal by June 30, 2026?",
        "Will the Communist Party of the Russian Federation gain the most seats?",
    ]:
        assert um.match_title(title, i) is None, title


def test_tag_rows_stamps_and_counts():
    i = idx()
    rows = [
        {"title": "GTA VI Released?"},
        {"title": "Will Haiti win the 2026 FIFA World Cup?"},
    ]
    n = um.tag_rows(rows, i)
    assert n == 1
    assert rows[0]["us_available"] is True and rows[0]["us_url"].endswith("/gtavi")
    assert rows[1]["us_available"] is False and "us_slug" not in rows[1]


def test_tag_rows_handles_empty_and_nonmatch_cleanup():
    i = idx()
    # a row previously tagged True must be cleared when it no longer matches
    row = {"title": "Some foreign market", "us_available": True, "us_slug": "stale", "us_url": "x"}
    um.tag_rows([row], i)
    assert row["us_available"] is False and "us_slug" not in row
    assert um.tag_rows([], i) == 0
    assert um.tag_rows(None, i) == 0


def test_build_index_from_empty():
    i = um.build_index_from_events([])
    assert i["items"] == [] and um.match_title("anything", i) is None
