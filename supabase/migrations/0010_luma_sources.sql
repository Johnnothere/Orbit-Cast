-- OrbitCast AI: Luma organisers as data rather than code
--
-- LUMA_CALENDARS and LUMA_USERS are dicts in scraper.py, turned into @source
-- functions by a loop that runs at import. That has two consequences: adding an
-- organiser is a code change and a deploy, and source_candidates - the table
-- that exists precisely so discovered organisers can be approved - could never
-- actually DO anything when one was approved. Marking a candidate 'added' set a
-- string and nothing else; the feed still had to be written by hand. Sixty-two
-- candidates accumulated unreviewed behind that gap.
--
-- Rows here are read at scrape time by the DB-backed Luma sources in
-- scraper.py, so approving an organiser makes it feed on the next refresh with
-- no deploy. The hardcoded dicts stay exactly as they are - this is additive,
-- and dedupe on identifier means an organiser cannot be tracked twice if it
-- appears in both places.
--
-- `category` is stored per organiser because the catalog's category comes from
-- the SOURCE, not the event. One dynamic source per category is registered at
-- import, and each reads the organisers assigned to it - which is what keeps
-- categories correct without needing to register a source per organiser.
--
-- `verdict` records the check that admitted the organiser: how many real
-- upcoming events it had when verified, how many were London, and what its
-- category was judged from. An organiser that later goes quiet is not deleted -
-- a quiet organiser is not a dead one - so this is the only record of what it
-- looked like when it was accepted.
--
-- Same RLS posture as every other table here: deny by default, service_role
-- (DATABASE_URL) only.

create table if not exists luma_sources (
  identifier  text primary key,
  kind        text        not null default 'calendar'
                check (kind in ('calendar', 'user')),
  name        text        not null,
  emoji       text,
  category    text        not null,
  status      text        not null default 'active'
                check (status in ('active', 'paused')),
  verdict     jsonb,
  added_by    text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- The scrape-time read is "active organisers in this category", once per
-- category per refresh.
create index if not exists luma_sources_active_idx
  on luma_sources (status, category);

alter table luma_sources enable row level security;
alter table luma_sources force row level security;

comment on table luma_sources is
  'Luma calendars/profiles tracked as recurring sources, held as data so an organiser can be approved from the admin dashboard without a deploy.';
comment on column luma_sources.identifier is
  'Luma calendar api id (cal-...) or a user handle/usr- id. Primary key, so an organiser cannot be tracked twice.';
comment on column luma_sources.category is
  'Which catalog category this organiser feeds. The category comes from the source, not the event, so it is set per organiser at approval time.';
comment on column luma_sources.status is
  'active = scraped every refresh, paused = kept on the books but not scraped. Deleting is deliberately not the way to silence one.';
comment on column luma_sources.verdict is
  'The verification that admitted it: real upcoming event count, how many passed the London check, and what the category was judged from.';
