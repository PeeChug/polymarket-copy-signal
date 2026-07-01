-- ============================================================================
-- Cloudflare D1 (SQLite) schema — the Cloudflare-native store, mirroring
-- sql/schema.sql (Postgres). Dialect notes:
--   serial/identity  -> INTEGER PRIMARY KEY AUTOINCREMENT
--   bigint/int       -> INTEGER      numeric -> REAL      boolean -> 0/1
--   timestamptz      -> TEXT (ISO 8601 strings; the app passes them explicitly)
--   jsonb / text[]   -> TEXT holding JSON (arrays + kv blobs serialized by the app)
-- Idempotent (IF NOT EXISTS) so re-applying is safe.
-- ============================================================================

CREATE TABLE IF NOT EXISTS config_history (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source                      TEXT DEFAULT 'dashboard',
  note                        TEXT,
  top_n                       INTEGER DEFAULT 5,
  candidate_pool              INTEGER DEFAULT 400,
  leaderboard_window          TEXT DEFAULT 'MONTH',
  size_threshold              REAL DEFAULT 1,
  poll_interval_minutes       INTEGER DEFAULT 15,
  tier_green_min              INTEGER DEFAULT 5,
  tier_blue_min               INTEGER DEFAULT 3,
  tier_green_frac             REAL DEFAULT 0.14,
  tier_blue_frac              REAL DEFAULT 0.10,
  min_liquidity               REAL DEFAULT 1000,
  min_entry_price             REAL DEFAULT 0.05,
  max_entry_price             REAL DEFAULT 0.85,
  skip_band_lo                REAL DEFAULT 0,
  skip_band_hi                REAL DEFAULT 0,
  min_resolve_hours           REAL DEFAULT 24,
  max_resolve_hours           REAL DEFAULT 0,
  min_tier_to_trade           TEXT DEFAULT 'blue',
  stake_usd                   REAL DEFAULT 100,
  price_source                TEXT DEFAULT 'realistic',
  control_respects_guardrails INTEGER DEFAULT 1,
  stop_loss_pct               REAL DEFAULT 0.30,
  take_profit_pct             REAL DEFAULT 0,
  trailing_stop_pct           REAL DEFAULT 0.15,
  trailing_arm_pct            REAL DEFAULT 0.10,
  time_stop_minutes           REAL DEFAULT 30,
  fast_exit_slippage_pct      REAL DEFAULT 0.02,
  reentry_cooldown_hours      REAL DEFAULT 24,
  contested_policy            TEXT DEFAULT 'both',
  min_holder_value            REAL DEFAULT 10000,
  min_holder_win_ratio        REAL DEFAULT 0.5,
  cohort_grace_hours          REAL DEFAULT 48
);

CREATE TABLE IF NOT EXISTS cycles (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  config_id      INTEGER,
  top_n          INTEGER,
  window         TEXT,
  n_traders      INTEGER DEFAULT 0,
  n_observations INTEGER DEFAULT 0,
  n_signals      INTEGER DEFAULT 0,
  opened_overlap INTEGER DEFAULT 0,
  opened_control INTEGER DEFAULT 0,
  closed         INTEGER DEFAULT 0,
  status         TEXT DEFAULT 'ok',
  error          TEXT,
  duration_ms    INTEGER
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id    INTEGER,
  captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  window      TEXT,
  rank        INTEGER,
  wallet      TEXT,
  username    TEXT,
  pnl         REAL,
  volume      REAL,
  in_cohort   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id          INTEGER,
  observed_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  condition_id      TEXT,
  asset             TEXT,
  outcome           TEXT,
  outcome_index     INTEGER,
  title             TEXT,
  slug              TEXT,
  overlap           INTEGER,
  participants      INTEGER,
  tier              TEXT,
  holder_wallets    TEXT DEFAULT '[]',
  holder_usernames  TEXT DEFAULT '[]',
  holder_ranks      TEXT DEFAULT '[]',
  price             REAL,
  liquidity         REAL,
  market_closed     INTEGER,
  market_active     INTEGER,
  end_date          TEXT,
  holder_sizes      TEXT DEFAULT '[]',
  holder_avg_prices TEXT DEFAULT '[]',
  notional          REAL
);

CREATE TABLE IF NOT EXISTS paper_trades (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy         TEXT,
  condition_id     TEXT,
  asset            TEXT,
  outcome          TEXT,
  outcome_index    INTEGER,
  title            TEXT,
  status           TEXT DEFAULT 'OPEN',
  entry_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  entry_cycle_id   INTEGER,
  entry_price      REAL,
  stake_usd        REAL,
  shares           REAL,
  tier_at_entry    TEXT,
  overlap_at_entry INTEGER,
  holders_at_entry TEXT DEFAULT '[]',
  marked_price     REAL,
  marked_at        TEXT,
  unrealized_pnl   REAL,
  exit_at          TEXT,
  exit_cycle_id    INTEGER,
  exit_price       REAL,
  realized_pnl     REAL,
  close_reason     TEXT,
  resolved_won     INTEGER,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  end_date         TEXT,
  peak_price       REAL
);

-- one OPEN trade per (strategy, market, outcome); CLOSED rows don't block re-entry
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_trade ON paper_trades(strategy, condition_id, outcome_index) WHERE status='OPEN';
CREATE INDEX IF NOT EXISTS ix_pt_strategy ON paper_trades(strategy);
CREATE INDEX IF NOT EXISTS ix_pt_status   ON paper_trades(status);
CREATE INDEX IF NOT EXISTS ix_pt_asset    ON paper_trades(asset);
CREATE INDEX IF NOT EXISTS ix_obs_observed ON observations(observed_at);
CREATE INDEX IF NOT EXISTS ix_lb_captured  ON leaderboard_snapshots(captured_at);

-- accumulated dashboard trackers (consensus_watch/history/agreement/... as JSON text)
CREATE TABLE IF NOT EXISTS kv_store (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- the precomputed dashboard payload (data.json), served by the Worker at /data.json
-- so the browser never egresses from Supabase.
CREATE TABLE IF NOT EXISTS site_blob (
  name       TEXT PRIMARY KEY,
  body       TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
