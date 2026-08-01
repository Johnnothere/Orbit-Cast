-- OrbitCast AI: admin-editable config
--
-- Key/value store for settings the admin dashboard can change without a
-- code deploy - currently just the scoring engine's editable rules block
-- (key = 'scoring_rules'). Same RLS posture as every other table here:
-- deny-by-default for anon/authenticated, only the service_role connection
-- (DATABASE_URL) can read or write.

create table if not exists ai_config (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

alter table ai_config enable row level security;
alter table ai_config force row level security;
