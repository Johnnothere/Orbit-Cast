-- 0025_resync_id_sequences.sql
--
-- The Tokyo -> London copy (scripts/migrate_db.py, 30 Oct 2026) inserted rows
-- with their original ids but never advanced the identity sequences behind
-- them: its reset step matched only serial-owned sequences (pg_depend type
-- 'a') and skipped identity ones (type 'i'). Every insert afterwards drew an
-- id that a copied row already had:
--
--   save_analysis failed:  duplicate key ... analyses_pkey  (id)=(58)
--   save_raw_file failed:  duplicate key ... raw_files_pkey (id)=(3)
--
-- Each failure still used up one sequence value, so a table caught up on its
-- own only after as many failed inserts as it was behind - analyses had by
-- the time this was written; raw_files (3 vs 21) and recommendations (133 vs
-- 138) had not.
--
-- This sets every serial/identity sequence in public to at least the highest
-- id in its table. Forward only: a sequence already past its rows is left
-- alone, so this is safe to re-run and cannot hand out an id twice.

do $$
declare
  r   record;
  mx  bigint;
  cur bigint;
begin
  for r in
    select c.table_name, c.column_name,
           pg_get_serial_sequence(format('public.%I', c.table_name), c.column_name) as seq
      from information_schema.columns c
     where c.table_schema = 'public'
       and (c.is_identity = 'YES' or c.column_default like 'nextval(%')
  loop
    continue when r.seq is null;
    execute format('select max(%I) from public.%I', r.column_name, r.table_name) into mx;
    continue when mx is null;
    execute format('select last_value from %s', r.seq) into cur;
    if mx >= cur then
      perform setval(r.seq, mx, true);
      raise notice '% : sequence % -> %', r.table_name, cur, mx;
    end if;
  end loop;
end $$;
