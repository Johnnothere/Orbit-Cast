-- OrbitCast AI: consent-gated analytics + RAG knowledge base
--
-- Design principle: consent is the hinge. Every table except `consent`
-- itself only ever gets written to for a given oc_uid if that oc_uid has
-- consented=true in the consent table. No consent -> nothing persisted,
-- the app behaves exactly as it did before this migration.
--
-- All tables use RLS with zero policies, i.e. deny-by-default for the
-- anon/authenticated roles Supabase's PostgREST API would otherwise expose
-- them through. Only a service_role connection (which bypasses RLS by
-- Postgres/Supabase convention) can read or write - that's what the Flask
-- backend connects as via DATABASE_URL.

create extension if not exists vector;
create extension if not exists pgcrypto; -- gen_random_uuid, used nowhere critical but harmless to have

-- ─────────────────────────────────────────────
-- CONSENT
-- ─────────────────────────────────────────────
create table if not exists consent (
  oc_uid       text primary key,
  consented    boolean not null,
  consented_at timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

alter table consent enable row level security;
alter table consent force row level security;

-- ─────────────────────────────────────────────
-- ANALYSES  (one row per /api/analyze call, consented users only)
-- ─────────────────────────────────────────────
create table if not exists analyses (
  id         bigint generated always as identity primary key,
  oc_uid     text not null references consent(oc_uid) on delete cascade,
  created_at timestamptz not null default now(),
  is_cv      boolean not null,
  cv_text    text,          -- extracted text only, never the raw file
  profile    jsonb          -- the AI's structured profile output
);

create index if not exists analyses_oc_uid_idx on analyses (oc_uid);
create index if not exists analyses_created_at_idx on analyses (created_at);

alter table analyses enable row level security;
alter table analyses force row level security;

-- ─────────────────────────────────────────────
-- RECOMMENDATIONS  (one row per event recommended within an analysis)
-- ─────────────────────────────────────────────
create table if not exists recommendations (
  id          bigint generated always as identity primary key,
  analysis_id bigint not null references analyses(id) on delete cascade,
  event_id    text,
  title       text,
  category    text,
  fit_score   int,
  why         text,
  why_now     text,
  prepare     text,
  benefit     text,
  created_at  timestamptz not null default now()
);

create index if not exists recommendations_analysis_id_idx on recommendations (analysis_id);

alter table recommendations enable row level security;
alter table recommendations force row level security;

-- ─────────────────────────────────────────────
-- INTERACTIONS  (implicit signal - e.g. "View event" clicks)
-- ─────────────────────────────────────────────
create table if not exists interactions (
  id                bigint generated always as identity primary key,
  oc_uid            text not null references consent(oc_uid) on delete cascade,
  recommendation_id bigint references recommendations(id) on delete cascade,
  event_type        text not null check (event_type in ('view_event')),
  created_at        timestamptz not null default now()
);

create index if not exists interactions_recommendation_id_idx on interactions (recommendation_id);
create index if not exists interactions_oc_uid_idx on interactions (oc_uid);

alter table interactions enable row level security;
alter table interactions force row level security;

-- ─────────────────────────────────────────────
-- LABELED_EXAMPLES  (the RAG knowledge base - human-judged CV/event
-- reasoning pairs, retrieved by embedding similarity and injected as
-- dynamic few-shot examples into the scoring prompt)
-- ─────────────────────────────────────────────
create table if not exists labeled_examples (
  id              bigint generated always as identity primary key,
  source          text,                -- e.g. uploaded filename, or 'manual'
  profile_summary text not null,       -- the CV/person summary this example is about
  profile_json    jsonb,
  event_context   text,                -- description of the event(s) being judged
  judgment        text not null check (judgment in ('good','bad','borderline')),
  ideal_why       text,                -- the corrected/approved "why" reasoning
  embedding       vector(1024),        -- voyage-3 embedding of profile_summary + event_context
  reviewed_by     text,
  created_at      timestamptz not null default now()
);

-- HNSW over ivfflat here on purpose: this table starts empty and grows
-- slowly (each row is a human-reviewed judgment), and unlike ivfflat, HNSW
-- doesn't need a data-dependent "lists" parameter tuned against real data
-- to behave well - it builds and queries fine at 10 rows or 10,000.
create index if not exists labeled_examples_embedding_idx
  on labeled_examples using hnsw (embedding vector_cosine_ops);

alter table labeled_examples enable row level security;
alter table labeled_examples force row level security;
