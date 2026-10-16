-- One account, one person's CV (see identity.py). The lock is the person's
-- name as their first named document stated it. It lives on the account and
-- dies with the account (GDPR forget deletes the row), deliberately surviving
-- "delete my CV" so upload-analyse-delete-repeat can't dodge it.
alter table accounts add column if not exists cv_identity text;
alter table accounts add column if not exists cv_identity_set_at timestamptz;
