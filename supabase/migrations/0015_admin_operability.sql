-- 0015_admin_operability.sql
--
-- Everything here exists so the admin panel can ACT on what it shows.
-- Three gaps it closes, all of them read-only before:
--
--   1. Coverage printed a per-source count and nothing else, so "0 events"
--      could not be told apart from "this source has been broken for a
--      fortnight". source_health stores the outcome of every scrape per
--      source: the count, the last time it returned anything at all, the
--      last error it raised, and how many empty runs it has had in a row.
--
--   2. Only the 52 Luma organisers could be paused. The 32 hardcoded sources
--      in scraper.py had no controls at all. source_health.paused is checked
--      for EVERY source, whichever kind it is.
--
--   3. A bad scraped event could not be touched — only manually added ones
--      could be removed. hidden_events suppresses one by its event_id, which
--      is a stable hash of title+url, so it stays suppressed across scrapes.
--
-- Additive only. Safe to apply before or after the code deploy: the app
-- degrades to the old behaviour if these tables are missing.

-- ── 1. per-source scrape health ──────────────────────────────────────────
create table if not exists source_health (
  source            text        primary key,
  kind              text        not null default 'builtin',   -- builtin | luma
  emoji             text,
  category          text,
  last_count        integer     not null default 0,
  consecutive_zero  integer     not null default 0,
  last_run          timestamptz,
  last_success_at   timestamptz,                              -- last run that returned >0
  last_error        text,
  paused            boolean     not null default false,
  updated_at        timestamptz not null default now()
);
create index if not exists source_health_paused_idx on source_health (paused);
create index if not exists source_health_zero_idx   on source_health (last_count, consecutive_zero desc);

alter table source_health enable row level security;
alter table source_health force  row level security;

-- ── 2. manually suppressed catalogue events ──────────────────────────────
create table if not exists hidden_events (
  event_id   text        primary key,
  title      text,
  reason     text,
  hidden_by  text,
  created_at timestamptz not null default now()
);
create index if not exists hidden_events_created_idx on hidden_events (created_at desc);

alter table hidden_events enable row level security;
alter table hidden_events force  row level security;

-- ── 3. indexes the leads queue needs now that it paginates ───────────────
-- 806 rows with status='new' were served by one unindexed scan with a hard
-- cap of 100 and no offset. These cover the four sorts the panel offers.
create index if not exists source_candidates_status_events_idx
  on source_candidates (status, event_count desc, times_seen desc);
create index if not exists source_candidates_status_lastseen_idx
  on source_candidates (status, last_seen desc);
create index if not exists source_candidates_status_firstseen_idx
  on source_candidates (status, first_seen asc);
