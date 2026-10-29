-- 0021_event_times.sql
--
-- The start and end time each event's own page states (event_times.py). One
-- row per event URL, replaced whenever the page is re-read (about daily for a
-- page with a time, every few hours for one without). found=false records
-- a page that was read and states no time, so it is not re-read on every
-- scrape. start_time / end_time are "HH:MM" in London time, or null.
--
-- Public information copied from the organiser's page; nothing here is about
-- a user. Additive and idempotent; the app runs without it (times then live
-- in process memory only and pages are re-read after a deploy).

create table if not exists event_times (
  url         text primary key,
  found       boolean not null default false,
  start_time  text,
  end_time    text,
  payload     jsonb not null,
  fetched_at  timestamptz not null default now()
);

alter table event_times enable row level security;
