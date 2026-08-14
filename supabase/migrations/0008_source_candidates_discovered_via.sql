-- OrbitCast AI: record HOW a source lead was found
--
-- The first harvester only walked events already in the catalog, so it could
-- only ever find organisers who are actively publishing. An organiser who ran
-- London events for a year and has nothing scheduled right now is invisible
-- to it - which is why two dormant Luma sources still had to arrive by hand.
--
-- There are now three routes, and they reach different things:
--
--   catalog_host - the calendar behind an event we already ingest. Narrow:
--                  limited to what our existing sources already surface.
--   city_feed    - Luma's London discover feed. Much wider than the catalog,
--                  and the calendar is embedded in each entry, so a page of
--                  ~46 events costs one request rather than 46.
--   host_history - a known host's PAST events. The only route that reaches a
--                  dormant organiser: people outlive their calendars, so the
--                  host of an event we can see today leads back to calendars
--                  that have published nothing for months.
--
-- Knowing which route found a lead is what tells us whether discovery is
-- actually widening or just re-finding the same neighbourhood.

alter table source_candidates add column if not exists discovered_via text;

comment on column source_candidates.discovered_via is
  'How the lead was found: catalog_host (behind an event we ingest), city_feed (Luma London discover), or host_history (a known host past events - the only route that reaches an organiser with nothing scheduled).';
