"""
Streamlit dashboard for the Polymarket copy-signal tester.

Read-only view of the database the poller writes, plus a forward-only settings
editor. Deploy on Streamlit Community Cloud with the main file set to
`dashboard/app.py` and SUPABASE_URL / SUPABASE_KEY in the app's secrets.

This app NEVER trades and never talks to Polymarket — it only reads the DB
(and writes new config rows).
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import analytics
from core.config import Config
from core.store import PostgrestStore

st.set_page_config(page_title="Polymarket Copy-Signal Tester", layout="wide")


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def _secret(name: str):
    return st.secrets.get(name) or os.environ.get(name)


@st.cache_resource
def get_store() -> PostgrestStore:
    url = _secret("SUPABASE_URL")
    key = _secret("SUPABASE_KEY") or _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL / SUPABASE_KEY in app secrets.")
        st.stop()
    return PostgrestStore(url, key)


@st.cache_data(ttl=120)
def load_all():
    s = get_store()
    return {
        "trades": s.all_trades(),
        "observations": s.latest_observations(),
        "leaderboard": s.latest_leaderboard(),
        "config_rows": s.config_history(limit=50),
    }


def fmt_money(v):
    if v is None:
        return "—"
    return f"${v:,.2f}"


def fmt_pct(v):
    return "—" if v is None else f"{v * 100:,.1f}%"


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("📊 Polymarket Copy-Signal Tester")
st.caption("Read-only paper-trading measurement tool — it never places real orders.")
st.warning(
    "**Measurement tool, not financial advice.** Leaderboard performance is "
    "backward-looking and survivorship-biased. A strong paper result is a reason "
    "to keep testing, not a guarantee of future returns.",
    icon="⚠️",
)

c_top = st.columns([1, 1, 6])
if c_top[0].button("🔄 Refresh"):
    st.cache_data.clear()
    st.rerun()

data = load_all()
trades = data["trades"]
if not trades and not data["observations"]:
    st.info("No data yet. Run the poller (`python -m poller.main`, or trigger the "
            "GitHub Action) to populate the database, then refresh.")
    st.stop()

perf = analytics.strategy_performance(trades)


# --------------------------------------------------------------------------- #
# 1. Strategy vs control
# --------------------------------------------------------------------------- #
st.header("Overlap strategy vs. control benchmark")
st.caption("Control = naively copy the #1-ranked trader. If overlap doesn't beat it, the tiering adds no value.")


def strategy_card(col, title, m):
    with col:
        st.subheader(title)
        a, b, c = st.columns(3)
        a.metric("Net P&L", fmt_money(m["net_pnl"]))
        b.metric("Realized", fmt_money(m["realized_pnl"]))
        c.metric("Unrealized", fmt_money(m["unrealized_pnl"]))
        a, b, c = st.columns(3)
        a.metric("Open", m["open_count"])
        b.metric("Closed", m["closed_count"])
        c.metric("Win rate", fmt_pct(m["win_rate"]))
        a, b = st.columns(2)
        a.metric("ROI (realized)", fmt_pct(m["roi_realized"]))
        b.metric("ROI (incl. open)", fmt_pct(m["roi_total"]))


cols = st.columns(2)
strategy_card(cols[0], "🟢🔵 Overlap", perf["overlap"])
strategy_card(cols[1], "1️⃣ Control (#1 copy)", perf["control"])


# --------------------------------------------------------------------------- #
# 2. Overlap by tier
# --------------------------------------------------------------------------- #
st.header("Overlap strategy by tier")
st.caption("Does green (all N agree) actually beat blue (≥ blue-threshold agree)?")
tiers = analytics.tier_breakdown(trades)


def tier_row(label, m):
    return {
        "Tier": label, "Open": m["open_count"], "Closed": m["closed_count"],
        "Win rate": fmt_pct(m["win_rate"]), "Realized P&L": fmt_money(m["realized_pnl"]),
        "Unrealized P&L": fmt_money(m["unrealized_pnl"]), "Net P&L": fmt_money(m["net_pnl"]),
        "ROI (realized)": fmt_pct(m["roi_realized"]),
    }


tier_df = pd.DataFrame([tier_row("🟢 green", tiers["green"]), tier_row("🔵 blue", tiers["blue"])])
st.dataframe(tier_df, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
# 3. Open positions (live mark-to-market)
# --------------------------------------------------------------------------- #
st.header("Open paper positions")
open_rows = analytics.open_positions(trades)
if open_rows:
    df = pd.DataFrame([{
        "Strategy": t["strategy"], "Tier": t.get("tier_at_entry"),
        "Market": (t.get("title") or "")[:60], "Outcome": t.get("outcome"),
        "Overlap@entry": t.get("overlap_at_entry"),
        "Entry": round(float(t["entry_price"]), 3),
        "Mark": None if t.get("marked_price") is None else round(float(t["marked_price"]), 3),
        "Shares": round(float(t["shares"]), 1),
        "Unrealized P&L": round(t["unrealized_pnl"], 2),
        "Opened": str(t.get("entry_at"))[:19],
    } for t in open_rows])
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption(f"{len(open_rows)} open · marks updated each poll cycle (every ~30 min).")
else:
    st.info("No open paper positions right now.")


# --------------------------------------------------------------------------- #
# 4. Recent signals
# --------------------------------------------------------------------------- #
st.header("Recent signals")
st.caption("Latest observation per market from the most recent cycle, sorted by overlap.")
sig = analytics.latest_signal_per_market(data["observations"])
if sig:
    df = pd.DataFrame([{
        "Overlap": o.get("overlap"), "Tier": o.get("tier"),
        "Market": (o.get("title") or "")[:60], "Outcome": o.get("outcome"),
        "Price": None if o.get("price") is None else round(float(o["price"]), 3),
        "Liquidity": None if o.get("liquidity") is None else round(float(o["liquidity"])),
        "Closed": o.get("market_closed"),
        "Holders": ", ".join(h for h in (o.get("holder_usernames") or []) if h) or
                   f"{len(o.get('holder_wallets') or [])} wallet(s)",
    } for o in sig])
    st.dataframe(df, hide_index=True, use_container_width=True)
else:
    st.info("No observations yet.")

with st.expander("Current leaderboard cohort (latest snapshot)"):
    lb = data["leaderboard"]
    if lb:
        st.dataframe(pd.DataFrame([{
            "Rank": e.get("rank"), "Trader": e.get("username") or e.get("wallet"),
            "30d P&L": fmt_money(e.get("pnl")), "Volume": fmt_money(e.get("volume")),
            "Wallet": e.get("wallet"),
        } for e in lb]), hide_index=True, use_container_width=True)
    else:
        st.write("No leaderboard snapshot yet.")


# --------------------------------------------------------------------------- #
# 5. Settings editor (forward-only)
# --------------------------------------------------------------------------- #
st.header("⚙️ Settings")
st.caption("Saving writes a NEW timestamped config row. Changes apply only to FUTURE "
           "cycles — past trades are never rewritten.")

cur = Config.from_row(data["config_rows"][0]) if data["config_rows"] else Config()
with st.form("settings"):
    c1, c2, c3 = st.columns(3)
    top_n = c1.number_input("Top N traders", 1, 50, int(cur.top_n))
    window = c2.selectbox("Leaderboard window", ["DAY", "WEEK", "MONTH", "ALL"],
                          index=["DAY", "WEEK", "MONTH", "ALL"].index(cur.leaderboard_window))
    size_threshold = c3.number_input("Min position size", 0.0, value=float(cur.size_threshold))

    c1, c2, c3 = st.columns(3)
    green = c1.number_input("Green tier: overlap ≥", 1, 50, int(cur.tier_green_min),
                            help="Default: all N hold it.")
    blue = c2.number_input("Blue tier: overlap ≥", 1, 50, int(cur.tier_blue_min))
    min_tier = c3.selectbox("Min tier to trade", ["blue", "green"],
                            index=["blue", "green"].index(cur.min_tier_to_trade))

    c1, c2, c3 = st.columns(3)
    min_liq = c1.number_input("Min liquidity (USD)", 0.0, value=float(cur.min_liquidity))
    max_entry = c2.number_input("Max entry price", 0.0, 1.0, float(cur.max_entry_price), step=0.01)
    stake = c3.number_input("Stake per trade (USD)", 1.0, value=float(cur.stake_usd))

    c1, c2, c3 = st.columns(3)
    price_source = c1.selectbox("Price source", ["midpoint", "buy"],
                                index=["midpoint", "buy"].index(cur.price_source))
    control_guard = c2.checkbox("Control respects guardrails", bool(cur.control_respects_guardrails))
    note = c3.text_input("Note (optional)")

    if st.form_submit_button("💾 Save new config (forward-only)"):
        payload = {
            "top_n": int(top_n), "leaderboard_window": window, "size_threshold": float(size_threshold),
            "tier_green_min": int(green), "tier_blue_min": int(blue),
            "min_liquidity": float(min_liq), "max_entry_price": float(max_entry),
            "min_tier_to_trade": min_tier, "stake_usd": float(stake),
            "price_source": price_source, "control_respects_guardrails": bool(control_guard),
            "source": "dashboard", "note": note or None,
        }
        get_store().insert_config(payload)
        st.cache_data.clear()
        st.success("Saved. The next poller cycle will use these settings.")

with st.expander("Config history"):
    if data["config_rows"]:
        st.dataframe(pd.DataFrame(data["config_rows"]), hide_index=True, use_container_width=True)
