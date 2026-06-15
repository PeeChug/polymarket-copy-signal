# Polymarket Copy-Signal Tester

A **read-only** system that tests one hypothesis: *when several of the best
30-day traders on Polymarket independently hold the same position, is that
position a good buy?* It detects those overlaps, **paper-trades** them
automatically, and measures whether the strategy actually works — alongside a
naive control benchmark — so you can decide whether it's worth real money.

> ## 🚫 It never places real orders.
> It only reads public Polymarket data and simulates trades in a database.
> There is no order-placement code anywhere, no wallet key, no execution path.
> This is a **measurement tool, not financial advice** (see [Caveats](#caveats)).

---

## What it does

- Watches the **top N traders** on Polymarket's profit leaderboard (the 30-day
  "monthly winners"; `N` configurable, default 5).
- Each cycle, counts how many of them independently hold the **same position**
  (same market + same outcome) — the **overlap**.
- Turns overlap into **tiers**: `green` = all N hold it, `blue` = at least 3.
  Only green/blue positions are candidates to paper-trade.
- Paper-trades qualifying signals and tracks simulated P&L over time.

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

Two parts on different schedules that talk **only** through a shared database.

```
                          ┌───────────────────────────┐
   GitHub Actions cron    │   Polymarket public APIs   │
   (every 30 min)         │  data-api / clob / gamma   │  (read-only, no auth)
        │                 └───────────────────────────┘
        │  python -m poller.main          ▲
        ▼                                  │ reads leaderboard, positions,
   ┌─────────┐   writes snapshots,         │ prices, market status
   │ Poller  │───observations, trades──┐   │
   └─────────┘                         │   │
                                       ▼   │
                         ┌──────────────────────────┐
                         │  Supabase Postgres (free) │
                         └──────────────────────────┘
                                       ▲
                          reads (+ writes config rows)
                                       │
                                  ┌─────────┐
                                  │Streamlit│  (Community Cloud, free)
                                  │dashboard│
                                  └─────────┘
```

- **Poller** — a scheduled job (GitHub Actions). Fetches the leaderboard and
  positions, computes overlap, logs observations, opens/closes paper trades.
  Minimal deps (`requests`, `PyYAML`) so it installs fast.
- **Database** — hosted Postgres (Supabase free tier). Stores leaderboard
  snapshots, every observation, paper trades, and config history.
- **Dashboard** — a Streamlit app (Streamlit Community Cloud free tier) that
  reads results and edits settings.

### Repo layout

```
config.yaml                  # default settings (only seeds an EMPTY database)
requirements.txt             # DASHBOARD deps (repo root, for Streamlit Cloud)
sql/schema.sql               # run once in Supabase to create the tables
core/
  config.py                  # Config model + forward-only loader
  store.py                   # PostgrestStore (Supabase) + MemoryStore (dry-run/tests)
  analytics.py               # pure aggregation for the dashboard (tested)
poller/
  requirements.txt           # POLLER deps (minimal)
  polymarket.py              # ⭐ the ONLY file that knows the raw API shape
  strategy.py                # pure overlap/tiering/guardrails/P&L (tested)
  engine.py                  # one run_cycle(): the whole job
  main.py                    # entry point; supports --dry-run
  alerts.py                  # alert SEAM (intentionally a no-op for now)
dashboard/app.py             # Streamlit dashboard
tests/                       # unittest suite (no network/DB needed)
.github/workflows/poller.yml # 30-min cron + manual trigger
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

### 0. Local check first (recommended, no accounts needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r poller/requirements.txt
python -m unittest discover -s tests -t .   # 24 tests, no network
python -m poller.polymarket --selftest      # confirm live endpoints
python -m poller.main --dry-run             # full cycle, live data, writes NOTHING
```

### 1. Supabase (database)

1. Create a free project at <https://supabase.com>.
2. Open **SQL Editor** → paste the contents of [`sql/schema.sql`](sql/schema.sql)
   → **Run**. This creates all tables and the one-open-trade-per-market index.
3. **Project Settings → API**: copy the **Project URL** and the **`service_role`**
   key. (See the [security note](#security) before exposing the dashboard.)

### 2. GitHub (the poller)

1. Push this repo to a **private** GitHub repo.
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `SUPABASE_URL` — your project URL
   - `SUPABASE_KEY` — the `service_role` key
3. The workflow (`.github/workflows/poller.yml`) runs every 30 min automatically.
   To run on demand: **Actions → poller → Run workflow** (it has a manual trigger
   and an optional *dry-run* checkbox).

> Scheduled workflows only run from the **default branch**, and GitHub may delay
> a cron tick under load. The first scheduled run can take up to ~30 min to appear
> — use the manual trigger to seed data immediately.

### 3. Streamlit Community Cloud (the dashboard)

1. Go to <https://share.streamlit.io>, **New app**, point it at this repo.
2. Set **Main file path** to `dashboard/app.py`.
3. In **Advanced settings → Secrets**, add (TOML):
   ```toml
   SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
   SUPABASE_KEY = "your-service-role-key"
   ```
4. Deploy. Streamlit Cloud auto-installs the repo-root `requirements.txt`.

### 4. Verify, then trust the output

Trigger the workflow once (or run `--dry-run` locally), open the dashboard, and
confirm the leaderboard cohort and recent signals look sane **before** drawing
conclusions from the paper-trade results.

---

## Configuration is forward-only

`config.yaml` holds sensible defaults, but it **only seeds the first config row
when the database is empty**. After that, the database is the source of truth.

The dashboard's **Settings** editor changes settings by **inserting a new
timestamped `config_history` row** — it never edits an existing one. The poller
always reads the newest row, so changes apply **only to future cycles** and never
rewrite past trades. To audit what was live when, read `config_history` (every
cycle records the `config_id` it used).

| Setting | Default | Meaning |
|---|---|---|
| `top_n` | 5 | Number of leaderboard traders in the cohort. |
| `leaderboard_window` | `MONTH` | `DAY`/`WEEK`/`MONTH`/`ALL`. |
| `size_threshold` | 1 | Min position size to count a holding (ignore dust). |
| `tier_green_min` | 5 | Overlap ≥ this → green (default: all N). |
| `tier_blue_min` | 3 | Overlap ≥ this → blue. |
| `min_liquidity` | 1000 | USD liquidity floor. |
| `max_entry_price` | 0.90 | Skip near-resolution positions. |
| `min_tier_to_trade` | `blue` | `blue` (trade blue+green) or `green` (green only). |
| `stake_usd` | 100 | Fixed stake per simulated trade. |
| `price_source` | `midpoint` | `midpoint` or `buy` (best ask). |
| `control_respects_guardrails` | `true` | Apply liquidity + max-entry to control too. |

---

## What the dashboard shows

- **Overlap vs. control** — open/closed counts, win rate, realized & unrealized
  P&L, and ROI, side by side.
- **Overlap by tier** — does green beat blue?
- **Open paper positions** — with live mark-to-market P&L (marks refresh each cycle).
- **Recent signals** — latest observation per market, sorted by overlap.
- **Settings editor** — saves a new forward-only config row.

---

## Cost target: $0 / month

A private repo polling every 30 minutes (~1,440 short runs/month) stays within
GitHub's free Actions minutes, and Supabase + Streamlit Community Cloud free tiers
cover the rest.

> **Faster polling** (e.g. every minute) would exceed free Actions minutes and
> need a small **~$5/month always-on worker** (Fly.io / Railway / a tiny VPS
> running `poller/main.py` on a loop). Not needed for this test — 30-minute
> cadence is plenty to measure a multi-day/week strategy.

---

## Security

Using the `service_role` key is fine for a **private, personal** tool: it lives
only in GitHub Actions secrets and Streamlit secrets, and the repo is private.

**If you ever make the dashboard public**, do not ship it the `service_role`
key. Instead:

1. Enable **Row-Level Security** on the tables and add read-only policies.
2. Create/use the Supabase **`anon`** key for the dashboard (read-only).
3. Keep the write-capable `service_role` key **only** in the GitHub Actions
   secret used by the poller.

That way the write key never leaves the scheduled job.

---

## Extending later (out of scope now)

No real trading, order placement, or automated execution — by design. A
phone/Telegram alert layer may come later: the clean seam already exists at
[`poller/alerts.py`](poller/alerts.py) → `notify_trade_opened(trade, cfg)`, which
the engine calls once whenever a new green/blue overlap trade opens. It's a no-op
today (zero extra deps/secrets); wiring up Telegram is a one-function change.

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
