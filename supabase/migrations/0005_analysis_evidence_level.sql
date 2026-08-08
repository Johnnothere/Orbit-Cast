-- Needed for the "reuse my last profile" returning-visitor feature: without
-- these, a re-score from a stored profile would always treat it as "rich"
-- evidence, silently skipping the orientation section for someone whose
-- original submission was thin (a self-description rather than a CV).
alter table analyses add column if not exists evidence_level text;
alter table analyses add column if not exists field text;
