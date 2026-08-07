-- OrbitCast AI: raw resume storage + location consent
--
-- Two additions, both consent-gated the same way as everything else in this
-- schema - nothing here changes that design, it extends it:
--
-- 1. raw_files - the ORIGINAL uploaded CV/resume file, stored byte-for-byte,
--    alongside (not instead of) the existing extracted-text pipeline in
--    ai_engine.py, which is unchanged. One row per analysis that came from
--    an uploaded file; only written when that oc_uid has consented, exactly
--    like analyses/recommendations/interactions already are.
--
-- 2. user_locations - one-shot geolocation captures, gated by their OWN
--    consent flag (location_consented on the consent table), separate from
--    the general analysis consent. Granting GPS access is a materially
--    different decision from letting us save a CV, so it gets its own
--    yes/no rather than being folded into the existing banner.

alter table consent add column if not exists location_consented boolean;
alter table consent add column if not exists location_consented_at timestamptz;

create table if not exists raw_files (
  id           bigint generated always as identity primary key,
  analysis_id  bigint not null references analyses(id) on delete cascade,
  filename     text,
  content_type text,
  file_bytes   bytea not null,
  size_bytes   int not null,
  created_at   timestamptz not null default now()
);

create index if not exists raw_files_analysis_id_idx on raw_files (analysis_id);

alter table raw_files enable row level security;
alter table raw_files force row level security;

create table if not exists user_locations (
  id         bigint generated always as identity primary key,
  oc_uid     text not null references consent(oc_uid) on delete cascade,
  lat        double precision not null,
  lng        double precision not null,
  accuracy_m double precision,
  created_at timestamptz not null default now()
);

create index if not exists user_locations_oc_uid_idx on user_locations (oc_uid);

alter table user_locations enable row level security;
alter table user_locations force row level security;
