# Cloudflare Worker — reliable 15-min poller trigger

GitHub Actions' scheduled cron is "best effort" and fires unreliably on the free
tier. This tiny Worker pokes the poller's `workflow_dispatch` endpoint every 15
minutes on Cloudflare's dependable cron, so the dashboard stays fresh **with no
browser tab open**. The poller's actual work still runs in GitHub Actions; this
only presses the button.

You need a GitHub fine-grained PAT with **Actions: Read and write** on this repo
(reuse the one from the dashboard's Auto-poll). Free Cloudflare plan is plenty:
a 15-min cron is 96 runs/day, far under the 100k/day free limit.

## Option A — Dashboard (no CLI, ~10 min)

1. Sign in at **dash.cloudflare.com** → **Workers & Pages** → **Create** →
   **Create Worker**. Name it `polymarket-poller-cron`, Deploy (the default hello
   worker is fine for now).
2. **Edit code** → paste the contents of [`worker.js`](worker.js) over the
   template → **Deploy**.
3. Worker → **Settings → Variables and Secrets** → **Add** a **Secret**:
   - Name: `GH_PAT`
   - Value: your `github_pat_…` token
   - Save / Deploy.
4. Worker → **Settings → Triggers → Cron Triggers** → **Add Cron Trigger** →
   `*/15 * * * *` → Save.
5. Test: open `https://polymarket-poller-cron.<your-subdomain>.workers.dev/?run`
   — it should say "Triggered a poll (204)". Check the repo's Actions tab for a
   new `workflow_dispatch` run.

Then turn **off** the dashboard's in-page Auto-poll and close the tab — it stays
fresh on its own.

## Option B — Wrangler CLI

```bash
npm i -g wrangler
cd cloudflare
wrangler login
wrangler secret put GH_PAT      # paste the token when prompted
wrangler deploy                 # reads wrangler.toml (cron + entry point)
```

## Manage

- Test now: visit the Worker URL with `?run`.
- Logs: Worker → **Logs** (or `wrangler tail`).
- Change cadence: edit the cron in the dashboard trigger or `wrangler.toml`.
- Remove: delete the Worker, or remove the cron trigger.

The GitHub `*/5` Actions cron stays as a free backup; the 15-minute interval gate
(`poll_interval_minutes`) means there's no double work if both happen to fire.
