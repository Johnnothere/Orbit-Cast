-- 0020_event_details.sql
--
-- Per-event details read from each event's own page: a short "about" summary,
-- "good fit for" labels, the exact venue when the page gives one, and the
-- organiser's contact links. One row per catalogue event id, replaced whenever
-- the page is re-read (every few hours while the event is upcoming).
--
-- The payload is public information copied from the organiser's page; nothing
-- here is about a user. Additive and idempotent; the app runs without it
-- (details then live in process memory only and are re-read after a deploy).

create table if not exists event_details (
  id          text primary key,
  url         text not null,
  payload     jsonb not null,
  fetched_at  timestamptz not null default now()
);

alter table event_details enable row level security;
