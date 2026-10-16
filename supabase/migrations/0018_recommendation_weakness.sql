-- 0018_recommendation_weakness.sql
--
-- One honest limitation per match, written by the scoring pass alongside the
-- existing "why / why_now / prepare / benefit" fields. The UI shows it on
-- every recommendation card, right under the band label.
--
-- WHY
-- A bare integer ("72") reads as marketing. The research on LinkedIn's
-- "top applicant" score and similar bare-number affordances is unambiguous:
-- without visible reasoning, people treat the number as either flattery or
-- a BS metric and discount it. Stating one honest limit next to the score
-- flips that read: it signals the engine isn't trying to sell them on the
-- match, which is exactly what the honest-matching brand is for.
--
-- Additive and idempotent; safe to apply before or after the code deploy.
-- Scoring prompt is forward-compatible: existing rows have weakness NULL
-- and the UI falls back cleanly when the field is missing.

alter table recommendations add column if not exists weakness text;
