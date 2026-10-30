-- 0023_digest_shape.sql
--
-- Two things a subscriber can now say about their digest beyond when it
-- arrives: how many events they want in it, and what they are actually
-- interested in, in their own words.
--
-- max_items caps the list AFTER scoring. It is a ceiling, never a target:
-- asking the scorer for "five" is how you get a padded five, which is the
-- one behaviour this product refuses. Default matches the engine's own cap.
--
-- niche_text is free text the subscriber writes. It reaches the scoring
-- prompt as a preference signal and nothing else - see ai_engine.score_events
-- for the delimiters and the instruction that it is data, not instruction.
-- Capped in the application at 400 characters; the column is text so a longer
-- value written by some future path is storable rather than silently cut.
--
-- Both are nullable: an account that never touches them keeps today's
-- behaviour exactly.

alter table digest_prefs add column if not exists max_items        smallint;
alter table digest_prefs add column if not exists niche_text       text;
alter table digest_prefs add column if not exists niche_updated_at timestamptz;

-- A value outside this range can only be a bug or a crafted request; the
-- application clamps first, this is the backstop.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'digest_prefs_max_items_range'
  ) then
    alter table digest_prefs
      add constraint digest_prefs_max_items_range
      check (max_items is null or (max_items >= 1 and max_items <= 10));
  end if;
end $$;
