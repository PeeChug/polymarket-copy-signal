// Cloudflare Worker — three jobs:
//   1. FULL SCAN (every 10 min): fire the GitHub Actions poller (heavy Python
//      job — leaderboard, 50-trader cohort, consensus, open/close trades). Too
//      big for a Worker (50-subrequest free cap; it's Python), so GitHub owns it.
//   2. FAST MARK (every 1 min): re-price just the OPEN paper trades via one
//      batched CLOB /prices call and write a tiny marks.json to Supabase
//      Storage. Light enough for the Worker free tier (≈3 subrequests, trivial
//      CPU) → the dashboard's P&L refreshes every minute with no GitHub spin-up.
//   3. Accept paper-trade config saves from the dashboard (POST /config).
//
// Secrets (set via the Cloudflare API / `wrangler secret put`):
//   GH_PAT        — GitHub PAT with Actions: write (fires workflow_dispatch)
//   SUPABASE_URL  — https://<project>.supabase.co
//   SUPABASE_KEY  — Supabase secret (service) key; stays server-side only
//
// Setup: see ./README.md.

const DISPATCH_URL =
  "https://api.github.com/repos/PeeChug/polymarket-copy-signal/actions/workflows/poller.yml/dispatches";

const CLOB_PRICES = "https://clob.polymarket.com/prices";   // batch price endpoint (500 req/10s)
const MARKS_OBJECT = "/storage/v1/object/dashboard/marks.json";
const FULL_SCAN_CRON = "*/10 * * * *";                       // GitHub heavy scan cadence

// The ONLY columns the dashboard may write into config_history. Anything else
// in the request body is dropped, so a stray/hostile field can never land.
const ALLOWED = [
  "top_n", "leaderboard_window", "size_threshold", "poll_interval_minutes",
  "tier_green_min", "tier_blue_min", "min_liquidity", "max_entry_price", "min_resolve_hours",
  "min_tier_to_trade", "stake_usd", "price_source", "control_respects_guardrails",
  "stop_loss_pct", "contested_policy", "min_holder_value", "min_holder_win_ratio",
];

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });

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

// Append a new (forward-only) config_history row from the dashboard.
async function saveConfig(request, env) {
  if (!env.SUPABASE_URL || !env.SUPABASE_KEY) {
    return json({ error: "Worker is missing SUPABASE_URL / SUPABASE_KEY secrets." }, 500);
  }
  let body;
  try { body = await request.json(); }
  catch { return json({ error: "Body must be JSON." }, 400); }

  const row = {};
  for (const k of ALLOWED) if (body[k] !== undefined && body[k] !== null) row[k] = body[k];
  if (Object.keys(row).length === 0) return json({ error: "No recognized config fields." }, 400);

  // Don't trust the client: re-clamp the risky knobs and re-check the tier order.
  if (row.stop_loss_pct !== undefined) row.stop_loss_pct = Math.min(0.95, Math.max(0, +row.stop_loss_pct || 0));
  if (row.stake_usd !== undefined) row.stake_usd = Math.max(1, Math.round(+row.stake_usd || 100));
  if (row.tier_green_min !== undefined && row.tier_blue_min !== undefined &&
      +row.tier_green_min < +row.tier_blue_min) {
    return json({ error: "Green threshold must be ≥ blue." }, 400);
  }
  row.source = "dashboard";
  row.note = "saved from dashboard (worker)";

  // Tolerate schema drift: if a column doesn't exist yet (PGRST204), drop it and
  // retry, so a newly-added config field never hard-fails the whole save before
  // its ALTER is run (it just won't persist until then).
  let text = "";
  for (let i = 0; i < 8; i++) {
    const r = await fetch(env.SUPABASE_URL + "/rest/v1/config_history", {
      method: "POST",
      headers: {
        "apikey": env.SUPABASE_KEY,
        "Authorization": "Bearer " + env.SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
      },
      body: JSON.stringify(row),
    });
    text = await r.text();
    if (r.ok) {
      let saved; try { saved = JSON.parse(text); } catch { saved = text; }
      return json({ ok: true, saved: Array.isArray(saved) ? saved[0] : saved });
    }
    const m = text.match(/Could not find the '([^']+)' column/);
    if (m && m[1] in row) { delete row[m[1]]; continue; }
    return json({ error: "Supabase rejected the write.", status: r.status, detail: text }, 502);
  }
  return json({ error: "Supabase rejected the write.", detail: text }, 502);
}

// Save the wallet/account policy (dashboard Settings) into kv_store. The poller
// reads it to build the wallet sims. Clamped server-side; secret stays here.
const WALLET_CLAMP = {
  stake: [1, 100000], max_exposure: [0.05, 1],
  slippage_pct: [0, 0.2], fee_pct: [0, 0.2], min_overlap: [0, 50],
};
async function saveWallet(request, env) {
  if (!env.SUPABASE_URL || !env.SUPABASE_KEY) return json({ error: "Missing SUPABASE secrets." }, 500);
  let body;
  try { body = await request.json(); } catch { return json({ error: "Body must be JSON." }, 400); }
  const w = { green_only: !!body.green_only };
  for (const [k, [lo, hi]] of Object.entries(WALLET_CLAMP)) {
    const v = +body[k];
    if (isFinite(v)) w[k] = Math.min(hi, Math.max(lo, v));
  }
  const r = await fetch(env.SUPABASE_URL + "/rest/v1/kv_store", {
    method: "POST",
    headers: {
      apikey: env.SUPABASE_KEY, Authorization: "Bearer " + env.SUPABASE_KEY,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates,return=representation",
    },
    body: JSON.stringify({ key: "wallet_config", value: w, updated_at: new Date().toISOString() }),
  });
  const text = await r.text();
  if (r.ok) return json({ ok: true, saved: w });
  return json({ error: "Supabase rejected the write.", status: r.status, detail: text.slice(0, 200) }, 502);
}

// Batched sell-side (bid) prices — what you'd realistically GET exiting now,
// matching the Python engine's realistic mark. {token_id: float}.
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
      const px = v && typeof v === "object" ? v.SELL : v;   // {token:{SELL:"0.93"}} or {token:"0.93"}
      const f = parseFloat(px);
      if (isFinite(f)) out[t] = f;
    }
  }
  return out;
}

async function putMarks(env, auth, payload) {
  const r = await fetch(env.SUPABASE_URL + MARKS_OBJECT, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json", "x-upsert": "true" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.log("fastmark: marks upload failed", r.status, await r.text());
}

// FAST MARK: re-price the OPEN paper trades and publish a tiny marks.json the
// dashboard overlays. Read trades (1) + CLOB prices (1) + upload (1) = ≈3
// subrequests, well under the free 50/invocation cap.
async function fastMark(env) {
  if (!env.SUPABASE_URL || !env.SUPABASE_KEY) return;
  const auth = { apikey: env.SUPABASE_KEY, Authorization: "Bearer " + env.SUPABASE_KEY };
  const sel = "?status=eq.OPEN&select=strategy,asset,shares,entry_price,tier_at_entry";
  let trades = [];
  try {
    const tr = await fetch(env.SUPABASE_URL + "/rest/v1/paper_trades" + sel, { headers: auth });
    if (!tr.ok) { console.log("fastmark: trades read failed", tr.status); return; }
    trades = await tr.json();
  } catch (e) { console.log("fastmark: trades read error", e); return; }

  const ts = new Date().toISOString();
  if (!Array.isArray(trades) || trades.length === 0) {
    await putMarks(env, auth, { ts, marks: {}, open: 0, priced: 0 });   // stay fresh even with 0 open
    return;
  }
  const assets = [...new Set(trades.map((t) => t.asset).filter(Boolean))];
  const marks = await clobMarks(assets);
  const by_strategy = {};
  for (const t of trades) {
    const p = marks[t.asset];
    if (p == null) continue;
    const up = (+t.shares || 0) * (p - (+t.entry_price || 0));
    const s = by_strategy[t.strategy] || (by_strategy[t.strategy] = { unrealized: 0, marked: 0 });
    s.unrealized += up; s.marked += 1;
  }
  await putMarks(env, auth, { ts, marks, open: trades.length, priced: Object.keys(marks).length, by_strategy });
}

export default {
  // Cloudflare invokes this on each cron (see wrangler.toml). Two cadences:
  //   */10 → heavy GitHub scan;  every minute → light fast-mark.
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

    // Save paper-trade config — writes to Supabase server-side, no token needed.
    if (url.pathname === "/config" && request.method === "POST") return saveConfig(request, env);

    // Save the wallet/account policy — writes to Supabase kv_store.
    if (url.pathname === "/wallet" && request.method === "POST") return saveWallet(request, env);

    // Fire a poll now (?run) — handy one-click test; the poller is read-only.
    if (url.searchParams.has("run")) {
      const res = await poke(env);
      const body = await res.text();
      return new Response(
        res.ok ? "Triggered a poll (204). Fresh data in ~1-2 min." : `Failed: ${res.status} ${body}`,
        { status: res.ok ? 200 : 502, headers: CORS },
      );
    }

    // Run a fast-mark now (?mark) — re-prices open positions + writes marks.json.
    if (url.searchParams.has("mark")) {
      try { await fastMark(env); return new Response("fast-mark done — marks.json updated.", { headers: CORS }); }
      catch (e) { return new Response("fast-mark error: " + e, { status: 502, headers: CORS }); }
    }

    return new Response(
      "Polymarket poller worker — alive. Full GitHub scan every 10 min; fast price-mark every 1 min. " +
      "POST /config (paper-trade settings) or /wallet (wallet policy); ?run = poll now; ?mark = re-mark prices now.",
      { headers: CORS },
    );
  },
};
