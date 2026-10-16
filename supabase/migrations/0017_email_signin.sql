-- 0017_email_signin.sql
--
-- Email sign-in alongside Google. One account, two possible doors.
--
-- WHY A SECOND DOOR
-- Google-only locked out the people this app most wants: defence, intel,
-- government and legal users whose corporate IT blocks third-party OAuth
-- consent, and anyone who simply doesn't use Gmail. The free anonymous
-- analysis closed the "nothing to try" gap; this closes the "I can't make an
-- account" gap. The two sign-in paths converge on the same accounts row and
-- the same signed session cookie, so everything downstream (/api/me, the
-- identity lock, digests, saved events) is unchanged.
--
-- WHY THIS CAN BE REINTRODUCED SAFELY
-- Migration 0014 dropped the old auth_tokens table because the magic-link
-- path had a cluster of problems: tokens stored in plaintext, no email
-- verification before linking an existing account, no rate limits on the
-- "request a link" endpoint, no browser binding on verify. All four are
-- fixed in this re-introduction:
--   1. token_hash only, never the plaintext (same shape as cal_token_hash).
--   2. For a NEW account, delivery of the mail IS verification: receiving
--      the link proves control of the mailbox. For an EXISTING account by
--      email, same proof. There is nothing to "verify separately".
--   3. app.py rate-limits requests per email and per IP, and the request
--      endpoint always answers {ok:true} so it cannot be used to probe
--      which addresses have accounts.
--   4. The session cookie that a successful verify sets is bound to the
--      User-Agent, same as the Google path (auth.make_session).
--
-- Additive and idempotent. Safe to apply before or after the code deploy.

-- Fresh-install path: full schema.
create table if not exists auth_tokens (
  id              bigserial     primary key,
  -- The address the link was sent to. Lowercased by the caller.
  email           text          not null,
  -- SHA-256 of the plaintext token (hex). A database dump no longer hands
  -- anyone a working link; only the person who received the mail has the
  -- preimage. Matches the cal_token_hash pattern already in use.
  token_hash      text          not null,
  -- 'signin' today, so we don't have to add a column tomorrow if we ever
  -- reuse this table for an "invite someone" or "verify new address" flow.
  purpose         text          not null default 'signin',
  expires_at      timestamptz   not null,
  used_at         timestamptz,
  -- Context recorded at request time, for the audit trail and for
  -- diagnosing a reported "I never got the link". Not used by the verify
  -- step: the real check is the hash, the TTL and single-use.
  requested_ip    text,
  requested_ua    text,
  -- The oc_uid the browser presented at request time. The verify step
  -- hands this to get_or_create_account_by_email so the account can adopt
  -- it (iff no account already owns it), the same rule as the Google path.
  oc_uid_hint     text,
  created_at      timestamptz   not null default now()
);

-- Upgrade path: on Tokyo, migration 0014's `drop table if exists auth_tokens`
-- did not clear two expired-but-unused orphans created minutes before 0014
-- ran, so when 0017 first landed the table still carried the stripped-down
-- schema the old magic-link code wrote (email, token_hash, created_at,
-- expires_at, used_at — no id, no purpose, no request context, no oc_uid
-- hint). These add-column calls are no-ops on a fresh schema and bring an
-- older one up to the full shape 0017 defines. Any orphan rows are
-- harmless once they expire; the index below enforces uniqueness going
-- forward only.
alter table auth_tokens add column if not exists id          bigserial;
alter table auth_tokens add column if not exists purpose     text        not null default 'signin';
alter table auth_tokens add column if not exists requested_ip text;
alter table auth_tokens add column if not exists requested_ua text;
alter table auth_tokens add column if not exists oc_uid_hint  text;
-- A legacy table may miss NOT NULL on these; we only enforce it going
-- forward via the application layer, since historical rows (if any) could
-- in principle have blanks.
do $$
begin
  if not exists (
    select 1 from information_schema.table_constraints
     where table_name = 'auth_tokens' and constraint_type = 'PRIMARY KEY'
  ) then
    alter table auth_tokens add primary key (id);
  end if;
end$$;

-- One hex sha256 maps to one row; UNIQUE is what lets consume use a plain
-- UPDATE ... WHERE token_hash = $1 AND used_at IS NULL instead of a lock.
create unique index if not exists auth_tokens_hash_idx on auth_tokens (token_hash);
-- Rate-limit lookups by email want "how many issued recently for this
-- address" fast; the digest scheduler-ish cleanup wants "which are expired".
create index if not exists auth_tokens_email_idx   on auth_tokens (email, created_at desc);
create index if not exists auth_tokens_expires_idx on auth_tokens (expires_at);

alter table auth_tokens enable row level security;
alter table auth_tokens force  row level security;
