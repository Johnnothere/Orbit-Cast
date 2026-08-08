-- OrbitCast AI: first-party traffic-source attribution
--
-- Captures where a consenting visitor came from (HTTP referrer + UTM params
-- from the URL), recorded once, at the same moment consent is granted -
-- same rule as everything else here: nothing is written on decline.
--
-- Deliberately NOT a fingerprint, device ID, or MAC address - none of those
-- are collected anywhere in this schema, and MAC addresses in particular
-- are never exposed to a website by any browser, so there's no column for
-- one to go in.

alter table consent add column if not exists referrer text;
alter table consent add column if not exists utm_source text;
alter table consent add column if not exists utm_medium text;
alter table consent add column if not exists utm_campaign text;
