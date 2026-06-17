-- ============================================================================
-- Polymarket copy-signal logger + paper-trade tester  —  database schema
-- Target: Supabase (Postgres). Run this once in the Supabase SQL Editor.
--
-- Design notes:
--   * Everything is append-mostly. The poller INSERTs snapshots/observations
--     every cycle and only ever UPDATEs the mutable fields of an OPEN paper
--     trade (its mark + close columns). It never rewrites history.
--   * Config is forward-only: settings changes INSERT a new config_history row.
--     The poller always reads the newest row. Past trades keep the config that
--     was live when they opened (captured on the trade itself where it matters).
--   * "Position identity" = (condition_id, outcome_index), equivalently the
--     CLOB `asset` token id. Overlap is counted across distinct cohort wallets
--     holding the same identity.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. config_history  — forward-only settings. Newest row wins.
-- ----------------------------------------------------------------------------
create table if not exists config_history (
    id                          bigint generated always as identity primary key,
    created_at                  timestamptz not null default now(),
    source                      text not null default 'dashboard',  -- 'default-seed' | 'dashboard'
    note                        text,

    -- cohort / leaderboard
    top_n                       int     not null default 5,
    leaderboard_window          text    not null default 'MONTH',   -- DAY | WEEK | MONTH | ALL
    size_threshold              numeric not null default 1,          -- min position size to count a holding
    poll_interval_minutes       int     not null default 15,         -- how often the poller does real work (5|10|15|30)

    -- tiering (overlap -> tier)
    tier_green_min              int     not null default 5,          -- overlap >= this => green (default: all N)
    tier_blue_min               int     not null default 3,          -- overlap >= this => blue

    -- guardrails before a signal becomes a paper trade
    min_liquidity               numeric not null default 1000,       -- USD; skip illiquid markets
    min_entry_price             numeric not null default 0.05,       -- skip deep longshots (spread eats any win); also the stop price floor
    max_entry_price             numeric not null default 0.85,       -- skip positions near resolution (0.90 caps upside at +11% = bad R/R)
    min_resolve_hours           numeric not null default 2,          -- skip markets resolving within N hours (almost-over games); 0=off
    min_tier_to_trade           text    not null default 'blue',     -- 'blue' | 'green'

    -- paper-trade mechanics
    stake_usd                   numeric not null default 100,        -- fixed $ stake per simulated trade
    price_source                text    not null default 'realistic', -- 'realistic'(ask in/bid out) | 'midpoint' | 'buy'
    control_respects_guardrails boolean not null default true,       -- apply liquidity+max_entry to the #1-copy control too

    -- exit + conflict rules
    stop_loss_pct               numeric not null default 0.30,       -- WIDE backstop (holder-exit is primary); 0=off, 0.30=-30%
    -- fast price-based exits (run EVERY MINUTE in the Worker, not just the scan); overlap-only
    take_profit_pct             numeric not null default 0,          -- bank a gain >= +X%; 0=off
    trailing_stop_pct           numeric not null default 0.15,       -- once armed, exit if price gives back this from its peak; 0=off
    trailing_arm_pct            numeric not null default 0.20,       -- only arm the trailing stop after +X% (locks profit, not a tight stop)
    time_stop_minutes           numeric not null default 30,         -- force-exit N min before resolution (short-fuse safety); 0=off
    fast_exit_slippage_pct      numeric not null default 0.02,       -- extra haircut on a PANIC sell (stop/trailing) — thin book on the way down
    contested_policy            text    not null default 'both',     -- 'both' | 'dominant' | 'skip'

    -- cohort quality (which top earners count toward a signal)
    min_holder_value            numeric not null default 10000,      -- min USD in OPEN positions to count toward overlap
    min_holder_win_ratio        numeric not null default 0.65        -- min fraction of their open positions in profit
);

-- ----------------------------------------------------------------------------
-- 2. cycles  — one row per poller run (heartbeat + audit).
-- ----------------------------------------------------------------------------
create table if not exists cycles (
    id              bigint generated always as identity primary key,
    run_at          timestamptz not null default now(),
    config_id       bigint references config_history(id),
    top_n           int,
    "window"        text,                 -- quoted: WINDOW is a reserved keyword in Postgres
    n_traders       int     default 0,
    n_observations  int     default 0,
    n_signals       int     default 0,   -- observations that passed guardrails this cycle
    opened_overlap  int     default 0,
    opened_control  int     default 0,
    closed          int     default 0,
    status          text    default 'ok',  -- 'ok' | 'degraded' | 'error'
    error           text,
    duration_ms     int
);

-- ----------------------------------------------------------------------------
-- 3. leaderboard_snapshots  — who the top traders were each cycle.
-- ----------------------------------------------------------------------------
create table if not exists leaderboard_snapshots (
    id            bigint generated always as identity primary key,
    cycle_id      bigint not null references cycles(id) on delete cascade,
    captured_at   timestamptz not null default now(),
    "window"      text,                 -- quoted: WINDOW is a reserved keyword in Postgres
    rank          int,
    wallet        text not null,
    username      text,
    pnl           numeric,
    volume        numeric,
    in_cohort     boolean not null default false   -- rank <= top_n
);
create index if not exists ix_lb_cycle  on leaderboard_snapshots(cycle_id);
create index if not exists ix_lb_wallet on leaderboard_snapshots(wallet);

-- ----------------------------------------------------------------------------
-- 4. observations  — EVERY cohort-held position, EVERY cycle (honesty rule #2).
--    This is the empirical record used later to choose the right overlap cutoff.
-- ----------------------------------------------------------------------------
create table if not exists observations (
    id               bigint generated always as identity primary key,
    cycle_id         bigint not null references cycles(id) on delete cascade,
    observed_at      timestamptz not null default now(),

    condition_id     text not null,
    asset            text not null,        -- CLOB token id (market+outcome key)
    outcome          text,
    outcome_index    int,
    title            text,
    slug             text,

    overlap          int  not null,        -- # of distinct cohort wallets holding this outcome
    participants     int,                  -- # of cohort wallets holding ANY outcome of this market
    tier             text not null,        -- 'green' | 'blue' | 'none'
    holder_wallets    text[]    not null default '{}',
    holder_usernames  text[]    not null default '{}',
    holder_ranks      int[]     not null default '{}',
    holder_sizes      numeric[] not null default '{}',   -- each holder's share count (conviction)
    holder_avg_prices numeric[] not null default '{}',   -- each holder's average entry price
    notional          numeric,                           -- total $ the cohort holds on this side

    price            numeric,              -- mark price at this moment (the signal's "now")
    liquidity        numeric,              -- USD liquidity from Gamma
    market_closed    boolean,
    market_active    boolean,
    end_date         timestamptz
);
create index if not exists ix_obs_cycle on observations(cycle_id);
create index if not exists ix_obs_asset on observations(asset);
create index if not exists ix_obs_cond  on observations(condition_id);
create index if not exists ix_obs_time  on observations(observed_at);

-- ----------------------------------------------------------------------------
-- 5. paper_trades  — simulated trades for both strategies. NEVER real orders.
--    Forward-only: entry_price is locked at open and never recomputed.
--    At most one OPEN trade per (strategy, condition_id, outcome_index);
--    re-entry after a close is allowed (the partial unique index permits it).
-- ----------------------------------------------------------------------------
create table if not exists paper_trades (
    id               bigint generated always as identity primary key,
    strategy         text not null,            -- 'overlap' | 'control'
    condition_id     text not null,
    asset            text not null,
    outcome          text,
    outcome_index    int  not null,
    title            text,

    status           text not null default 'OPEN',  -- 'OPEN' | 'CLOSED'

    -- entry (locked forever)
    entry_at         timestamptz not null default now(),
    entry_cycle_id   bigint references cycles(id),
    entry_price      numeric not null,         -- LOCKED at signal time, never backfilled
    stake_usd        numeric not null,
    shares           numeric not null,         -- stake_usd / entry_price
    tier_at_entry    text,
    overlap_at_entry int,
    holders_at_entry text[] default '{}',
    end_date         timestamptz,              -- market resolution time (for the pre-resolution time-stop)

    -- mark-to-market (updated every cycle while OPEN)
    marked_price     numeric,
    marked_at        timestamptz,
    unrealized_pnl   numeric,
    peak_price       numeric,                  -- highest mark since entry (for the trailing stop)

    -- close
    exit_at          timestamptz,
    exit_cycle_id    bigint references cycles(id),
    exit_price       numeric,
    realized_pnl     numeric,
    close_reason     text,                     -- 'resolved'|'cohort_abandoned'|'leader_abandoned'|'holder_exited'|'stop_loss'|'take_profit'|'trailing_stop'|'time_stop'
    resolved_won     boolean,                  -- on resolution: did our outcome win

    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

-- The core invariant: only one OPEN trade per (strategy, market, outcome).
-- Partial index => CLOSED rows don't block re-entry.
create unique index if not exists uq_open_trade
    on paper_trades(strategy, condition_id, outcome_index)
    where status = 'OPEN';

create index if not exists ix_pt_strategy on paper_trades(strategy);
create index if not exists ix_pt_status   on paper_trades(status);
create index if not exists ix_pt_asset    on paper_trades(asset);

-- ----------------------------------------------------------------------------
-- 6. kv_store  — accumulated dashboard trackers as JSON blobs, mirroring the
--    file-store's state.json: the consensus-resolution watch, the per-cycle
--    history series, per-trader pnl sparklines, the agreement summary, and the
--    latest per-trader snapshot. The poller reads-modifies-writes these each
--    cycle; the dashboard payload is built from them. (The relational tables
--    above stay the queryable empirical record.)
-- ----------------------------------------------------------------------------
create table if not exists kv_store (
    key         text primary key,   -- consensus_watch | history | trader_series | agreement | latest_traders
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 7. forward migrations — idempotent ALTERs so re-running this file on an
--    EXISTING database adds new columns without a wipe. (CREATE-only above is
--    skipped on existing tables, so new columns must be added here.)
-- ----------------------------------------------------------------------------
-- fast price-based exits (every-minute Worker exits + trailing/time stops)
alter table config_history add column if not exists take_profit_pct        numeric not null default 0;
alter table config_history add column if not exists trailing_stop_pct       numeric not null default 0.15;
alter table config_history add column if not exists trailing_arm_pct        numeric not null default 0.20;
alter table config_history add column if not exists time_stop_minutes       numeric not null default 30;
alter table config_history add column if not exists fast_exit_slippage_pct  numeric not null default 0.02;
alter table paper_trades  add column if not exists end_date   timestamptz;   -- for the pre-resolution time-stop
alter table paper_trades  add column if not exists peak_price numeric;       -- for the trailing stop
