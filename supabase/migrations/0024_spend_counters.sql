-- 0024_spend_counters.sql
--
-- The two site-wide daily spend ceilings - every analysis (ai-daily) and
-- every anonymous analysis (anon-daily) - used to live in flask-limiter's
-- in-process storage. That storage resets on every deploy, and Railway
-- deploys on every push, so on a busy day of pushing the "daily" ceiling
-- reset several times and stopped being a ceiling at all.
--
-- One row per (UTC day, scope). The application takes from it in a single
-- transaction at the moment a job is accepted, locking the rows it needs, so
-- the check and the increment cannot be separated by a concurrent request.
--
-- Old rows are harmless and tiny (two a day); nothing reads past today.

create table if not exists spend_counters (
  day        date        not null,
  scope      text        not null,
  n          integer     not null default 0 check (n >= 0),
  updated_at timestamptz not null default now(),
  primary key (day, scope)
);

-- Server-side only, like every other table here: the app connects as the
-- database owner, and nothing should reach this through the Data API.
alter table spend_counters enable row level security;
