-- ============================================================================
-- 24/7 poller trigger via Supabase pg_cron + pg_net
--
-- The poller runs in GitHub Actions, but GitHub's scheduled cron is "best
-- effort" and fires unreliably on the free tier. This makes Supabase poke the
-- workflow_dispatch endpoint every 15 minutes instead — so the dashboard stays
-- fresh with NO browser tab open and NO in-page Auto-poll.
--
-- A workflow_dispatch always runs `python -m poller.main --force` (a real poll),
-- so 15-min firing = 15-min cadence. Run this once in the Supabase SQL Editor.
-- ============================================================================

-- 1. Enable the scheduler + outbound-HTTP extensions
--    (or enable them in Dashboard → Database → Extensions: pg_cron, pg_net)
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- 2. Store your GitHub token once.
--    Reuse your in-page Auto-poll PAT (fine-grained, Actions: Read and write on
--    this repo). In Dashboard → Project Settings → Vault → "Add new secret":
--        name  = gh_pat
--        value = github_pat_...
--    (Alternatively, inline the token in the Authorization header below instead
--     of the vault lookup — simpler but less secure.)

-- 3. Schedule the poke every 15 minutes.
select cron.schedule(
  'poke-polymarket-poller',
  '*/15 * * * *',
  $$
  select net.http_post(
    url     := 'https://api.github.com/repos/PeeChug/polymarket-copy-signal/actions/workflows/poller.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'gh_pat'),
      'Accept',        'application/vnd.github+json',
      'User-Agent',    'supabase-pg-cron'
    ),
    body    := '{"ref":"main"}'::jsonb
  );
  $$
);

-- ---------------------------------------------------------------------------
-- Useful afterwards:
--   select * from cron.job;                              -- list schedules
--   select * from cron.job_run_details order by start_time desc limit 10;  -- history
--   select cron.unschedule('poke-polymarket-poller');   -- stop it
-- ============================================================================
