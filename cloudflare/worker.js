// Cloudflare Worker — two jobs:
//   1. Fire the GitHub Actions poller on Cloudflare's dependable cron (GitHub's
//      own scheduled cron is flaky on the free tier).
//   2. Accept paper-trade config saves from the dashboard (POST /config) and
//      write them to Supabase. The Supabase secret lives HERE, server-side, so
//      the public dashboard never holds a key and never asks for a token.
//
// Secrets (set via the Cloudflare API / `wrangler secret put`):
//   GH_PAT        — GitHub PAT with Actions: write (fires workflow_dispatch)
//   SUPABASE_URL  — https://<project>.supabase.co
//   SUPABASE_KEY  — Supabase secret (service) key; stays server-side only
//
// Setup: see ./README.md.

const DISPATCH_URL =
  "https://api.github.com/repos/PeeChug/polymarket-copy-signal/actions/workflows/poller.yml/dispatches";

// The ONLY columns the dashboard may write into config_history. Anything else
// in the request body is dropped, so a stray/hostile field can never land.
const ALLOWED = [
  "top_n", "leaderboard_window", "size_threshold", "poll_interval_minutes",
  "tier_green_min", "tier_blue_min", "min_liquidity", "max_entry_price",
  "min_tier_to_trade", "stake_usd", "price_source", "control_respects_guardrails",
  "stop_loss_pct", "contested_policy",
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
  const text = await r.text();
  if (!r.ok) return json({ error: "Supabase rejected the write.", status: r.status, detail: text }, 502);
  let saved; try { saved = JSON.parse(text); } catch { saved = text; }
  return json({ ok: true, saved: Array.isArray(saved) ? saved[0] : saved });
}

export default {
  // Cloudflare invokes this on the cron schedule (see wrangler.toml).
  async scheduled(event, env, ctx) {
    const res = await poke(env);
    if (!res.ok) console.log("dispatch failed", res.status, await res.text());
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    // Save paper-trade config — writes to Supabase server-side, no token needed.
    if (url.pathname === "/config" && request.method === "POST") return saveConfig(request, env);

    // Fire a poll now (?run) — handy one-click test; the poller is read-only.
    if (url.searchParams.has("run")) {
      const res = await poke(env);
      const body = await res.text();
      return new Response(
        res.ok ? "Triggered a poll (204). Fresh data in ~1-2 min." : `Failed: ${res.status} ${body}`,
        { status: res.ok ? 200 : 502, headers: CORS },
      );
    }

    return new Response(
      "Polymarket poller cron worker — alive. Fires every 5 min. POST /config to save settings; add ?run to poll now.",
      { headers: CORS },
    );
  },
};
