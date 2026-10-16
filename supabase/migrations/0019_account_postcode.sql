-- 0019_account_postcode.sql
--
-- Home postcode stored on the account, with its geocoded lat/lng. The
-- user_locations table already stores HTML5-geolocation pings for a
-- consenting browser; this is the per-account durable alternative that
-- doesn't need in-the-moment location consent and follows the person
-- across devices.
--
-- Why both? HTML5 geolocation prompts on every browser, resets with cookie
-- clears, and some privacy-aware users decline it. A saved postcode is
-- entered once and persists. The analysis code prefers the postcode when
-- present and falls back to the latest user_locations ping when not.
--
-- Geocoding is done at write time via postcodes.io (free, no key, UK
-- government-adjacent); the stored lat/lng is what every downstream query
-- uses. If postcodes.io is unreachable the write is refused rather than
-- storing an un-geocoded postcode - a bad coordinate is worse than none.
--
-- Additive and idempotent; safe to apply before or after the code deploy.

alter table accounts add column if not exists postcode        text;
alter table accounts add column if not exists postcode_lat    double precision;
alter table accounts add column if not exists postcode_lng    double precision;
alter table accounts add column if not exists postcode_set_at timestamptz;
