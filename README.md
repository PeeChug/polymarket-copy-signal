# Polymarket Copy-Signal Tester

A **read-only** system that tests one hypothesis: *when several of the best
30-day traders on Polymarket independently hold the same position, is that
position a good buy?* It detects those overlaps, **paper-trades** them
automatically, and measures whether the strategy actually works — alongside a
naive control benchmark — so you can decide whether it's worth real money.

> ## 🚫 It never places real orders.
> It only reads public Polymarket data and simulates trades in plain repo files.
> There is no order-placement code anywhere, no wallet key, no execution path.
> This is a **measurement tool, not financial advice** (see [Caveats](#caveats)).

---

## What it does

- Watches the **top N earners** on Polymarket's profit leaderboard (the 30-day
  "monthly winners"; `N` configurable, default **50** — the API max).
- Each cycle, **compares every position they hold against each other** and counts
  how many independently hold the **same position** (same market + same outcome)
  — the **overlap** / agreement. The dashboard also shows **overlap ÷
  participants** — how many hold *any* side of that market — e.g. `4/5` means 4 of
  the 5 cohort traders in that market are on the same outcome (a conviction lens).
- Turns agreement into **tiers** (absolute overlap): with 50 earners, `green` =
  **≥10 agree**, `blue` = **≥5 agree**. Only green/blue positions are candidates to
  paper-trade. (All thresholds are configurable in `config.yaml`.)
- Paper-trades qualifying signals and tracks simulated P&L over time, so you can
  measure whether copying the consensus actually pays.

### Three rules that keep the test honest

1. **Forward-only paper trades.** A simulated trade enters at the market price
   available the *moment the signal first qualifies*. That entry price is locked
   forever — never backfilled or recalculated. (`entry_price` is written once and
   only mark/close columns are ever updated.)
2. **Log everything, not just winners.** Every position the cohort holds is
   recorded every cycle in `observations` — not only the ones that cross a tier
   threshold — so you can later measure empirically what the right overlap cutoff
   is instead of guessing.
3. **Run a control benchmark.** Alongside the tiered "overlap" strategy, a naive
   `control` strategy just copies the **#1-ranked** trader's positions. Both
   strategies share the same execution mechanics and tradeability guardrails; the
   *only* difference is the selection rule. If overlap doesn't beat control, the
   tiering adds no value — and you learn that for free.

### Guardrails before a signal becomes a paper trade (all configurable)

| Guardrail | Default | Purpose |
|---|---|---|
| `min_liquidity` | `$1,000` | Don't simulate filling a market we couldn't actually trade. |
| `max_entry_price` | `0.90` | Skip positions already near resolution with little upside. |
| `min_tier_to_trade` | `blue` | Minimum tier required to open an overlap trade. |
| `stake_usd` | `$100` | Fixed dollar stake per simulated trade. |

A trade closes when the **market resolves**, or (overlap) when **none of the
cohort still holds it**, or (control) when the **#1 trader drops it**.

### P&L model (binary outcome share)

A fixed `stake_usd` buys `shares = stake / entry_price` at the locked entry
price. Each share is worth the current price (0–1); on resolution a winning
share is worth `1.0`, a loser `0.0`.

```
unrealized P&L = shares × (mark  − entry)
realized   P&L = shares × (exit  − entry)
```

---

## Architecture — the $0 stack

Live deployment: **https://peechug.github.io/polymarket-copy-signal/**

Everything runs on GitHub alone — no database, no server, no third-party
accounts, no secrets:

```
   ┌───────────────────────────┐
   │   Polymarket public APIs   │  (read-only, no auth)
   │  data-api / clob / gamma   │
   └─────────────┬─────────────┘
                 │ reads leaderboard, positions, prices, status
                 ▼
   ┌──────────────────────────────┐   commits JSON   ┌────────────────────┐
   │  Poller  (GitHub Actions cron │ ───────────────▶ │  repo files         │
   │  every 30 min, no secrets)    │   back to repo   │  data/*.json(l)     │
   └──────────────────────────────┘                  │  docs/data.json     │
                                                      └─────────┬──────────┘
                                                  push triggers │
                                                                ▼
                                          ┌──────────────────────────────┐
                                          │  GitHub Pages (free)          │
                                          │  static dashboard at /docs    │
                                          └──────────────────────────────┘
```

- **Poller** — a scheduled GitHub Action. Fetches the leaderboard and positions,
  computes overlap, logs observations, opens/closes paper trades, writes the
  results to JSON files, and **commits them back to the repo**. Minimal deps
  (`requests`, `PyYAML`). No secrets, because all data is public.
- **Store** — plain JSON files in the repo (`core/store.py: FileStore`). No
  hosted database. `data/state.json` is the source of truth; every observation
  is also appended to `data/observations.jsonl` (the empirical record).
- **Dashboard** — a static page (`docs/index.html`) served free by GitHub Pages.
  Each poll commits a precomputed `docs/data.json`; pushing it auto-rebuilds the
  site. It's a pure render-from-JSON page (the poller does the aggregation with
  the same tested code).

> **Optional alternative backend.** A Supabase + Streamlit path also ships in the
> repo (`PostgrestStore`, `dashboard/app.py`, `sql/schema.sql`) for a private
> deployment with a live settings editor. If you set `SUPABASE_URL`/`SUPABASE_KEY`
> as Actions secrets, the poller automatically uses Postgres instead of files.
> See [Optional: private Supabase + Streamlit](#optional-private-supabase--streamlit).

### Repo layout

```
config.yaml                  # live settings (edit + commit = forward-only change)
docs/
  index.html                 # the static GitHub Pages dashboard
  data.json                  # precomputed payload (written by the poller each cycle)
data/                        # the file "database" (committed by the poller)
  state.json                 #   trades + config history + counters (source of truth)
  observations.jsonl         #   append-only log of EVERY observation (honesty rule #2)
core/
  config.py                  # Config model + forward-only loader / yaml sync
  store.py                   # FileStore (default) + PostgrestStore + MemoryStore
  analytics.py               # pure aggregation, incl. the dashboard payload (tested)
poller/
  requirements.txt           # POLLER deps (minimal: requests, PyYAML)
  polymarket.py              # ⭐ the ONLY file that knows the raw API shape
  strategy.py                # pure overlap/tiering/guardrails/P&L (tested)
  engine.py                  # one run_cycle(): the whole job
  publish.py                 # writes docs/data.json for the static dashboard
  main.py                    # entry point; --dry-run; picks the store backend
  alerts.py                  # Telegram alert on new green/blue trade (opt-in via TELEGRAM_* secrets)
scripts/make_demo_data.py    # generates sample data for the Streamlit demo mode
dashboard/app.py             # optional Streamlit dashboard (Supabase path)
requirements.txt             # optional Streamlit-dashboard deps (repo root)
sql/schema.sql               # optional Supabase table definitions
tests/                       # unittest suite (no network/DB needed)
.github/workflows/poller.yml # 30-min cron + manual trigger; commits data back
```

The whole app depends only on the **normalized** output of
[`poller/polymarket.py`](poller/polymarket.py). If Polymarket changes an endpoint
or field name, that one file is the only thing to fix.

---

## ⚠️ Verify the Polymarket endpoints first

Polymarket's endpoint paths and field names drift between versions. The contract
below was **confirmed against the live APIs on 2026-06-15** (and live probing
caught three things the docs/memory got wrong — noted with ⚠️). Before relying on
output, re-confirm at <https://docs.polymarket.com> and by running the self-test:

```bash
python -m poller.polymarket --selftest   # hits the live APIs end-to-end
```

| Need | Endpoint (verified) | Key fields |
|---|---|---|
| Profit leaderboard | `GET data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=N` | `rank` ⚠️*(string)*, `proxyWallet`, `userName`, `pnl`, `vol` |
| Wallet positions | `GET data-api.polymarket.com/positions?user=0x…` | `conditionId`, `asset`, `outcome`, `outcomeIndex`, `size`, `avgPrice`, `curPrice`, `redeemable` |
| Outcome price | `GET clob.polymarket.com/price?token_id=…&side=BUY` → `{"price":"…"}`  ·  `…/midpoint` → `{"mid":"…"}` ⚠️ *(field is `mid`, not `mid_price`; 404 = resolved market)* |
| Market status + liquidity | `GET gamma-api.polymarket.com/markets?condition_ids=0x…` | `closed`, `active`, `liquidityNum`, `outcomes`, `clobTokenIds`, `umaResolutionStatuses` ⚠️*(plural)*, `outcomePrices` |

`timePeriod` accepts `DAY` / `WEEK` / `MONTH` / `ALL` (MONTH = the 30-day window).
Gamma returns `outcomes` / `clobTokenIds` / `outcomePrices` as **JSON-encoded
strings** (parse them) and does **not** serve every archived market (tolerate a
miss). All of this is handled in `polymarket.py`.

---

## Setup

**The default (GitHub Pages) deployment needs no setup beyond the repo itself** —
the Action runs on a schedule, commits data, and Pages serves the dashboard. If
you cloned this fresh, the one-time enablement is:

1. Push to GitHub (public repo, so free Pages can serve it).
2. **Settings → Pages → Build and deployment → Deploy from a branch → `main` / `/docs`.**
   (Or via the CLI: `gh api -X POST repos/OWNER/REPO/pages -f 'source[branch]=main' -f 'source[path]=/docs'`.)
3. **Actions → poller → Run workflow** to populate data immediately (otherwise wait
   for the next 30-min cron tick). The dashboard is then live at
   `https://OWNER.github.io/REPO/`.

That's it — `$0`, no database, no secrets.

### Local check first (recommended)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r poller/requirements.txt
python -m unittest discover -s tests -t .   # 27 tests, no network
python -m poller.polymarket --selftest      # confirm live Polymarket endpoints
python -m poller.main --dry-run             # full cycle, live data, writes NOTHING
python -m poller.main                        # real cycle -> writes data/ + docs/data.json locally
```

Preview the static dashboard locally: `cd docs && python3 -m http.server` then open
<http://localhost:8000>.

---

## Configuration is forward-only

`config.yaml` is the live settings file. **To change a setting, edit `config.yaml`
and commit it.** On the next cycle the poller notices the change and appends a new
timestamped row to the config history (`data/state.json` → `config_history`), then
uses it. Changes therefore apply **only to future cycles** and never rewrite past
trades — git history *is* the forward-only audit trail. The dashboard shows the
current settings read-only.

(In the optional Supabase path, the Streamlit **Settings** editor does the same
thing by inserting a new `config_history` row instead of editing `config.yaml`.)

| Setting | Default | Meaning |
|---|---|---|
| `top_n` | 50 | Number of leaderboard earners in the cohort (API max 50). |
| `leaderboard_window` | `MONTH` | `DAY`/`WEEK`/`MONTH`/`ALL`. |
| `poll_interval_minutes` | 15 | How often the poller actually works (5/10/15/30); the Action wakes every 5 min. |
| `size_threshold` | 1 | Min position size to count a holding (ignore dust). |
| `tier_green_min` | 10 | Overlap ≥ this → green (strong). |
| `tier_blue_min` | 5 | Overlap ≥ this → blue (moderate). |
| `min_liquidity` | 1000 | USD liquidity floor. |
| `max_entry_price` | 0.90 | Skip near-resolution positions. |
| `min_tier_to_trade` | `blue` | `blue` (trade blue+green) or `green` (green only). |
| `stake_usd` | 100 | Fixed stake per simulated trade. |
| `price_source` | `midpoint` | `midpoint` or `buy` (best ask). |
| `control_respects_guardrails` | `true` | Apply liquidity + max-entry to control too. |

---

## What the dashboard shows

A tabbed single-page app (no build step, no JS dependencies) — vanilla
HTML/CSS/JS rendering the precomputed `docs/data.json`, with count-up numbers,
sparklines, hand-rolled SVG charts, a live pulse, and auto-refresh every ~2.5 min:

- **Top Earners** *(default)* — a card per cohort trader, with **sort** (rank /
  profit / volume / positions / open P&L / shared), a **show-count** selector
  (12 / 25 / 50 / all), name search, and verified/shared filters. Each card: 30-day
  profit (+ **sparkline**), volume, positions, value "on the table", live open P&L,
  cohort overlap, expandable top positions (each shown as `overlap/participants`
  and linking to its market), and a **"View on Polymarket"** profile link.
- **Consensus** — the agreement table showing **overlap/participants** (e.g. 4/5)
  with conviction bars, plus a **min-agreement slider**, market search, liquidity
  filter, and sortable columns; the agreement distribution; an **agreement-over-
  time** chart; and the **consensus hit-rate** panel — win-rate of consensus
  positions once they resolve, by agreement bucket (the core hypothesis test).
- **Performance** — overlap vs. #1-copy control, a **cumulative-P&L-over-time**
  equity curve, the green-vs-blue tier breakdown, and open paper positions.
- **All Signals** — every observed position this cycle (honesty rule #2), with
  **search, tier filter, and sortable columns**.

The poller is fast: it **batches** the CLOB price calls (`POST /midpoints`) and
Gamma market lookups (repeated `condition_ids`) and fetches the cohort's positions
concurrently, so a full cycle over ~220 positions runs in ~2 seconds. It also
accumulates time-series (`data/history.jsonl`) and a consensus-resolution tracker
to power the charts and hit-rate.

---

## Cost target: $0 / month

Everything is free: GitHub Actions (a ~10-second run every 30 min ≈ 1,440 runs/
month, well within free minutes) and GitHub Pages hosting. No database bill, no
host bill, no secrets to leak. The trade-off is that the published repo is
**public** (there are no secrets to expose — only public Polymarket data and
simulated trades).

> **Faster polling** (e.g. every minute) would exceed free Actions minutes and
> need a small **~$5/month always-on worker** (Fly.io / Railway / a tiny VPS
> running `poller/main.py` on a loop). Not needed for this test — 30-minute
> cadence is plenty to measure a multi-day/week strategy.

Note: the poller commits once per cycle, so the repo accumulates ~48 small
commits/day. That's by design (the commits *are* the database) and is free.

---

## Security

The default (file/Pages) deployment has **no secrets at all** — the poller only
reads public Polymarket data and writes public files, so there is nothing
sensitive to protect. The repo is public by necessity (free Pages); that exposes
your code and paper-trade results, but no keys and no money.

If you switch to the optional Supabase backend, treat the `service_role` key as a
secret: keep it only in GitHub Actions secrets (and Streamlit secrets if you use
that dashboard). Should you ever expose that dashboard publicly, enable
Row-Level Security, use the read-only `anon` key for the dashboard, and keep the
write-capable `service_role` key only in the scheduled job.

---

## Optional: private Supabase + Streamlit

If you'd rather keep everything private (and don't mind a few minutes of setup
plus signing in to two free services), the repo also supports a Postgres +
Streamlit deployment:

1. **Supabase** — create a free project, run [`sql/schema.sql`](sql/schema.sql) in
   the SQL Editor, and copy the Project URL + `service_role` key.
2. **GitHub** — add `SUPABASE_URL` and `SUPABASE_KEY` as Actions secrets. The
   poller detects them and writes to Postgres instead of files (the Pages site
   then just stops updating; harmless). You can also make the repo private.
3. **Streamlit Community Cloud** — deploy with main file `dashboard/app.py` and the
   same two secrets (TOML). With no secrets it shows a bundled demo;
   `streamlit run dashboard/app.py` works locally too.

The Streamlit dashboard adds a live, writeable **Settings** editor (it inserts a
new forward-only `config_history` row).

---

## Extending later (out of scope now)

No real trading, order placement, or automated execution — by design.

**Telegram alerts** are implemented in [`poller/alerts.py`](poller/alerts.py) and
fire when a new green/blue overlap paper trade opens — but only if you opt in by
adding `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Actions secrets. With no
secrets set it stays a pure no-op (zero extra deps), so nothing changes until you
enable it.

---

## Caveats

- **This is a measurement tool, not financial advice.**
- **Leaderboard performance is backward-looking and survivorship-biased** —
  you're watching whoever happens to be winning *right now*, which over-represents
  luck and risk-taking. A strong paper-trade result is a reason to **keep
  testing**, not a guarantee of future returns.
- Paper P&L assumes you could fill at the observed price for your stake; the
  liquidity guardrail mitigates but doesn't eliminate this.
- The top traders are whales with idiosyncratic books — on many cycles they
  share *few* positions, so green/blue signals can be rare. That's expected, and
  exactly why rule #2 (log everything) matters: the `observations` table lets you
  measure the real overlap distribution before committing to a cutoff.

---

## Development

```bash
python -m unittest discover -s tests -t .   # run the suite
python -m poller.main --dry-run --dump out.json   # inspect a full live cycle
```

`strategy.py`, `analytics.py`, and the engine lifecycle are covered by tests that
need no network or database (they use the in-memory store and a scripted client).
