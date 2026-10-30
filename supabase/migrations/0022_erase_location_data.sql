-- 0022_erase_location_data.sql
--
-- The distance feature is gone: no home postcode, no browser geolocation, no
-- map, no commute links. Nothing in the app reads these columns any more.
--
-- Leaving them populated would mean holding home postcodes and GPS fixes for
-- a feature that no longer exists, which is exactly what data minimisation
-- says not to do - and these are among the more sensitive things OrbitCast
-- ever stored, since a home postcode plus a name identifies a household.
-- Erasing them is also what the privacy policy already promises.
--
-- Deliberately DATA ONLY, no DDL. Dropping a column cannot be undone and
-- costs nothing to defer; emptying one is the part that matters for privacy
-- and is safe to run now. Drop the columns in a later migration once this
-- has been in production long enough to be sure nothing reads them.
--
-- Re-runnable: both statements are idempotent.

update accounts
   set postcode     = null,
       postcode_lat = null,
       postcode_lng = null
 where postcode is not null
    or postcode_lat is not null
    or postcode_lng is not null;

-- Every row here is a browser geolocation fix. There is no longer any code
-- path that writes one or reads one.
delete from user_locations;
