-- OrbitCast AI: self-discovered source leads
--
-- Every scraper source in this project arrived because a human noticed a
-- missing event and sent a link. Coverage was therefore a function of how
-- much event-hunting somebody did that week, and an entire organiser could go
-- missing indefinitely - the run summary counts what was found and can never
-- count what wasn't.
--
-- Aggregator calendars carry other people's events, and each of those events
-- names its host calendar. So every scrape already sees organisers we don't
-- track. This table is where those leads accumulate instead of being thrown
-- away, with a count of how many events we've seen from each and how many
-- scrapes it has shown up in, so a one-off is distinguishable from a regular.
--
-- Candidates are PROPOSED, never auto-added: hosting one London event is not
-- the same as being worth a permanent source. `status` is how a human answers
-- - and 'ignored' has to survive re-harvesting, or every rejected lead would
-- reappear at the top of the list on the next scrape.
--
-- Same RLS posture as every other table here: deny-by-default for anon and
-- authenticated, only the service_role connection (DATABASE_URL) can read it.

create table if not exists source_candidates (
  identifier   text primary key,
  kind         text        not null default 'luma_calendar',
  name         text,
  url          text,
  city         text,
  timezone     text,
  event_count  integer     not null default 0,
  times_seen   integer     not null default 1,
  sample_title text,
  status       text        not null default 'new'
                 check (status in ('new', 'added', 'ignored')),
  first_seen   timestamptz not null default now(),
  last_seen    timestamptz not null default now()
);

create index if not exists source_candidates_status_idx
  on source_candidates (status, event_count desc);

alter table source_candidates enable row level security;
alter table source_candidates force row level security;

comment on table source_candidates is
  'Host calendars seen behind ingested events but not yet tracked as sources. Proposals for a human to accept or reject.';
comment on column source_candidates.status is
  'new = unreviewed, added = now a real source, ignored = rejected and must stay suppressed across re-harvests.';
