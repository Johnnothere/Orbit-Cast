-- Accounts, passwordless login, and scheduled email digests.
--
-- DESIGN NOTE — why an account OWNS an oc_uid instead of replacing it:
-- every existing table in this schema (consent, analyses, recommendations,
-- interactions, raw_files, locations) is keyed on oc_uid. Re-keying all of
-- them onto a new user id would be a large, risky migration with a data-loss
-- failure mode. Instead an account claims the oc_uid the browser already has;
-- logging in re-issues that same oc_uid as the cookie. The result is that
-- signing up KEEPS the analyses, consent and recommendations the person
-- already had, and not one existing table or query changes.
--
-- Consequence to remember: oc_uid is now sometimes an identified person, not
-- an anonymous browser. The GDPR delete path has to remove the account too,
-- which db.forget() does via the cascade below.

-- ── Accounts ──────────────────────────────────────────────────────────────
create table if not exists accounts (
  id            bigserial primary key,
  email         text        not null,
  oc_uid        text        not null unique,
  created_at    timestamptz not null default now(),
  last_login_at timestamptz
);

-- Case-insensitive uniqueness without requiring the citext extension, which
-- is not guaranteed to be enabled on a given Supabase project.
create unique index if not exists accounts_email_lower_idx on accounts (lower(email));

-- ── Magic-link tokens ─────────────────────────────────────────────────────
-- Only the SHA-256 of the token is stored. A leaked database therefore does
-- not hand anyone a working login link, the same reason passwords are hashed.
create table if not exists auth_tokens (
  token_hash text        primary key,
  email      text        not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_at    timestamptz
);
create index if not exists auth_tokens_expires_idx on auth_tokens (expires_at);

-- ── Digest preferences ────────────────────────────────────────────────────
-- Modelled on a reminders app: daily, every N days, or weekly on a chosen
-- weekday, at a chosen hour, in the person's own timezone.
create table if not exists digest_prefs (
  account_id    bigint      primary key references accounts(id) on delete cascade,
  frequency     text        not null default 'weekly'
                            check (frequency in ('daily', 'every_n_days', 'weekly')),
  interval_days int         not null default 3  check (interval_days between 1 and 30),
  weekday       int         not null default 0  check (weekday between 0 and 6), -- 0 = Monday
  send_hour     int         not null default 8  check (send_hour between 0 and 23),
  timezone      text        not null default 'Europe/London',
  paused        boolean     not null default false,
  next_send_at  timestamptz,
  unsub_token   text        not null unique,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- The scheduler's only query: "what is due?". Partial index so paused rows
-- cost nothing to skip.
create index if not exists digest_prefs_due_idx
  on digest_prefs (next_send_at) where paused = false;

-- ── Send log ──────────────────────────────────────────────────────────────
-- The idempotency record. period_key is the LOCAL date the digest was due,
-- so the unique index below makes a double-send physically impossible even
-- if the process restarts mid-run — which on Railway it does, every deploy.
create table if not exists digest_sends (
  id          bigserial   primary key,
  account_id  bigint      not null references accounts(id) on delete cascade,
  period_key  text        not null,
  sent_at     timestamptz not null default now(),
  match_count int         not null default 0,
  event_ids   text[]      not null default '{}',
  status      text        not null default 'sent'
);
create unique index if not exists digest_sends_account_period_idx
  on digest_sends (account_id, period_key);
create index if not exists digest_sends_account_sent_idx
  on digest_sends (account_id, sent_at desc);

-- ── RLS: deny by default, same as every other table here ──────────────────
-- Enabled AND forced with zero policies: nothing reaches these rows except
-- the service-role DATABASE_URL connection the Flask backend uses. auth_tokens
-- and accounts especially — a readable auth_tokens table is an account
-- takeover.
alter table accounts     enable row level security;
alter table accounts     force  row level security;
alter table auth_tokens  enable row level security;
alter table auth_tokens  force  row level security;
alter table digest_prefs enable row level security;
alter table digest_prefs force  row level security;
alter table digest_sends enable row level security;
alter table digest_sends force  row level security;
