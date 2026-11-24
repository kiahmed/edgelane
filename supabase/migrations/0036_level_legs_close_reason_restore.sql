-- ============================================================================
-- 0036 — level legs: close_reason CHECK back to the 0035 list
--
-- A "progressed" directional win (2026-11-24) was added and removed the same
-- day; an earlier draft of this migration widened the CHECK to allow it and
-- was applied. This restores the 0035 constraint exactly.
--
-- Rows the progressed rule wrote (the live engine keeps writing them until it
-- is redeployed) are neutralised first — result 'unresolved' (read as a LOSS
-- by the stats, day_summary.LEGACY_RESULTS) and close_reason null — so the
-- add constraint always succeeds (tools/db_push.py re-runs every migration)
-- and those early-closed wins, which the owner rejected, leave the win rate.
--
-- APPLY AFTER the level engine without the progressed rule is deployed (the
-- old engine could still write 'progressed' until then). Idempotent.
-- ============================================================================

do $$
declare
    r record;
begin
    for r in
        select conrelid::regclass as tbl, conname
          from pg_constraint
         where contype = 'c'
           and conrelid in ('public.lvl_legs'::regclass, 'lvl_replay.legs'::regclass)
           and pg_get_constraintdef(oid) ilike '%close_reason%'
    loop
        execute format('alter table %s drop constraint %I', r.tbl, r.conname);
    end loop;
end $$;

update public.lvl_legs set result = 'unresolved', close_reason = null
 where close_reason = 'progressed';
update lvl_replay.legs set result = 'unresolved', close_reason = null
 where close_reason = 'progressed';

alter table public.lvl_legs add constraint lvl_legs_close_reason_check
  check (close_reason in ('target', 'invalidation', 'flipped', 'session_close', 'zone_held',
                          'zone_exit'));

alter table lvl_replay.legs add constraint legs_close_reason_check
  check (close_reason in ('target', 'invalidation', 'flipped', 'session_close', 'zone_held',
                          'zone_exit'));
