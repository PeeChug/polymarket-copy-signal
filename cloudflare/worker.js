// Cloudflare Worker — the Cloudflare-native control plane (D1-backed):
//   1. FULL SCAN (every 5 min): fire the GitHub Actions poller (heavy Python job —
//      leaderboard, cohort, consensus, open/close). Too big for a Worker, so
//      GitHub owns it.
//   2. FAST MARK (every 1 min): re-price the OPEN paper trades via one batched
//      CLOB /prices call, run the fast price/time exits, and write marks.json into
//      D1 (site_blob) — the dashboard's P&L refreshes every minute.
//   3. Serve GET /data.json and /marks.json from D1 — so the browser fetches from
//      the Worker (free egress), never from Supabase.
//   4. Accept paper-trade config (POST /config) + wallet policy (POST /wallet),
//      written to D1.
//
// Binding: DB (D1).  Secret: GH_PAT (GitHub Actions: write, fires the scan).
// No Supabase — D1 is priced by rows, not bandwidth, so there is no egress bill.

const DISPATCH_URL =
  "https://api.github.com/repos/PeeChug/polymarket-copy-signal/actions/workflows/poller.yml/dispatches";

const CLOB_PRICES = "https://clob.polymarket.com/prices";   // batch price endpoint (500 req/10s)
const FULL_SCAN_CRON = "*/5 * * * *";                        // GitHub heavy scan cadence; MUST match wrangler.toml

// The ONLY columns the dashboard may write into config_history. Anything else in
// the request body is dropped, so a stray/hostile field can never land.
const ALLOWED = [
  "top_n", "candidate_pool", "leaderboard_window", "size_threshold", "poll_interval_minutes",
  "tier_green_min", "tier_blue_min", "tier_green_frac", "tier_blue_frac",
  "min_liquidity", "min_entry_price", "max_entry_price", "min_resolve_hours",
  "min_tier_to_trade", "stake_usd", "price_source", "control_respects_guardrails",
  "stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trailing_arm_pct",
  "time_stop_minutes", "fast_exit_slippage_pct", "reentry_cooldown_hours", "contested_policy",
  "min_holder_value", "min_holder_win_ratio", "cohort_grace_hours",
  "max_resolve_hours", "skip_band_lo", "skip_band_hi",
];

// Defaults the fast-mark falls back to when a config column doesn't exist yet.
// MUST mirror core/config.py's Config dataclass defaults.
const EXIT_DEFAULTS = {
  stop_loss_pct: 0.30, min_entry_price: 0.05, take_profit_pct: 0.0,
  trailing_stop_pct: 0.15, trailing_arm_pct: 0.10, time_stop_minutes: 30,
  fast_exit_slippage_pct: 0.02,
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });

// D1 bind conversion: booleans -> 0/1, objects/arrays -> JSON text (mirrors D1Store._bind).
const bindVal = (v) =>
  typeof v === "boolean" ? (v ? 1 : 0) : (v != null && typeof v === "object" ? JSON.stringify(v) : v);

async function poke(env) {
  return fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + env.GH_PAT,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "cf-worker-poller-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
}

// Append a new (forward-only) config_history row from the dashboard, into D1.
async function saveConfig(request, env) {
  if (!env.DB) return json({ error: "Worker is missing the D1 binding." }, 500);
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "Body must be JSON." }, 400); }

  const row = {};
  for (const k of ALLOWED) if (body[k] !== undefined && body[k] !== null) row[k] = body[k];
  if (Object.keys(row).length === 0) return json({ error: "No recognized config fields." }, 400);

  // Don't trust the client: re-clamp the risky knobs and re-check the tier order.
  if (row.stop_loss_pct !== undefined) row.stop_loss_pct = Math.min(0.95, Math.max(0, +row.stop_loss_pct || 0));
  if (row.stake_usd !== undefined) row.stake_usd = Math.max(1, Math.round(+row.stake_usd || 100));
  if (row.candidate_pool !== undefined) row.candidate_pool = Math.min(1000, Math.max(10, Math.round(+row.candidate_pool || 400)));
  if (row.cohort_grace_hours !== undefined) row.cohort_grace_hours = Math.min(720, Math.max(0, +row.cohort_grace_hours || 0));
  if (row.reentry_cooldown_hours !== undefined) row.reentry_cooldown_hours = Math.min(720, Math.max(0, +row.reentry_cooldown_hours || 0));
  for (const k of ["tier_green_frac", "tier_blue_frac"])
    if (row[k] !== undefined) row[k] = Math.min(1, Math.max(0, +row[k] || 0));
  if (row.tier_green_min !== undefined && row.tier_blue_min !== undefined &&
      +row.tier_green_min < +row.tier_blue_min) {
    return json({ error: "Green threshold must be ≥ blue." }, 400);
  }
  if (row.tier_green_frac && row.tier_blue_frac && +row.tier_green_frac < +row.tier_blue_frac) {
    return json({ error: "Green % must be ≥ blue %." }, 400);
  }
  row.source = "dashboard";
  row.note = "saved from dashboard (worker)";

  // Tolerate schema drift: if a column doesn't exist in D1 yet, drop it and retry.
  let cols = Object.keys(row);
  for (let i = 0; i < 8; i++) {
    const sql = `INSERT INTO config_history (${cols.map((c) => `"${c}"`).join(",")}) ` +
                `VALUES (${cols.map(() => "?").join(",")})`;
    try {
      await env.DB.prepare(sql).bind(...cols.map((c) => bindVal(row[c]))).run();
      return json({ ok: true, saved: row });
    } catch (e) {
      const m = String(e).match(/(?:has no column named|no such column:?)\s+(\w+)/);
      if (m && cols.includes(m[1])) { cols = cols.filter((c) => c !== m[1]); continue; }
      return json({ error: "D1 rejected the write.", detail: String(e).slice(0, 200) }, 502);
    }
  }
  return json({ error: "D1 write failed." }, 502);
}

// Save the wallet/account policy (dashboard Settings) into D1 kv_store.
const WALLET_CLAMP = {
  stake: [1, 100000], max_exposure: [0.05, 1],
  slippage_pct: [0, 0.2], fee_pct: [0, 0.2], min_overlap: [0, 50],
};
async function saveWallet(request, env) {
  if (!env.DB) return json({ error: "Missing D1 binding." }, 500);
  let body;
  try { body = await request.json(); } catch { return json({ error: "Body must be JSON." }, 400); }
  const w = { green_only: !!body.green_only };
  for (const [k, [lo, hi]] of Object.entries(WALLET_CLAMP)) {
    const v = +body[k];
    if (isFinite(v)) w[k] = Math.min(hi, Math.max(lo, v));
  }
  try {
    await env.DB.prepare(
      "INSERT INTO kv_store (key,value,updated_at) VALUES ('wallet_config',?,?) " +
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at")
      .bind(JSON.stringify(w), new Date().toISOString()).run();
    return json({ ok: true, saved: w });
  } catch (e) {
    return json({ error: "D1 rejected the write.", detail: String(e).slice(0, 200) }, 502);
  }
}

// Batched sell-side (bid) prices — what you'd realistically GET exiting now. {token_id: float}.
async function clobMarks(assets) {
  const out = {};
  for (let i = 0; i < assets.length; i += 250) {
    const body = assets.slice(i, i + 250).map((t) => ({ token_id: t, side: "SELL" }));
    let r;
    try {
      r = await fetch(CLOB_PRICES, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "User-Agent": "cf-worker-fastmark/1.0",   // CLOB 1010-blocks POSTs with no UA
        },
        body: JSON.stringify(body),
      });
    } catch { continue; }
    if (!r.ok) continue;
    const data = await r.json().catch(() => ({}));
    for (const [t, v] of Object.entries(data || {})) {
      const px = v && typeof v === "object" ? v.SELL : v;
      const f = parseFloat(px);
      if (isFinite(f)) out[t] = f;
    }
  }
  return out;
}

// Upsert a blob (marks.json / data.json) into D1; the Worker serves it at /<name>.
async function putBlob(env, name, payload, ts) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  try {
    await env.DB.prepare(
      "INSERT INTO site_blob (name,body,updated_at) VALUES (?,?,?) " +
      "ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at")
      .bind(name, body, ts || new Date().toISOString()).run();
  } catch (e) { console.log("blob write failed", name, e); }
}

// Newest config row (the exit knobs), with pre-migration fallbacks. One D1 read.
async function loadExitConfig(env) {
  try {
    const row = await env.DB.prepare("SELECT * FROM config_history ORDER BY id DESC LIMIT 1").first();
    if (row) {
      const cfg = {};
      for (const k of Object.keys(EXIT_DEFAULTS)) {
        const v = +row[k];
        cfg[k] = isFinite(v) ? v : EXIT_DEFAULTS[k];
      }
      return cfg;
    }
  } catch (e) { console.log("fastmark: config read error", e); }
  return { ...EXIT_DEFAULTS };
}

// PRICE/TIME exit decision — the JS mirror of poller/strategy.py `price_exit`.
// OVERLAP-ONLY (control stays naive). Keep in lockstep with the Python source.
function priceExit(t, mark, peak, cfg) {
  if ((t.strategy || "overlap") !== "overlap" || mark == null) return null;
  const entry = +t.entry_price || 0;
  if (entry <= 0) return null;
  const ret = (mark - entry) / entry;
  const fast = +cfg.fast_exit_slippage_pct || 0;
  const hair = (px) => Math.max(0, px * (1 - fast));
  const tmin = +cfg.time_stop_minutes || 0;
  if (tmin && t.end_date) {
    const end = Date.parse(t.end_date);
    if (isFinite(end) && (end - Date.now()) <= tmin * 60000) return { reason: "time_stop", price: mark };
  }
  const stop = +cfg.stop_loss_pct || 0, floor = +cfg.min_entry_price || 0;
  if ((stop && ret <= -stop) || (floor && mark < floor)) return { reason: "stop_loss", price: hair(mark) };
  const tp = +cfg.take_profit_pct || 0;
  if (tp && ret >= tp) return { reason: "take_profit", price: mark };
  const trail = +cfg.trailing_stop_pct || 0, arm = +cfg.trailing_arm_pct || 0;
  if (trail && peak && peak > entry) {
    const armed = arm ? ((peak - entry) / entry >= arm) : true;
    if (armed && mark <= peak * (1 - trail)) return { reason: "trailing_stop", price: hair(mark) };
  }
  return null;
}

// Close one paper trade in D1. `AND status='OPEN'` makes it idempotent vs the
// engine: whoever flips status first wins; the other UPDATE matches 0 rows.
async function closeTrade(env, t, reason, exitPrice, peak, ts) {
  const rpnl = (+t.shares || 0) * (exitPrice - (+t.entry_price || 0));
  try {
    const res = await env.DB.prepare(
      "UPDATE paper_trades SET status='CLOSED',exit_at=?,exit_price=?,realized_pnl=?,marked_price=?," +
      "marked_at=?,unrealized_pnl=0,peak_price=?,close_reason=?,updated_at=? WHERE id=? AND status='OPEN'")
      .bind(ts, exitPrice, rpnl, exitPrice, ts, peak, reason, ts, t.id).run();
    return !!(res.meta && res.meta.changes > 0);
  } catch (e) { console.log("fastmark: close failed", t.id, e); return false; }
}

// FAST MARK: re-price OPEN paper trades, run the fast price/time EXITS (overlap
// only), and publish marks.json (in D1) the dashboard overlays every minute.
async function fastMark(env) {
  if (!env.DB) return;
  let trades = [];
  try {
    const r = await env.DB.prepare(
      "SELECT id,strategy,asset,shares,entry_price,tier_at_entry,end_date,peak_price " +
      "FROM paper_trades WHERE status='OPEN'").all();
    trades = r.results || [];
  } catch (e) { console.log("fastmark: trades read error", e); return; }

  const ts = new Date().toISOString();
  if (trades.length === 0) {
    await putBlob(env, "marks.json", { ts, marks: {}, open: 0, priced: 0 }, ts);
    return;
  }
  const cfg = await loadExitConfig(env);
  const assets = [...new Set(trades.map((t) => t.asset).filter(Boolean))];
  const marks = await clobMarks(assets);

  const closedIds = new Set();
  for (const t of trades) {
    const p = marks[t.asset];
    if (p == null) continue;
    const peak = Math.max(+t.peak_price || +t.entry_price || 0, p);
    const ex = priceExit(t, p, peak, cfg);
    if (ex) {
      if (await closeTrade(env, t, ex.reason, ex.price, peak, ts)) closedIds.add(t.id);
    } else if (peak > (+t.peak_price || 0)) {
      try {
        await env.DB.prepare("UPDATE paper_trades SET peak_price=?,updated_at=? WHERE id=? AND status='OPEN'")
          .bind(peak, ts, t.id).run();
      } catch (e) { /* best-effort; the engine also persists the peak */ }
    }
  }

  const open = trades.filter((t) => !closedIds.has(t.id));
  const by_strategy = {};
  for (const t of open) {
    const p = marks[t.asset];
    if (p == null) continue;
    const up = (+t.shares || 0) * (p - (+t.entry_price || 0));
    const s = by_strategy[t.strategy] || (by_strategy[t.strategy] = { unrealized: 0, marked: 0 });
    s.unrealized += up; s.marked += 1;
  }
  await putBlob(env, "marks.json", {
    ts, marks, open: open.length, priced: Object.keys(marks).length,
    closed: closedIds.size, by_strategy,
  }, ts);
}

// Serve a D1 blob (data.json / marks.json) — the browser fetches from the Worker
// (free egress) instead of Supabase Storage.
async function serveBlob(env, name) {
  try {
    const row = await env.DB.prepare("SELECT body FROM site_blob WHERE name=?").bind(name).first();
    if (row && row.body) {
      return new Response(row.body, {
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...CORS },
      });
    }
    return json({ error: name + " not published yet" }, 404);
  } catch (e) {
    return json({ error: String(e).slice(0, 200) }, 502);
  }
}

export default {
  async scheduled(event, env, ctx) {
    if ((event.cron || "") === FULL_SCAN_CRON) {
      const res = await poke(env);
      if (!res.ok) console.log("dispatch failed", res.status, await res.text());
    } else {
      await fastMark(env);
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    // Dashboard data, served from D1 (zero Supabase egress).
    if (url.pathname === "/data.json") return serveBlob(env, "data.json");
    if (url.pathname === "/marks.json") return serveBlob(env, "marks.json");

    if (url.pathname === "/config" && request.method === "POST") return saveConfig(request, env);
    if (url.pathname === "/wallet" && request.method === "POST") return saveWallet(request, env);

    if (url.searchParams.has("run")) {
      const res = await poke(env);
      const body = await res.text();
      return new Response(
        res.ok ? "Triggered a poll (204). Fresh data in ~1-2 min." : `Failed: ${res.status} ${body}`,
        { status: res.ok ? 200 : 502, headers: CORS },
      );
    }

    if (url.searchParams.has("mark")) {
      try { await fastMark(env); return new Response("fast-mark done — marks.json updated.", { headers: CORS }); }
      catch (e) { return new Response("fast-mark error: " + e, { status: 502, headers: CORS }); }
    }

    return new Response(
      "Polymarket poller worker (D1) — alive. GitHub full scan every 5 min; fast price-mark every 1 min. " +
      "GET /data.json + /marks.json (served from D1); POST /config or /wallet; ?run = poll now; ?mark = re-mark now.",
      { headers: CORS },
    );
  },
};
