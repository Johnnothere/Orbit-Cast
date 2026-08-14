-- OrbitCast AI: durable event catalog
--
-- events_cache.json is gitignored AND Railway's filesystem is ephemeral, so
-- a freshly deployed container has no catalog at all - not a stale one, none.
-- Every visitor in the first minute of a deploy got an empty list, and the
-- only thing that fixed it was the boot scrape finishing.
--
-- The catalog lives here instead. It survives deploys, is shared by every
-- process that connects, and never needs a human to refresh it.
--
-- Stored as ONE row holding the whole snapshot rather than a row per event.
-- The catalog is rebuilt wholesale on every scrape and is only ever read
-- wholesale by the app, so per-event rows would buy queryability nobody
-- calls and cost a diff/upsert path that could half-apply. A single jsonb
-- swap is atomic: readers see the old catalog or the new one, never a
-- partially rewritten one.
--
-- Same RLS posture as every other table here: deny-by-default for anon and
-- authenticated, only the service_role connection (DATABASE_URL) can touch it.

create table if not exists events_cache (
  id           smallint primary key default 1 check (id = 1),
  payload      jsonb       not null,
  event_count  integer     not null default 0,
  source_count integer     not null default 0,
  last_run     timestamptz not null default now()
);

alter table events_cache enable row level security;
alter table events_cache force row level security;

comment on table events_cache is
  'Single-row snapshot of the scraped event catalog. Survives Railway deploys, which wipe the local JSON cache.';
comment on column events_cache.payload is
  'Full {events, summary, last_run} document, identical in shape to the events_cache.json fallback.';
