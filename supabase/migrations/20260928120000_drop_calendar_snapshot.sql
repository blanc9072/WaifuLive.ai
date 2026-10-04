-- Tears down the calendar snapshot cache (CS1, added in
-- 20260927120000_add_calendar_snapshot.sql) — superseded one day later by
-- EK1, which repoints every calendar read at EventKit directly (see
-- core/calendar_read_ek.py). EventKit answers a full multi-calendar window
-- in single-digit milliseconds and correctly expands recurring events,
-- which the cache's osascript-based refresher never did — so there is no
-- longer a slow read to cache, and no code reads these tables anymore
-- (core/calendar_refresher.py and core.db's snapshot functions were
-- removed in the same change that authored this migration).
--
-- Pure tidiness, not load-bearing: the application already stopped using
-- these tables the moment EK1 shipped, regardless of when this migration
-- is applied to the hosted database.
drop table if exists calendar_snapshot;
drop table if exists calendar_refresh_state;
