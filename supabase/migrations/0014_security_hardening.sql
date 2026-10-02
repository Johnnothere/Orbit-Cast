-- Security hardening, October 2026.
--
-- 1. Google Sign-In is the only door: the magic-link token table goes.
-- 2. The calendar-feed token is stored as a SHA-256 hash. A database dump no
--    longer hands anyone a working feed URL. Existing tokens are hashed in
--    place so current subscriptions keep working; the plaintext column is
--    emptied and kept (nullable) for one release, then can be dropped.
-- 3. An audit log of who touched what. Deny-by-default RLS like every other
--    table; only the backend's service-role connection writes or reads it.
--
-- Everything here is idempotent. Apply BEFORE deploying the matching code:
-- db.py reads cal_token_hash and writes audit_log unconditionally.

-- ── 1. magic links removed ───────────────────────────────────────────────
drop table if exists auth_tokens;

-- ── 2. calendar-feed token hashed ────────────────────────────────────────
alter table accounts add column if not exists cal_token_hash text;
create unique index if not exists accounts_cal_token_hash_idx
  on accounts (cal_token_hash) where cal_token_hash is not null;

-- pgcrypto is already enabled by 0001_init.sql (digest()).
update accounts
   set cal_token_hash = encode(digest(cal_token, 'sha256'), 'hex'),
       cal_token      = null
 where cal_token is not null and cal_token_hash is null;

-- Next release: alter table accounts drop column cal_token;
-- Next release: alter table digest_prefs drop column unsub_token;
--   (unsubscribe tokens are now derived with HMAC - auth.unsub_token - and the
--    stored column only serves links in emails sent before this change.)

-- ── 3. audit log ─────────────────────────────────────────────────────────
create table if not exists audit_log (
  id          bigserial   primary key,
  account_id  bigint      references accounts(id) on delete set null,
  action      text        not null,
  resource    text,
  detail      jsonb       not null default '{}'::jsonb,
  ip_address  text,
  user_agent  text,
  created_at  timestamptz not null default now()
);
create index if not exists audit_log_account_idx on audit_log (account_id, created_at desc);
create index if not exists audit_log_action_idx  on audit_log (action, created_at desc);
create index if not exists audit_log_created_idx on audit_log (created_at desc);

alter table audit_log enable row level security;
alter table audit_log force  row level security;
