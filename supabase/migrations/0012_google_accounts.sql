-- Google Sign-In on the EXISTING accounts table.
--
-- WHY NOT A NEW users TABLE:
-- accounts is already the identity row here. digest_prefs and digest_sends
-- both foreign-key accounts(id), auth.current_account() reads it, /api/me
-- renders it, and db.delete_account() is part of the GDPR cascade. A second
-- users table keyed on google_sub would leave every one of those pointing at
-- the old row, so a Google user would have an account that could not receive
-- a digest or be deleted. Three columns on the table that already exists
-- keeps one identity per person and changes no existing query.
--
-- The oc_uid design from 0011 is unchanged and deliberately so: an account
-- OWNS an oc_uid rather than replacing it, because consent, analyses,
-- recommendations, interactions, raw_files and locations are all keyed on
-- oc_uid. A Google signup adopts the browser's existing oc_uid exactly as a
-- magic-link signup does, so whatever the person did before signing in stays
-- theirs.

alter table accounts add column if not exists google_sub text;
alter table accounts add column if not exists name       text;
alter table accounts add column if not exists avatar_url text;

-- Partial unique index, not a plain unique constraint: every account created
-- by magic link has google_sub null, and in Postgres nulls do not collide in
-- a unique index - but being explicit about the predicate documents that the
-- null rows are expected rather than looking like an oversight.
--
-- google_sub, never email, is the join key. Google's `sub` is the stable,
-- immutable account id; an email address can be changed by its owner and a
-- Workspace address can be reassigned to a different human entirely, which
-- would silently hand that person someone else's account history.
create unique index if not exists accounts_google_sub_idx
  on accounts (google_sub) where google_sub is not null;

-- RLS is already enabled and forced on accounts (0011). Adding columns does
-- not change that, and no policy is added here - the service-role
-- DATABASE_URL connection the backend uses stays the only way in.
