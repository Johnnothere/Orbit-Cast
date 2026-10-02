-- The account panel: saved events that follow the person across devices,
-- "not for me" on a match, digest categories and snooze, and a private
-- calendar feed.
--
-- Everything here is ADDITIVE and idempotent. No existing column, row or
-- query changes, so this can be applied before or after the code that uses
-- it deploys: db.py reads every new column defensively and degrades to the
-- pre-migration behaviour when one is missing.

-- ── Saved events ──────────────────────────────────────────────────────────
-- A SNAPSHOT of the event is stored, not just its id. The catalogue is
-- rebuilt every 30 minutes and an event drops out of it the moment its
-- source stops listing it; a saved list keyed on id alone would silently
-- lose rows. People notice that, and it is the complaint legacy event apps
-- earn most reliably: "I saved it and now it is gone".
create table if not exists saved_events (
  account_id bigint      not null references accounts(id) on delete cascade,
  event_id   text        not null,
  title      text        not null,
  url        text,
  category   text,
  event_date text,                      -- as the catalogue gives it (ISO or source text)
  event_time text,
  location   text,
  is_online  boolean,
  saved_at   timestamptz not null default now(),
  primary key (account_id, event_id)
);
create index if not exists saved_events_account_saved_idx
  on saved_events (account_id, saved_at desc);

-- ── "Not for me" ──────────────────────────────────────────────────────────
-- A dismissed event is never emailed to this account again. That is all it
-- does, and the UI says so: it is a mute, not a training signal.
create table if not exists dismissed_events (
  account_id bigint      not null references accounts(id) on delete cascade,
  event_id   text        not null,
  title      text,
  created_at timestamptz not null default now(),
  primary key (account_id, event_id)
);

-- ── Digest: categories and snooze ─────────────────────────────────────────
-- categories null (or empty) means "everything", so every existing
-- subscriber keeps exactly the digest they have now.
alter table digest_prefs add column if not exists categories   text[];
alter table digest_prefs add column if not exists snooze_until timestamptz;

-- ── Private calendar feed ─────────────────────────────────────────────────
-- A calendar app cannot sign in, so the feed URL carries its own secret.
-- It is a capability: anyone holding the URL can read this account's saved
-- events and nothing else. Regenerating it revokes the old one.
alter table accounts add column if not exists cal_token text;
create unique index if not exists accounts_cal_token_idx
  on accounts (cal_token) where cal_token is not null;

-- ── RLS: deny by default, same as every other table here ──────────────────
alter table saved_events     enable row level security;
alter table saved_events     force  row level security;
alter table dismissed_events enable row level security;
alter table dismissed_events force  row level security;
