-- OrbitCast AI: operator-added events, held in the database rather than in code
--
-- Curated one-off events have always lived in CURATED_LONDON_EVENTS, a Python
-- list in scraper.py. That works, but it means adding a single event is a code
-- edit, a commit, a push and a Railway redeploy - so every "why isn't this
-- event in here?" link costs a full development round trip. This table is the
-- same idea with the list moved out of the source file, so the admin dashboard
-- can add one directly and it survives the next scrape.
--
-- The catalog is rebuilt from scratch every REFRESH_MINUTES, so anything that
-- is not re-emitted by a source on each run disappears. A row here is read
-- back by the DB-backed curated sources in scraper.py on every scrape, which
-- is what makes an added event persist rather than lasting until the next
-- refresh.
--
-- `url` is the primary key by intent: the operator's input is a link, and the
-- same link submitted twice is the same event. Re-submitting updates the row
-- instead of creating a second copy of the event under a slightly different
-- title.
--
-- `verdict` keeps the whole verification record - which checks ran, what the
-- page actually said, whether the URL was resolved through an aggregator, and
-- whether a human overrode a failed check. Without it there is no way to tell
-- later whether an event was accepted because it passed or because someone
-- forced it through.
--
-- Same RLS posture as every other table in this project: deny by default for
-- anon and authenticated, only the service_role connection (DATABASE_URL)
-- reaches it.

create table if not exists manual_events (
  url          text primary key,
  title        text        not null,
  event_date   text,
  event_time   text,
  location     text,
  description  text,
  category     text        not null,
  emoji        text,
  is_online    boolean     not null default false,
  source_label text,
  verdict      jsonb,
  added_by     text,
  status       text        not null default 'active'
                 check (status in ('active', 'removed')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- The scrape-time read is "every active row", run once per refresh across all
-- categories, so status is the selective column here.
create index if not exists manual_events_status_idx
  on manual_events (status, created_at desc);

alter table manual_events enable row level security;
alter table manual_events force row level security;

comment on table manual_events is
  'Events added by an operator through the admin link portal. Re-emitted into the catalog by the DB-backed curated sources on every scrape.';
comment on column manual_events.url is
  'The event page URL, and the natural key - resubmitting the same link updates the row rather than duplicating the event.';
comment on column manual_events.event_date is
  'ISO date (YYYY-MM-DD) where one could be resolved. Past-dated rows are filtered at scrape time, not deleted, so the record of what was added survives.';
comment on column manual_events.verdict is
  'Full verification record: each guideline check, the extracted fields, any aggregator resolution, and whether a failed check was overridden by a human.';
comment on column manual_events.status is
  'active = emitted into the catalog, removed = kept for the record but no longer served.';
