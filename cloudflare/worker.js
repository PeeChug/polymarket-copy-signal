// Cloudflare Worker — fires the GitHub Actions poller every 15 minutes.
//
// The poller's compute runs in GitHub Actions, but GitHub's own scheduled cron
// is unreliable on the free tier. This Worker pokes the workflow_dispatch
// endpoint on Cloudflare's dependable cron instead, so the dashboard stays
// fresh with NO browser tab open and NO in-page Auto-poll.
//
// A workflow_dispatch runs `python -m poller.main --force` (a real poll), so a
// 15-minute trigger == a 15-minute refresh. Setup: see ./README.md.
// The GitHub token is read from the GH_PAT secret (never hard-coded here).

const DISPATCH_URL =
  "https://api.github.com/repos/PeeChug/polymarket-copy-signal/actions/workflows/poller.yml/dispatches";

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

export default {
  // Cloudflare invokes this on the cron schedule (see wrangler.toml / dashboard).
  async scheduled(event, env, ctx) {
    const res = await poke(env);
    if (!res.ok) console.log("dispatch failed", res.status, await res.text());
  },

  // Visiting the Worker URL shows a status line; add ?run to fire a poll now
  // (a handy one-click test). The poller is read-only, so this is harmless.
  async fetch(request, env, ctx) {
    if (new URL(request.url).searchParams.has("run")) {
      const res = await poke(env);
      const body = await res.text();
      return new Response(
        res.ok ? "Triggered a poll (204). Fresh data in ~1-2 min."
               : `Failed: ${res.status} ${body}`,
        { status: res.ok ? 200 : 502 },
      );
    }
    return new Response(
      "Polymarket poller cron worker — alive. Fires every 15 min. Add ?run to poll now.",
    );
  },
};
